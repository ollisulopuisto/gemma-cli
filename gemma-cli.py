#!/usr/bin/env uv run
import httpx
import asyncio
import json
import os
import yaml
import argparse
import sys
import re
from datetime import datetime

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML, ANSI
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.styles import Style
from prompt_toolkit.lexers import Lexer

from gemma_utils import (
    parse_tool_call, get_system_context, get_sandbox_command, 
    get_all_binaries, update_config_whitelist, get_skills_context
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

class GemmaApp:
    def __init__(self, config, args, ctx, system_prompt):
        self.config = config
        self.args = args
        self.ctx = ctx
        self.is_thinking = False
        self.last_duration = 0.0
        self.messages = [{"role": "system", "content": system_prompt}]
        self.waiting_for_approval = None 
        
        # UI Components
        self.output_field = TextArea(read_only=True, scrollbar=True, focusable=True, lexer=ChatLexer())
        self.input_field = TextArea(height=1, prompt=" User: ", multiline=False, focusable=True, style="class:input-area")
        self.sb_summary = config['sandbox']['level'] if config['sandbox']['enabled'] else "OFF"
        
        self.status_bar = Window(
            content=FormattedTextControl(self.get_status_text),
            height=1,
            style="class:status-bar"
        )
        
        # Padding lines using half blocks to simulate 1/2 line height
        padding_char_top = "▄" * 500
        padding_char_bottom = "▀" * 500
        
        self.layout = Layout(
            HSplit([
                self.output_field,
                Window(content=FormattedTextControl(padding_char_top), height=1, style="class:padding-line"),
                self.input_field,
                Window(content=FormattedTextControl(padding_char_bottom), height=1, style="class:padding-line"),
                self.status_bar
            ]),
            focused_element=self.input_field
        )
        
        self.kb = KeyBindings()
        
        @self.kb.add("c-c")
        @self.kb.add("c-q")
        def _(event):
            event.app.exit()

        @self.kb.add("tab")
        def _(event):
            event.app.layout.focus_next()

        @self.kb.add("enter")
        def _(event):
            content = self.input_field.text.strip()
            if not content: return
            self.input_field.text = ""
            
            if self.waiting_for_approval:
                cmd, resolve = self.waiting_for_approval
                if content.lower() in ['y', 'yes', 's', 'p']:
                    self.waiting_for_approval = None
                    self.input_field.prompt = " User: "
                    asyncio.create_task(resolve(content.lower()))
                else:
                    self.waiting_for_approval = None
                    self.input_field.prompt = " User: "
                    self.log("[System] Command denied.")
                    asyncio.create_task(resolve('n'))
            else:
                asyncio.create_task(self.handle_input(content))

        self.app = Application(
            layout=self.layout, key_bindings=self.kb, full_screen=True, mouse_support=True,
            style=Style.from_dict({
                'user-label': 'ansicyan bold',
                'gemma-label': 'ansimagenta bold',
                'system-label': 'ansiyellow italic',
                'thought-label': 'ansigray italic',
                'input-area': 'bg:#333333 #ffffff',
                'padding-line': 'fg:#333333 bg:#000000',
                'status-bar': 'bg:#000000 #ffffff',
            })
        )

    def get_status_text(self):
        status = "THINKING..." if self.is_thinking else ("WAITING APPROVAL" if self.waiting_for_approval else "IDLE")
        color = "ansired" if self.is_thinking or self.waiting_for_approval else "ansigreen"
        dur_text = f" | Last: {self.last_duration:.2f}s" if self.last_duration > 0 else ""
        return HTML(
            f" User: {self.ctx['username']} | "
            f"CWD: {os.getcwd()} | "
            f"Sandbox: {self.sb_summary} | "
            f"Status: <{color}>{status}</{color}>{dur_text} "
        )

    def log(self, text):
        self.output_field.text += text + "\n"
        self.output_field.buffer.cursor_position = len(self.output_field.text)

    async def handle_input(self, text):
        if text.lower() in ["exit", "quit"]:
            self.app.exit()
            return

        self.log(f"User: {text}\n")
        self.messages.append({"role": "user", "content": text})
        
        while True:
            self.is_thinking = True
            self.app.invalidate()
            try:
                content, reasoning, duration = await call_gemma_async(self.messages, self.config)
                self.is_thinking = False
                self.last_duration = duration
                
                if reasoning and self.args.show_reasoning:
                    self.log(f"[Thought]\n{reasoning}\n")
                
                self.log(f"Gemma: {content}\n")
                self.messages.append({"role": "assistant", "content": content})
                
                cmd = parse_tool_call(content)
                if cmd:
                    if self.args.yes:
                        obs = await run_command_async(cmd, self.config['sandbox'])
                    else:
                        self.log(f"[System] Proposed Command: {cmd}")
                        self.input_field.prompt = f" Execute '{cmd}'? (y/n/s/p): "
                        
                        future = asyncio.get_event_loop().create_future()
                        self.waiting_for_approval = (cmd, lambda val: future.set_result(val))
                        self.app.invalidate()
                        
                        ans = await future
                        if ans in ['y', 's', 'p']:
                            obs = await run_command_async(cmd, self.config['sandbox'])
                        else:
                            obs = "User denied execution."
                    
                    self.messages.append({"role": "user", "content": f"Observation:\n{obs}"})
                    if self.args.show_output:
                        self.log(f"[Tool Output]\n{obs}\n")
                    continue
                else:
                    break
            except Exception as e:
                self.is_thinking = False
                self.log(f"[Error] {str(e)}")
                break
            finally:
                self.app.invalidate()
                self.app.layout.focus(self.input_field)

    async def run(self):
        self.log(f"Gemma CLI Agent started. CWD: {os.getcwd()}")
        await self.app.run_async()

async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--show-output", action="store_true")
    parser.add_argument("--show-reasoning", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("--sandbox", choices=["off", "permissive", "strict"])
    parser.add_argument("--allow-path", action="append")
    parser.add_argument("--skill", action="append")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print(f"Error: Config not found.")
        return

    # Overrides
    if args.no_sandbox: config['sandbox']['enabled'] = False
    if args.sandbox:
        config['sandbox']['level'] = args.sandbox
        config['sandbox']['enabled'] = (args.sandbox != "off")
    
    ctx = get_system_context()
    skills_text, skill_files = get_skills_context(config)
    
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
   # Prepare old.txt and new.txt with EXACT content, then:
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
1. You have REAL-TIME capabilities. If asked for the time, weather (via curl), system stats, or file info, USE A TOOL.
2. DO NOT say "I am a language model" or "I don't have access to real-time info". You HAVE access via the shell.
3. To use a tool, you MUST output a code block like this:
```tool_code
date
```
4. After receiving an "Observation:", analyze the output and provide the final answer or next command.
5. Always explain your reasoning briefly before a tool call."""

    app = GemmaApp(config, args, ctx, system_prompt)
    await app.run()

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
