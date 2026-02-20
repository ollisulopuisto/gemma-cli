#!/usr/bin/env uv run
import requests
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
from rich.prompt import Confirm
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.styles import Style
from gemma_utils import parse_tool_call, get_system_context, get_sandbox_command, get_base_binary, update_config_whitelist

# Default Settings
DEFAULT_CONFIG_PATH = "config.yaml"
session_whitelist = set()

def load_config(config_path):
    if not os.path.exists(config_path):
        return None
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def call_gemma(messages, config):
    server = config.get('server', {})
    payload = {
        "model": server.get('model'),
        "messages": messages,
        "temperature": 0.1
    }
    auth = server.get('auth', {})
    
    start_time = time.perf_counter()
    response = requests.post(
        server.get('url'), 
        json=payload, 
        auth=(auth.get('username'), auth.get('password'))
    )
    end_time = time.perf_counter()
    duration = end_time - start_time
    
    response.raise_for_status()
    data = response.json()
    message = data['choices'][0]['message']
    return message.get('content', ''), message.get('reasoning_content', ''), duration

def run_command(command, sandbox_config, console, config_path, show_output=False, auto_approve=False):
    global session_whitelist
    full_command, sandbox_label = get_sandbox_command(command, sandbox_config)
    
    binary = get_base_binary(command)
    persistent_whitelist = sandbox_config.get('whitelist', [])
    
    label_style = "yellow" if "Sandbox" in sandbox_label else "red"
    console.print(Panel(f"[bold {label_style}]Proposed Command ({sandbox_label}):[/bold {label_style}]\n{command}", border_style=label_style))

    # Check Whitelists
    approved = auto_approve or binary in session_whitelist or binary in persistent_whitelist
    
    if not approved:
        choices = ["y", "s", "p", "n"]
        prompt_text = f"[bold red]Do you want to execute '{binary}'?[/bold red] (y/s/p/n): "
        console.print(prompt_text, end="")
        ans = input().lower().strip()
        
        if ans not in choices or ans == "n":
            return "Observation:\nUser denied execution for security reasons."
        elif ans == "s":
            session_whitelist.add(binary)
            approved = True
        elif ans == "p":
            update_config_whitelist(config_path, binary)
            session_whitelist.add(binary)
            approved = True
        else: # "y"
            approved = True

    start_time = time.perf_counter()
    try:
        result = subprocess.run(full_command, shell=True, capture_output=True, text=True, timeout=30)
        end_time = time.perf_counter()
        duration = end_time - start_time
        
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        console.print(f"[dim italic]Tool finished in {duration:.3f} seconds[/dim italic]")
        
        if show_output:
            console.print(Panel(output, title="Tool Output", border_style="dim"))
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent TUI - SECURITY ENHANCED")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml")
    parser.add_argument("--sandbox", choices=["off", "permissive", "strict"], help="Override sandbox level")
    parser.add_argument("--no-sandbox", action="store_true", help="Disable sandboxing")
    parser.add_argument("--allow-path", action="append", help="Allow access to this path")
    parser.add_argument("--show-output", action="store_true", help="Show tool output in the UI")
    parser.add_argument("--show-reasoning", action="store_true", help="Show model thinking/reasoning if available")
    parser.add_argument("--yes", action="store_true", help="AUTO-APPROVE ALL COMMANDS (DANGEROUS!)")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print(f"Error: Config file not found at {args.config}.")
        sys.exit(1)

    if args.no_sandbox: config['sandbox']['enabled'] = False
    if args.sandbox:
        config['sandbox']['level'] = args.sandbox
        config['sandbox']['enabled'] = True
    if args.allow_path:
        config['sandbox'].setdefault('allowed_paths', []).extend(args.allow_path)

    console = Console()
    if args.yes:
        console.print(Panel("[bold red]SECURITY WARNING: Auto-approve (--yes) is enabled. All tool commands will run without confirmation.[/bold red]", border_style="red"))

    ctx = get_system_context()
    
    system_prompt = f"""You are a senior CLI agent with direct access to the user's computer via shell commands.
Current Context (Sniffed from System):
- OS: {ctx['os']} ({ctx['os_release']})
- User: {ctx['username']}
- Directory: {ctx['cwd']}
- Time: {ctx['now']}
- Shell: {ctx['shell']}

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
    
    console.print(Panel.fit(
        f"[bold cyan]Gemma 3 Local Agent TUI (v2026.02.20)[/bold cyan]\n"
        f"Config: {args.config} | Sandbox Config: {sb_summary} | Output: {'[green]ON[/green]' if args.show_output else '[red]OFF[/red]'} | Reasoning: {'[green]ON[/green]' if args.show_reasoning else '[red]OFF[/red]'}\n"
        "Type your request below. Type [bold red]'exit'[/bold red] or [bold red]'quit'[/bold red] to end the session.",
        border_style="cyan"
    ))

    # Setup prompt_toolkit session
    history_file = os.path.expanduser("~/.gemma_history")
    session = PromptSession(
        history=FileHistory(history_file),
        completer=PathCompleter(expanduser=True),
        style=Style.from_dict({
            'prompt': '#00afff bold',
        })
    )
    
    while True:
        try:
            console.print("\n[bold blue]User[/bold blue]")
            user_input = session.prompt("> ")
            
            if not user_input: continue
            if user_input.lower() in ["exit", "quit"]:
                break
            messages.append({"role": "user", "content": user_input})
            
            while True:
                with console.status("[bold green]Gemma is thinking...", spinner="dots"):
                    content, reasoning, gemma_duration = call_gemma(messages, config)
                
                if reasoning and args.show_reasoning:
                    console.print("\n[italic dim cyan]Thought:[/italic dim cyan]")
                    console.print(Panel(reasoning, border_style="dim cyan"))

                console.print(f"\n[bold magenta]Gemma[/bold magenta]")
                console.print(Markdown(content))
                console.print(f"[dim italic]Response time: {gemma_duration:.2f}s[/dim italic]")
                messages.append({"role": "assistant", "content": content})
                
                cmd = parse_tool_call(content)
                if cmd:
                    observation = run_command(cmd, config['sandbox'], console, args.config, show_output=args.show_output, auto_approve=args.yes)
                    messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                    continue
                else:
                    break
        except KeyboardInterrupt: continue # Keep session alive on Ctrl+C
        except EOFError: break # Exit on Ctrl+D
        except Exception as e: console.print(f"[bold red]Error:[/bold red] {e}")

if __name__ == "__main__":
    main()
