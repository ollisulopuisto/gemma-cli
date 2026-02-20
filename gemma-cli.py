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
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from gemma_utils import parse_tool_call, get_system_context, get_sandbox_command, get_base_binary, update_config_whitelist, get_skills_context

# Default Settings
DEFAULT_CONFIG_PATH = "config.yaml"
session_whitelist = set()

try:
    import readline
    import atexit
    readline.parse_and_bind("tab: complete")
    readline.set_completer_delims(' \t\n=')
    history_file = os.path.expanduser("~/.gemma_cli_history")
    if os.path.exists(history_file):
        readline.read_history_file(history_file)
    atexit.register(readline.write_history_file, history_file)
except ImportError:
    pass

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
    url = server.get('url')
    
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            start_time = time.perf_counter()
            response = requests.post(
                url, 
                json=payload, 
                auth=(auth.get('username'), auth.get('password')),
                timeout=60
            )
            end_time = time.perf_counter()
            duration = end_time - start_time
            response.raise_for_status()
            data = response.json()
            message = data['choices'][0]['message']
            return message.get('content', ''), message.get('reasoning_content', ''), duration
        except Exception as e:
            if attempt < max_retries:
                print(f"\033[91mConnection error, retrying ({attempt + 1}/{max_retries})...\033[0m")
                time.sleep(1)
                continue
            raise e

def run_command(command, sandbox_config, config_path, show_output=False, auto_approve=False):
    global session_whitelist
    full_command, sandbox_label = get_sandbox_command(command, sandbox_config)
    binary = get_base_binary(command)
    persistent_whitelist = sandbox_config.get('whitelist', [])
    label_color = "\033[93m" if "Sandbox" in sandbox_label else "\033[91m"
    print(f"{label_color}[Proposed Command ({sandbox_label}): {command}]\033[0m")
    approved = auto_approve or binary in session_whitelist or binary in persistent_whitelist
    if not approved:
        confirm = input(f"\033[91mDo you want to execute '{binary}'? (y/s/p/n): \033[0m").lower()
        if confirm == 's':
            session_whitelist.add(binary)
            approved = True
        elif confirm == 'p':
            update_config_whitelist(config_path, binary)
            session_whitelist.add(binary)
            approved = True
        elif confirm == 'y':
            approved = True
        else:
            return "Observation:\nUser denied execution for security reasons."

    start_time = time.perf_counter()
    try:
        result = subprocess.run(full_command, shell=True, capture_output=True, text=True, timeout=30)
        end_time = time.perf_counter()
        duration = end_time - start_time
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        print(f"\033[3mTool finished in {duration:.3f} seconds\033[0m")
        if show_output:
            print(f"\n--- TOOL OUTPUT ---\n{output}\n-------------------\n")
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent CLI - SKILLS ENABLED")
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
    skills_summary = ", ".join(skill_files) if skill_files else "None"
    
    def get_bottom_toolbar():
        cwd = os.getcwd()
        user = ctx['username']
        os_sys = ctx['os']
        return HTML(f'<b>[User:</b> {user} <b>| OS:</b> {os_sys} <b>| CWD:</b> {cwd} <b>| Sandbox:</b> {sb_summary} <b>| Skills:</b> {skills_summary}<b>]</b>')

    session = PromptSession(bottom_toolbar=get_bottom_toolbar)
    
    print(f"Gemma CLI Agent started (v2026.02.20). Sandbox Config: {sb_summary}. Skills loaded: {skills_summary}. Config: {args.config}. Type 'exit' to quit.")
    
    while True:
        try:
            user_input = session.prompt(HTML('<style color="cyan">User: </style>'))
            if not user_input.strip(): continue
            if user_input.lower() in ["exit", "quit"]: break
            messages.append({"role": "user", "content": user_input})
            while True:
                content, reasoning, gemma_duration = call_gemma(messages, config)
                if reasoning and args.show_reasoning:
                    print(f"\n\033[36mThought: {reasoning}\033[0m")
                print(f"\nGemma: {content}")
                print(f"\033[90m(Response time: {gemma_duration:.2f}s)\033[0m\n")
                messages.append({"role": "assistant", "content": content})
                cmd = parse_tool_call(content)
                if cmd:
                    observation = run_command(cmd, config['sandbox'], args.config, show_output=args.show_output, auto_approve=args.yes)
                    messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                    continue
                else: break
        except KeyboardInterrupt: break
        except EOFError: break
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    main()
