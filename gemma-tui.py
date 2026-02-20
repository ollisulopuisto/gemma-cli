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
from rich.prompt import Prompt
from gemma_utils import parse_tool_call, get_system_context

# Default Settings
DEFAULT_CONFIG_PATH = "config.yaml"

def load_config(config_path):
    if not os.path.exists(config_path):
        return None
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def get_sandbox_profile(level, allowed_paths):
    cwd = os.getcwd()
    paths_str = "\n".join([f'    (subpath "{p}")' for p in allowed_paths + [cwd]])
    
    if level == "strict":
        return f"""(version 1)
(deny default)
(allow process-exec)
(allow sysctl-read)
(allow file-read* {paths_str})
(allow file-write* {paths_str})
(deny network*)
"""
    return f"""(version 1)
(allow default)
(deny file-write*
    (subpath "/")
)
(allow file-write*
{paths_str}
)
"""

def call_gemma(messages, config):
    server = config.get('server', {})
    payload = {
        "model": server.get('model'),
        "messages": messages,
        "temperature": 0.1
    }
    auth = server.get('auth', {})
    response = requests.post(
        server.get('url'), 
        json=payload, 
        auth=(auth.get('username'), auth.get('password'))
    )
    response.raise_for_status()
    data = response.json()
    message = data['choices'][0]['message']
    return message.get('content', ''), message.get('reasoning_content', '')

def run_command(command, sandbox_config, console, show_output=False):
    enabled = sandbox_config.get('enabled', True)
    level = sandbox_config.get('level', 'permissive')
    
    if enabled and level != "off":
        profile = get_sandbox_profile(level, sandbox_config.get('allowed_paths', []))
        console.print(Panel(f"[bold yellow]Executing ({level} Sandbox):[/bold yellow] {command}", border_style="yellow"))
        full_command = f"sandbox-exec -p '{profile}' {command}"
    else:
        console.print(Panel(f"[bold red]Executing (UNSANDBOXED):[/bold red] {command}", border_style="red"))
        full_command = command

    start_time = time.perf_counter()
    try:
        result = subprocess.run(full_command, shell=True, capture_output=True, text=True, timeout=30)
        end_time = time.perf_counter()
        duration = end_time - start_time
        
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        
        # Display timing info
        console.print(f"[dim italic]Command finished in {duration:.3f} seconds[/dim italic]")
        
        if show_output:
            console.print(Panel(output, title="Tool Output", border_style="dim"))
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent TUI")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml")
    parser.add_argument("--sandbox", choices=["off", "permissive", "strict"], help="Override sandbox level")
    parser.add_argument("--no-sandbox", action="store_true", help="Disable sandboxing")
    parser.add_argument("--show-output", action="store_true", help="Show tool output in the UI")
    parser.add_argument("--show-reasoning", action="store_true", help="Show model thinking/reasoning if available")
    args = parser.parse_args()

    config = load_config(args.config)
    if not config:
        print(f"Error: Config file not found at {args.config}.")
        sys.exit(1)

    if args.no_sandbox: config['sandbox']['enabled'] = False
    if args.sandbox:
        config['sandbox']['level'] = args.sandbox
        config['sandbox']['enabled'] = True

    console = Console()
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
    sandbox_status = f"[green]{config['sandbox']['level']}[/green]" if config['sandbox']['enabled'] else "[red]OFF[/red]"
    
    console.print(Panel.fit(
        f"[bold cyan]Gemma 3 Local Agent TUI (v2026.02.20)[/bold cyan]\n"
        f"Config: {args.config} | Sandbox: {sandbox_status} | Output: {'[green]ON[/green]' if args.show_output else '[red]OFF[/red]'} | Reasoning: {'[green]ON[/green]' if args.show_reasoning else '[red]OFF[/red]'}\n"
        "Type your request below. Type [bold red]'exit'[/bold red] or [bold red]'quit'[/bold red] to end the session.",
        border_style="cyan"
    ))
    
    while True:
        try:
            user_input = Prompt.ask("\n[bold blue]User[/bold blue]")
            if user_input.lower() in ["exit", "quit"]:
                break
            messages.append({"role": "user", "content": user_input})
            
            with console.status("[bold green]Gemma is thinking...", spinner="dots"):
                while True:
                    content, reasoning = call_gemma(messages, config)
                    
                    if reasoning and args.show_reasoning:
                        console.print("\n[italic dim cyan]Thought:[/italic dim cyan]")
                        console.print(Panel(reasoning, border_style="dim cyan"))

                    console.print("\n[bold magenta]Gemma[/bold magenta]")
                    console.print(Markdown(content))
                    messages.append({"role": "assistant", "content": content})
                    
                    cmd = parse_tool_call(content)
                    if cmd:
                        observation = run_command(cmd, config['sandbox'], console, show_output=args.show_output)
                        messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                        continue
                    else:
                        break
        except KeyboardInterrupt: break
        except EOFError: break
        except Exception as e: console.print(f"[bold red]Error:[/bold red] {e}")

if __name__ == "__main__":
    main()
