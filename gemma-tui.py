#!/usr/bin/env uv run
import httpx
import asyncio
import json
import subprocess
import re
import sys
import os
import yaml
import argparse
import time
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completer, PathCompleter, ThreadedCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from gemma_utils import parse_tool_call, get_system_context, get_sandbox_command, get_base_binary, get_all_binaries, update_config_whitelist, get_skills_context, save_config

# Default Settings
DEFAULT_CONFIG_PATH = "config.yaml"
session_whitelist = set()

class WordPathCompleter(Completer):
    """Wraps PathCompleter to only complete the current word under the cursor."""
    def __init__(self, **kwargs):
        self.path_completer = PathCompleter(**kwargs)

    def get_completions(self, document, complete_event):
        word_before_cursor = document.get_word_before_cursor()
        if word_before_cursor.startswith('.') or word_before_cursor.startswith('~') or '/' in word_before_cursor:
            fake_doc = Document(word_before_cursor, cursor_position=len(word_before_cursor))
            yield from self.path_completer.get_completions(fake_doc, complete_event)

def load_config(config_path):
    if not os.path.exists(config_path):
        return None
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def call_gemma_async(messages, config):
    server = config.get('server', {})
    payload = {
        "model": server.get('model'),
        "messages": messages,
        "temperature": 0.1
    }
    auth = server.get('auth', {})
    url = server.get('url')
    
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            start_time = time.perf_counter()
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url, 
                    json=payload, 
                    auth=(auth.get('username'), auth.get('password')),
                    timeout=60.0
                )
            end_time = time.perf_counter()
            duration = end_time - start_time
            response.raise_for_status()
            data = response.json()
            message = data['choices'][0]['message']
            return message.get('content', ''), message.get('reasoning_content', ''), duration
        except Exception as e:
            if attempt < max_retries:
                # Use patch_stdout to print during retry
                print(f"\033[91mConnection error, retrying ({attempt + 1}/{max_retries})...\033[0m")
                await asyncio.sleep(1)
                continue
            raise e

