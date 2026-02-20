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

def run_command(command, sandbox_config, show_output=False):
    enabled = sandbox_config.get('enabled', True)
    level = sandbox_config.get('level', 'permissive')
    
    if enabled and level != "off":
        profile = get_sandbox_profile(level, sandbox_config.get('allowed_paths', []))
        print(f"\033[93m[Executing ({level} Sandbox): {command}]\033[0m")
        full_command = f"sandbox-exec -p '{profile}' {command}"
    else:
        print(f"\033[91m[Executing (UNSANDBOXED): {command}]\033[0m")
        full_command = command

    start_time = time.perf_counter()
    try:
        result = subprocess.run(full_command, shell=True, capture_output=True, text=True, timeout=30)
        end_time = time.perf_counter()
        duration = end_time - start_time
        
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        
        print(f"\033[3mCommand finished in {duration:.3f} seconds\033[0m")
        
        if show_output:
            print(f"\n--- TOOL OUTPUT ---\n{output}\n-------------------\n")
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent CLI")
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
    sandbox_status = f"{config['sandbox']['level']}" if config['sandbox']['enabled'] else "OFF"
    print(f"Gemma CLI Agent started (v2026.02.20). Sandbox: {sandbox_status}. Config: {args.config}. Type 'exit' to quit.")
    
    while True:
        try:
            user_input = input("\033[94mUser: \033[0m")
            if user_input.lower() in ["exit", "quit"]: break
            messages.append({"role": "user", "content": user_input})
            
            while True:
                content, reasoning = call_gemma(messages, config)
                
                if reasoning and args.show_reasoning:
                    print(f"\n\033[36mThought: {reasoning}\033[0m")

                print(f"\nGemma: {content}\n")
                messages.append({"role": "assistant", "content": content})
                
                cmd = parse_tool_call(content)
                if cmd:
                    observation = run_command(cmd, config['sandbox'], show_output=args.show_output)
                    messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                    continue
                else:
                    break
        except KeyboardInterrupt: break
        except EOFError: break
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    main()
