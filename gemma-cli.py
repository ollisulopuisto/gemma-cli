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
from datetime import datetime
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.patch_stdout import patch_stdout
from gemma_utils import parse_tool_call, get_system_context, get_sandbox_command, get_base_binary, get_all_binaries, update_config_whitelist, get_skills_context

# Default Settings
DEFAULT_CONFIG_PATH = "config.yaml"
session_whitelist = set()

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
                print(f"\033[91mConnection error, retrying ({attempt + 1}/{max_retries})...\033[0m")
                await asyncio.sleep(1)
                continue
            raise e

async def run_command_async(command, sandbox_config, config_path, show_output=False, auto_approve=False):
    global session_whitelist
    full_command, sandbox_label = get_sandbox_command(command, sandbox_config)
    binaries = get_all_binaries(command)
    persistent_whitelist = sandbox_config.get('whitelist', [])
    label_color = "\033[93m" if "Sandbox" in sandbox_label else "\033[91m"
    print(f"{label_color}[Proposed Command ({sandbox_label}): {command}]\033[0m")
    
    for binary in binaries:
        approved = auto_approve or binary in session_whitelist or binary in persistent_whitelist
        if not approved:
            # We use standard input for confirmation as prompt_toolkit doesn't support nested input easily
            confirm = input(f"\033[91mDo you want to execute '{binary}'? (y/s/p/n): \033[0m").lower()
            if confirm == 's':
                session_whitelist.add(binary)
            elif confirm == 'p':
                update_config_whitelist(config_path, binary)
                session_whitelist.add(binary)
            elif confirm == 'y':
                pass # Approved for this one call
            else:
                return f"Observation:\nUser denied execution for '{binary}'."

    start_time = time.perf_counter()
    try:
        # Run asynchronously
        process = await asyncio.create_subprocess_shell(
            full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        end_time = time.perf_counter()
        duration = end_time - start_time
        output = f"STDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}"
        print(f"\033[3mTool finished in {duration:.3f} seconds\033[0m")
        if show_output:
            print(f"\n--- TOOL OUTPUT ---\n{output}\n-------------------\n")
        return output
    except Exception as e:
        return f"Error executing command: {str(e)}"

async def main():
    parser = argparse.ArgumentParser(description="Gemma 3 Local Agent CLI - ASYNC & PERSISTENT STATUS")
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
    skills_summary = ", ".join(skill_files) if skill_files else "None"
    
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
    is_thinking = False
    
    def get_bottom_toolbar():
        cwd = os.getcwd()
        user = ctx['username']
        os_sys = ctx['os']
        status = " [THINKING...]" if is_thinking else " [IDLE]"
        status_color = "red" if is_thinking else "green"
        return HTML(f'<b>[User:</b> {user} <b>| OS:</b> {os_sys} <b>| CWD:</b> {cwd} <b>| Sandbox:</b> {sb_summary} <b>| Status:</b> <style color="{status_color}">{status}</style><b>]</b>')

    session = PromptSession(bottom_toolbar=get_bottom_toolbar)
    print(f"Gemma CLI Agent started (v2026.02.20). Sandbox Config: {sb_summary}. Skills loaded: {skills_summary}. Config: {args.config}. Type 'exit' to quit.")
    
    while True:
        try:
            # patch_stdout ensures the toolbar stays at the bottom while things print
            with patch_stdout():
                user_input = await session.prompt_async(HTML('<style color="cyan">User: </style>'))
            
            if not user_input.strip(): continue
            if user_input.lower() in ["exit", "quit"]:
                print("\n\033[92mGoodbye! Happy hacking! 💎\033[0m")
                break
            
            messages.append({"role": "user", "content": user_input})
            while True:
                is_thinking = True
                with patch_stdout():
                    content, reasoning, gemma_duration = await call_gemma_async(messages, config)
                is_thinking = False
                
                if reasoning and args.show_reasoning:
                    print(f"\n\033[36mThought: {reasoning}\033[0m")
                
                print(f"\nGemma: {content}")
                print(f"\033[90m(Response time: {gemma_duration:.2f}s)\033[0m\n")
                
                messages.append({"role": "assistant", "content": content})
                cmd = parse_tool_call(content)
                if cmd:
                    with patch_stdout():
                        observation = await run_command_async(cmd, config['sandbox'], args.config, show_output=args.show_output, auto_approve=args.yes)
                    messages.append({"role": "user", "content": f"Observation:\n{observation}"})
                    continue
                else: break
        except KeyboardInterrupt:
            print("\n\033[93mSession interrupted. Goodbye!\033[0m")
            break
        except EOFError:
            print("\n\033[92mGoodbye! (EOF detected)\033[0m")
            break
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