async def run_command_async(command, sandbox_config, console, config_path, show_output=False, auto_approve=False):
    global session_whitelist
    full_command, sandbox_label = get_sandbox_command(command, sandbox_config)
    binaries = get_all_binaries(command)
    persistent_whitelist = sandbox_config.get('whitelist', [])
    label_style = "yellow" if "Sandbox" in sandbox_label else "red"
    console.print(Panel(f"[bold {label_style}]Proposed Command ({sandbox_label}):[/bold {label_style}]\n{command}", border_style=label_style))
    
    for binary in binaries:
        approved = auto_approve or binary in session_whitelist or binary in persistent_whitelist
        if not approved:
            choices = ["y", "s", "p", "n"]
            prompt_text = f"[bold red]Do you want to execute '{binary}'?[/bold red] (y/s/p/n): "
            console.print(prompt_text, end="")
            # Note: We still use standard input for confirmation to keep things simple
            ans = input().lower().strip()
            if ans not in choices or ans == "n":
                return f"Observation:\nUser denied execution for '{binary}'."
            elif ans == "s":
                session_whitelist.add(binary)
            elif ans == "p":
                update_config_whitelist(config_path, binary)
                session_whitelist.add(binary)
            else: # "y"
                pass

    start_time = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_shell(
            full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        end_time = time.perf_counter()
        duration = end_time - start_time
        output = f"STDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
        console.print(f"[dim italic]Tool finished in {duration:.3f} seconds[/dim italic]")
        if show_output:
            console.print(Panel(output, title="Tool Output", border_style="dim"))
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

def handle_configure(config, config_path, console):
    """Interactive configuration helper."""
    table = Table(title="Gemma CLI Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="magenta")
    
    sb = config.get('sandbox', {})
    table.add_row("Sandbox Enabled", str(sb.get('enabled')))
    table.add_row("Sandbox Level", sb.get('level'))
    table.add_row("Active Skills", ", ".join(sb.get('active_skills', [])))
    
    console.print(table)
    
    if Confirm.ask("Do you want to change settings?"):
        new_level = Prompt.ask("Sandbox Level", choices=["off", "permissive", "strict"], default=sb.get('level'))
        config['sandbox']['level'] = new_level
        config['sandbox']['enabled'] = (new_level != "off")
        save_config(config_path, config)
        console.print("[green]Configuration saved![/green]")

async def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent TUI - ASYNC")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml")
    parser.add_argument("--sandbox", choices=["off", "permissive", "strict"], help="Override sandbox level")
    parser.add_argument("--no-sandbox", action="store_true", help="Disable sandboxing")
    parser.add_argument("--allow-path", action="append", help="Allow access to this path")
    parser.add_argument("--skill", action="append", help="Activate a global skill (id)")
    parser.add_argument("--show-output", action="store_true", help="Show tool output in the UI")
    parser.add_argument("--show-reasoning", action="store_true", help="Show model thinking/reasoning if available")
    parser.add_argument("--yes", action="store_true", help="AUTO-APPROVE ALL COMMANDS (DANGEROUS!)")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print(f"Error: Config file not found at {args.config}.")
        sys.exit(1)

    # Overrides
    if args.no_sandbox: config['sandbox']['enabled'] = False
    if args.sandbox:
        config['sandbox']['level'] = args.sandbox
        config['sandbox']['enabled'] = (args.sandbox != "off")
    if args.allow_path:
        config['sandbox'].setdefault('allowed_paths', []).extend(args.allow_path)
    if args.skill:
        config['sandbox'].setdefault('active_skills', []).extend(args.skill)

    console = Console()
    if args.yes:
        console.print(Panel("[bold red]SECURITY WARNING: Auto-approve (--yes) is enabled.[/bold red]", border_style="red"))

    ctx = get_system_context()
    skills_text, skill_files = get_skills_context(config)
    skills_label = ", ".join(skill_files) if skill_files else "None"
    
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

    messages = [{"role": "system", "content": system_prompt}]
    sb_config = config['sandbox']
    sb_summary = sb_config['level'] if sb_config['enabled'] else "OFF"
    
    def get_bottom_toolbar():
        cwd = os.getcwd()
        user = ctx['username']
        os_sys = ctx['os']
        return HTML(f'<b>[User:</b> {user} <b>| OS:</b> {os_sys} <b>| CWD:</b> {cwd} <b>| Sandbox:</b> {sb_summary} <b>| Skills:</b> {skills_label}<b>]</b>')

    console.print(Panel.fit(
        f"[bold cyan]Gemma 3 Local Agent TUI (v2026.02.20)[/bold cyan]\n"
        f"Config: {args.config} | Sandbox Config: {sb_summary}\n"
        f"Skills loaded: [green]{skills_label}[/green]\n"
        f"Output: {'[green]ON[/green]' if args.show_output else '[red]OFF[/red]'} | Reasoning: {'[green]ON[/green]' if args.show_reasoning else '[red]OFF[/red]'}\n"
        "Type your request below. Commands: /configure, exit, quit",
        border_style="cyan"
    ))

    history_file = os.path.expanduser("~/.gemma_history")
    session = PromptSession(
        history=FileHistory(history_file),
        completer=ThreadedCompleter(WordPathCompleter(expanduser=True)),
        complete_while_typing=True,
        bottom_toolbar=get_bottom_toolbar,
        style=Style.from_dict({'prompt': '#00afff bold', 'completion-menu.completion': 'bg:#008888 #ffffff', 'completion-menu.completion.current': 'bg:#00aaaa #000000',})
    )
    
    while True:
        try:
            with patch_stdout():
                user_input = await session.prompt_async(HTML('<style color="cyan">User: </style>'))
            
            if not user_input: continue
            
            # Internal Commands
            if user_input.strip().lower() == "/configure":
                handle_configure(config, args.config, console)
                continue
            
            if user_input.lower() in ["exit", "quit"]:
                console.print("\n[bold green]Goodbye! Happy hacking! 💎[/bold green]")
                break
            
            messages.append({"role": "user", "content": user_input})
            while True:
                with patch_stdout():
                    with console.status("[bold green]Gemma is thinking...", spinner="dots"):
                        content, reasoning, gemma_duration = await call_gemma_async(messages, config)
                
                if reasoning and args.show_reasoning:
                    console.print("\n[italic dim cyan]Thought:[/italic dim cyan]")
                    console.print(Panel(reasoning, border_style="dim cyan"))
                
                console.print(f"\n[bold magenta]Gemma[/bold magenta]")
                console.print(Markdown(content))
                console.print(f"[dim italic]Response time: {gemma_duration:.2f}s[/dim italic]")
                
                messages.append({"role": "assistant", "content": content})
                cmd = parse_tool_call(content)
                if cmd:
                    with patch_stdout():
                        observation = await run_command_async(cmd, config['sandbox'], console, args.config, show_output=args.show_output, auto_approve=args.yes)
                    messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                    continue
                else: break
        except KeyboardInterrupt: 
            console.print("\n[bold yellow]Session interrupted. Goodbye![/bold yellow]")
            break
        except EOFError: 
            console.print("\n[bold green]Goodbye! (EOF detected)[/bold green]")
            break
        except Exception as e: console.print(f"[bold red]Error:[/bold red] {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
