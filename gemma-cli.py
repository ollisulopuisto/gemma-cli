#!/usr/bin/env uv run
import httpx
import asyncio
import json
import os
import yaml
import argparse
import sys
import re
import html
from datetime import datetime

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML, ANSI
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.styles import Style
from prompt_toolkit.lexers import Lexer

from gemma_utils import (
    parse_tool_call, get_system_context, get_sandbox_command, 
    get_all_binaries, get_base_binary, update_config_whitelist, get_skills_context
)

# --- Utilities ---

def strip_ansi(text):
    """Removes ANSI escape sequences from a string."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

class ChatLexer(Lexer):
    """Simple lexer to colorize chat lines based on prefixes."""
    def lex_document(self, document):
        def get_line(lineno):
            line = document.lines[lineno]
            if line.startswith("User:"):
                return [("class:user-label", "User:"), ("", line[5:])]
            elif line.startswith("Gemma:"):
                return [("class:gemma-label", "Gemma:"), ("", line[6:])]
            elif line.startswith("[System]") or line.startswith("[Tool Output]") or line.startswith("[Error]"):
                return [("class:system-label", line)]
            elif line.startswith("[Thought]"):
                return [("class:thought-label", line)]
            return [("", line)]
        return get_line

# --- Core Logic ---

def load_config(config_path):
    if not os.path.exists(config_path):
        return None
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def call_gemma_async(messages, config):
    server = config.get('server', {})
    payload = {"model": server.get('model'), "messages": messages, "temperature": 0.1}
    auth = server.get('auth', {})
    url = server.get('url')
    
    start_time = asyncio.get_event_loop().time()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, json=payload, 
            auth=(auth.get('username'), auth.get('password')),
            timeout=120.0
        )
        response.raise_for_status()
        data = response.json()
        message = data['choices'][0]['message']
        end_time = asyncio.get_event_loop().time()
        return message.get('content', ''), message.get('reasoning_content', ''), end_time - start_time

async def run_command_async(command, sandbox_config):
    full_command, _ = get_sandbox_command(command, sandbox_config)
    process = await asyncio.create_subprocess_shell(
        full_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    output = f"STDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
    return strip_ansi(output)

# --- TUI Application ---

session_whitelist = set()

class GemmaApp:
    def __init__(self, config, args, ctx, system_prompt):
        self.config = config
        self.args = args
        self.ctx = ctx
        self.is_thinking = False
        self.input_enabled = True
        self.last_duration = 0.0
        self.messages = [{"role": "system", "content": system_prompt}]
        self.waiting_for_approval = None 
        
        # Logging
        self.log_path = args.log_file or config.get('logging', {}).get('path', 'gemma_chat.log')
        self.logging_enabled = config.get('logging', {}).get('enabled', True)
        
        # Spinner state
        self.spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.spinner_idx = 0
        
        # UI Components
        self.output_field = TextArea(read_only=True, scrollbar=True, focusable=True, lexer=ChatLexer())
        self.input_field = TextArea(height=1, multiline=False, focusable=True, style="class:input-area")
        self.prompt_label = FormattedTextControl(" User: ")
        self.sb_summary = config['sandbox']['level'] if config['sandbox']['enabled'] else "OFF"
        
        # Fetch skills summary
        _, skill_files = get_skills_context(config)
        self.skills_summary = ", ".join(skill_files) if skill_files else "None"

        self.status_bar = Window(
            content=FormattedTextControl(self.get_status_text),
            height=1,
            style="class:status-bar"
        )
        
        padding_char_top = "▄" * 500
        padding_char_bottom = "▀" * 500
        
        # Windows
        self.top_padding = Window(content=FormattedTextControl(padding_char_top), height=1, style="class:padding-line")
        self.bottom_padding = Window(content=FormattedTextControl(padding_char_bottom), height=1, style="class:padding-line")
        self.prompt_window = Window(content=self.prompt_label, height=1, dont_extend_width=True, style="class:input-area")
        
        self.input_field.window.cursor = Condition(lambda: self.input_enabled)

        self.layout = Layout(
            HSplit([
                self.output_field,
                self.top_padding,
                VSplit([self.prompt_window, self.input_field]),
                self.bottom_padding,
                self.status_bar
            ]),
            focused_element=self.input_field
        )
        
        self.kb = KeyBindings()
        @self.kb.add("c-c")
        @self.kb.add("c-q")
        def _(event): event.app.exit()

        @self.kb.add("tab")
        def _(event): event.app.layout.focus_next()

        @Condition
        def is_waiting():
            return self.waiting_for_approval is not None

        @self.kb.add("y", filter=is_waiting)
        @self.kb.add("n", filter=is_waiting)
        @self.kb.add("s", filter=is_waiting)
        @self.kb.add("p", filter=is_waiting)
        def _(event):
            _, resolve = self.waiting_for_approval
            resolve(event.data.lower())

        @self.kb.add("enter")
        def _(event):
            if not self.input_enabled and not self.waiting_for_approval:
                return 
            content = self.input_field.text.strip()
            if not content: return
            self.input_field.text = ""
            if self.waiting_for_approval:
                _, resolve = self.waiting_for_approval
                resolve(content.lower())
            else:
                asyncio.create_task(self.handle_input(content))

        self.app = Application(
            layout=self.layout, key_bindings=self.kb, full_screen=True, mouse_support=False,
            style=Style.from_dict({
                'user-label': 'ansicyan bold',
                'gemma-label': 'ansimagenta bold',
                'system-label': 'ansiyellow italic',
                'thought-label': 'ansigray italic',
                'input-area': 'bg:#333333 #ffffff',
                'input-area-disabled': 'bg:#222222 #888888',
                'padding-line': 'fg:#333333 bg:#000000',
                'padding-line-disabled': 'fg:#222222 bg:#000000',
                'status-bar': 'bg:#000000',
                'status-label': '#888888 bold',
                'status-value': '#ffffff',
                'status-idle': 'ansigreen',
                'status-thinking': 'ansired',
                'status-waiting': 'ansiyellow',
            })
        )
        asyncio.create_task(self._spinner_loop())

    async def _spinner_loop(self):
        while True:
            if self.is_thinking:
                self.spinner_idx += 1
                self.app.invalidate()
            await asyncio.sleep(0.1)

    def get_status_text(self):
        L = 'fg="#888888" b' # Label style
        V = 'fg="#ffffff"'    # Value style
        
        # Escape dynamic content to avoid XML/HTML parsing errors
        user = html.escape(self.ctx["username"])
        cwd = html.escape(os.getcwd())
        sb = html.escape(self.sb_summary)
        skills = html.escape(self.skills_summary)

        if self.is_thinking:
            frame = self.spinner_frames[self.spinner_idx % len(self.spinner_frames)]
            status = f'<style fg="ansired">{frame} THINKING...</style>'
        elif self.waiting_for_approval:
            status = '<style fg="ansiyellow">WAITING APPROVAL</style>'
        else:
            status = '<style fg="ansigreen">IDLE (type /help)</style>'
            
        dur = f' | <style {L}>Last:</style> <style {V}>{self.last_duration:.2f}s</style>' if self.last_duration > 0 else ""
        
        return HTML(
            f' <style {L}>User:</style> <style {V}>{user}</style> | '
            f'<style {L}>CWD:</style> <style {V}>{cwd}</style> | '
            f'<style {L}>Sandbox:</style> <style {V}>{sb}</style> | '
            f'<style {L}>Skills:</style> <style {V}>{skills}</style> | '
            f'<style {L}>Status:</style> {status}{dur} '
        )

    def log(self, text):
        self.output_field.read_only = False
        self.output_field.buffer.insert_text(text + "\n")
        self.output_field.read_only = True
        if self.logging_enabled:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {strip_ansi(text)}\n")

    def _set_input_enabled(self, enabled: bool):
        self.input_enabled = enabled
        self.input_field.read_only = not enabled
        style = "class:input-area" if enabled else "class:input-area-disabled"
        padding_style = "class:padding-line" if enabled else "class:padding-line-disabled"
        
        self.input_field.style = style
        self.top_padding.style = padding_style
        self.bottom_padding.style = padding_style
        self.prompt_window.style = style
        if not enabled: self.app.layout.focus(self.output_field)
        else: self.app.layout.focus(self.input_field)

    async def handle_input(self, text):
        global session_whitelist
        if text.startswith("/"):
            cmd = text.lower().strip()
            if cmd == "/exit" or cmd == "/quit": self.app.exit()
            elif cmd == "/clear":
                self.output_field.read_only = False
                self.output_field.text = ""
                self.output_field.read_only = True
                self.log("[System] Chat log cleared.")
            elif cmd == "/help":
                self.log("\n[System] Available commands:")
                self.log(" /help  - Show this help message")
                self.log(" /clear - Clear the chat log")
                self.log(" /exit  - Exit the application\n")
            else: self.log(f"[System] Unknown command: {text}")
            return

        self.log(f"User: {text}\n")
        self.messages.append({"role": "user", "content": text})
        self._set_input_enabled(False)
        try:
            while True:
                self.is_thinking = True
                self.app.invalidate()
                try:
                    content, reasoning, duration = await call_gemma_async(self.messages, self.config)
                    self.is_thinking = False
                    self.last_duration = duration
                    if reasoning and self.args.show_reasoning: self.log(f"[Thought]\n{reasoning}\n")
                    self.log(f"Gemma: {content}\n")
                    self.messages.append({"role": "assistant", "content": content})
                    cmd = parse_tool_call(content)
                    if cmd:
                        binaries = get_all_binaries(cmd)
                        persistent_whitelist = self.config.get('sandbox', {}).get('whitelist', [])
                        all_approved = self.args.yes or all(b in session_whitelist or b in persistent_whitelist for b in binaries)
                        if all_approved:
                            obs = await run_command_async(cmd, self.config['sandbox'])
                        else:
                            self.log(f"[System] Proposed Command: {cmd}")
                            self.prompt_label.text = " Execute? [y]es, [n]o, [s]ession, [p]ersistent: "
                            self._set_input_enabled(True)
                            future = asyncio.get_event_loop().create_future()
                            self.waiting_for_approval = (cmd, lambda val: future.set_result(val))
                            self.app.invalidate()
                            ans = await future
                            self.waiting_for_approval = None
                            self._set_input_enabled(False)
                            self.prompt_label.text = " User: "
                            self.log(f"[System] User selected: {ans}")
                            if ans in ['y', 's', 'p']:
                                if ans == 's':
                                    for b in binaries: session_whitelist.add(b)
                                elif ans == 'p':
                                    for b in binaries:
                                        update_config_whitelist(self.args.config, b)
                                        session_whitelist.add(b)
                                obs = await run_command_async(cmd, self.config['sandbox'])
                            else: obs = "User denied execution."
                        self.messages.append({"role": "user", "content": f"Observation:\n{obs}"})
                        if self.args.show_output: self.log(f"[Tool Output]\n{obs}\n")
                        continue
                    else: break
                except Exception as e:
                    self.is_thinking = False
                    self.log(f"[Error] {str(e)}")
                    break
        finally:
            self._set_input_enabled(True)
            self.app.invalidate()

    async def run(self):
        self.log(f"Gemma CLI Agent started. CWD: {os.getcwd()}\n")
        await self.app.run_async()

async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-file", help="Path to log file")
    parser.add_argument("--no-log", action="store_true", help="Disable logging")
    parser.add_argument("--show-output", action="store_true")
    parser.add_argument("--show-reasoning", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("--sandbox", choices=["off", "permissive", "strict"])
    parser.add_argument("--allow-path", action="append")
    parser.add_argument("--skill", action="append")
    args = parser.parse_args()
    config = load_config(args.config)
    if not config: print("Error: Config not found."); return
    if args.no_log: config.setdefault('logging', {})['enabled'] = False
    if args.no_sandbox: config['sandbox']['enabled'] = False
    if args.sandbox:
        config['sandbox']['level'] = args.sandbox
        config['sandbox']['enabled'] = (args.sandbox != "off")
    ctx = get_system_context()
    skills_text, _ = get_skills_context(config)
    system_prompt = f"""You are a senior CLI agent with direct access to the user's computer via shell commands.
