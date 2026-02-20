#!/usr/bin/env uv run
import httpx
import asyncio
import json
import os
import yaml
import argparse
import sys
from datetime import datetime

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.widgets import TextArea, Frame
from prompt_toolkit.styles import Style

from gemma_utils import (
    parse_tool_call, get_system_context, get_sandbox_command, 
    get_all_binaries, update_config_whitelist, get_skills_context
)

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
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, json=payload, 
            auth=(auth.get('username'), auth.get('password')),
            timeout=120.0
        )
        response.raise_for_status()
        data = response.json()
        message = data['choices'][0]['message']
        return message.get('content', ''), message.get('reasoning_content', '')

async def run_command_async(command, sandbox_config, config_path, log_func, auto_approve=False):
    full_command, sandbox_label = get_sandbox_command(command, sandbox_config)
    # Note: In this TUI mode, we assume user either auto-approves or we'd need a complex modal.
    # For now, we'll log the command and execute it if permissive.
    log_func(f"\n[STDOUT] Executing: {command} ({sandbox_label})\n")
    
    process = await asyncio.create_subprocess_shell(
        full_command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    output = f"STDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
    return output

# --- TUI Application ---

class GemmaApp:
    def __init__(self, config, args, ctx, skills_summary, skills_text):
        self.config = config
        self.args = args
        self.ctx = ctx
        self.skills_summary = skills_summary
        self.is_thinking = False
        self.messages = [{"role": "system", "content": skills_text}] # Simplified for brevity
        
        # UI Components
        self.output_field = TextArea(read_only=True, scrollbar=True, focusable=True)
        self.input_field = TextArea(height=1, prompt="User: ", multiline=False, focusable=True)
        self.sb_summary = config['sandbox']['level'] if config['sandbox']['enabled'] else "OFF"
        
        # Status Bar
        self.status_bar = Window(
            content=FormattedTextControl(self.get_status_text),
            height=1,
            style="reverse"
        )
        
        # Layout
        self.layout = Layout(
            HSplit([
                Frame(self.output_field, title="Gemma CLI Chat Log"),
                self.input_field,
                self.status_bar,
            ]),
            focused_element=self.input_field
        )
        
        # Key Bindings
        self.kb = KeyBindings()
        
        @self.kb.add("tab")
        def _(event):
            event.app.layout.focus_next()

        @self.kb.add("c-c")
        @self.kb.add("c-q")
        def _(event):
            event.app.exit()

        @self.kb.add("enter")
        def _(event):
            content = self.input_field.text
            if content:
                self.input_field.text = ""
                asyncio.create_task(self.handle_input(content))

        self.app = Application(
            layout=self.layout,
            key_bindings=self.kb,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict({
                'frame.label': 'cyan bold',
                'status': 'reverse',
            })
        )

    def get_status_text(self):
        status = "THINKING..." if self.is_thinking else "IDLE"
        color = "red" if self.is_thinking else "green"
        return HTML(
            f" <b>User:</b> {self.ctx['username']} | "
            f"<b>CWD:</b> {os.getcwd()} | "
            f"<b>Sandbox:</b> {self.sb_summary} | "
            f"<b>Status:</b> <{color}>{status}</{color}> "
        )

    def log(self, text):
        self.output_field.text += text + "\n"
        # Auto-scroll
        self.output_field.buffer.cursor_position = len(self.output_field.text)

    async def handle_input(self, text):
        if text.lower() in ["exit", "quit"]:
            self.app.exit()
            return

        self.log(f"\nUser: {text}")
        self.messages.append({"role": "user", "content": text})
        
        while True:
            self.is_thinking = True
            self.app.invalidate()
            try:
                content, reasoning = await call_gemma_async(self.messages, self.config)
                self.is_thinking = False
                
                if reasoning and self.args.show_reasoning:
                    self.log(f"\n[Reasoning]\n{reasoning}")
                
                self.log(f"\nGemma: {content}")
                self.messages.append({"role": "assistant", "content": content})
                
                cmd = parse_tool_call(content)
                if cmd:
                    # In this version, we auto-approve for brevity or safety check here
                    obs = await run_command_async(
                        cmd, self.config['sandbox'], self.args.config, self.log, 
                        auto_approve=self.args.yes
                    )
                    self.messages.append({"role": "user", "content": f"Observation:\n{obs}"})
                    if self.args.show_output:
                        self.log(f"\n[Tool Output]\n{obs}")
                    continue
                else:
                    break
            except Exception as e:
                self.is_thinking = False
                self.log(f"\n[Error] {str(e)}")
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
    skills_summary = ", ".join(skill_files) if skill_files else "None"

    app = GemmaApp(config, args, ctx, skills_summary, skills_text)
    await app.run()

if __name__ == "__main__":
    asyncio.run(main_async())