Current Context (Sniffed from System):
- OS: {ctx['os']} ({ctx['os_release']})
- User: {ctx['username']}
- Directory: {ctx['cwd']}
- Time: {ctx['now']}
- Shell: {ctx['shell']}

{skills_text}

SPECIAL TOOLS (via python gemma_utils.py):
1. **Smarter Editing**: To edit a file precisely, use:
   ```tool_code
   python gemma_utils.py edit path/to/file old.txt new.txt
   ```
2. **Sub-agents**: To delegate a complex sub-task to another agent:
   ```tool_code
   python gemma_utils.py subagent "Objective for the sub-agent"
   ```
3. **Notifications**: To send an alert to the user's configured webhook (Slack/Discord):
   ```tool_code
   python gemma_utils.py notify "Message to send"
   ```

RULES:
1. You have REAL-TIME capabilities. USE A TOOL for system info.
2. BE CONCISE. Do not repeat previous observations or reasoning steps unless requested.
3. To use a tool, output:
```tool_code
command
```
4. After receiving an "Observation:", provide the final answer or next command.
5. Always explain reasoning briefly before a tool call."""
    app = GemmaApp(config, args, ctx, system_prompt)
    await app.run()

if __name__ == "__main__":
    try: asyncio.run(main_async())
    except KeyboardInterrupt: pass
