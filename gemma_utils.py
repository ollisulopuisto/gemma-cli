import re
import os
import platform
import getpass
import sys
import subprocess
from datetime import datetime

def parse_tool_call(content):
    """Parses a tool_code block from the model's response."""
    match = re.search(r"```tool_code\n(.*?)\n```", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def get_system_context():
    """Gathers environmental context for the system prompt."""
    try:
        username = os.getlogin()
    except Exception:
        username = getpass.getuser()
        
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "username": username,
        "cwd": os.getcwd(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "shell": os.environ.get("SHELL", "cmd.exe" if platform.system() == "Windows" else "/bin/sh")
    }

def get_skills_context(config=None):
    """Reads skills from local and global directories, and looks for priority files in CWD.
    Returns: (full_text, list_of_skill_names)
    """
    skills_text = ""
    skill_names = []
    
    # 1. High Priority: IDENTITY.md, SOUL.md, and AGENTS.md in current directory
    priority_files = ["IDENTITY.md", "SOUL.md", "AGENTS.md"]
    for filename in priority_files:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                content = f.read()
                skills_text += f"\n\n--- PROJECT {filename.replace('.md', '').upper()} ---\n{content}\n"
                skill_names.append(filename)
                if filename == "AGENTS.md":
                    # Inject Retrieval-led reasoning instruction
                    skills_text += "\nIMPORTANT: Always prioritize retrieval-led reasoning. Consult the documentation index above before relying on pre-trained knowledge.\n"

    # 2. Local project skills/ directory
    local_dir = "skills"
    if os.path.exists(local_dir) and os.path.isdir(local_dir):
        for filename in sorted(os.listdir(local_dir)):
            if filename.endswith(".md"):
                # Avoid double loading if priority files were also in skills/
                if filename in priority_files and filename in skill_names:
                    continue
                with open(os.path.join(local_dir, filename), 'r', encoding='utf-8') as f:
                    skills_text += f"\n\n--- LOCAL SKILL: {filename} ---\n{f.read()}\n"
                    skill_names.append(f"local:{filename}")

    # 3. Global agent skills (~/.agent/skills/)
    if config:
        active_globals = config.get('sandbox', {}).get('active_skills', [])
        global_base = os.path.expanduser("~/.agent/skills")
        
        if active_globals and os.path.exists(global_base):
            for skill_id in active_globals:
                skill_path = os.path.join(global_base, skill_id, "SKILL.md")
                if os.path.exists(skill_path):
                    with open(skill_path, 'r', encoding='utf-8') as f:
                        skills_text += f"\n\n--- GLOBAL SKILL: {skill_id} ---\n{f.read()}\n"
                        skill_names.append(f"global:{skill_id}")
                        
    return skills_text, skill_names

def edit_file(path, old_text, new_text):
    """Smarter file editing by replacing a specific block of text."""
    if not os.path.exists(path):
        return f"Error: File not found: {path}"
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if old_text not in content:
            return "Error: old_text not found in file. Match must be exact, including whitespace."
        
        count = content.count(old_text)
        if count > 1:
            return f"Error: old_text appears {count} times. Provide more context to make the match unique."
        
        new_content = content.replace(old_text, new_text, 1)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return f"Successfully edited {path}"
    except Exception as e:
        return f"Error editing file: {str(e)}"

def run_subagent(objective, config_path="config.yaml"):
    """Spawns a sub-agent to handle a specific sub-task."""
    # This calls gemma-cli.py with the objective
    cmd = [sys.executable, "gemma-cli.py", "--config", config_path, "--yes"]
    try:
        # We use a non-interactive pipe for the objective
        process = subprocess.run(cmd, input=objective, text=True, capture_output=True, timeout=300)
        return f"Sub-agent output:\n{process.stdout}\n{process.stderr}"
    except Exception as e:
        return f"Sub-agent failed: {str(e)}"

def get_base_binary(command):
    """Extracts the first word/binary from a command string."""
    if not command: return ""
    # Remove leading spaces and take the first word
    return command.strip().split()[0]

def get_all_binaries(command):
    """Extracts all potential binaries from a piped/chained command string."""
    if not command: return []
    # Split by common shell separators: |, ;, &, &&, ||
    # We use a regex to handle both single and double characters
    parts = re.split(r'[|;&]|\&\&|\|\|', command)
    binaries = []
    for part in parts:
        part = part.strip()
        if not part: continue
        binary = get_base_binary(part)
        if binary:
            binaries.append(binary)
    # Return unique binaries while preserving order of appearance
    seen = set()
    return [x for x in binaries if not (x in seen or seen.add(x))]

def update_config_whitelist(config_path, binary):
    """Adds a binary to the persistent whitelist in config.yaml."""
    import yaml
    if not os.path.exists(config_path):
        return
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    whitelist = config.setdefault('sandbox', {}).setdefault('whitelist', [])
    if binary not in whitelist:
        whitelist.append(binary)
        save_config(config_path, config)

def save_config(config_path, config):
    """Saves the configuration object back to YAML."""
    import yaml
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, default_flow_style=False)

def get_sandbox_command(command, sandbox_config):
    """Returns the command wrapped in an OS-specific sandbox if possible."""
    if not sandbox_config.get('enabled', True):
        return command, "OFF"
        
    level = sandbox_config.get('level', 'permissive')
    if level == "off":
        return command, "OFF"
        
    system = platform.system()
    if system == "Darwin":
        cwd = os.getcwd()
        raw_paths = sandbox_config.get('allowed_paths', [])
        # Normalize and filter paths to avoid accidentally allowing root
        allowed_paths = [os.path.abspath(os.path.expanduser(p)) for p in raw_paths]
        all_allowed = [p for p in allowed_paths + [cwd] if p != "/"]
        paths_str = "\n".join([f'    (subpath "{p}")' for p in all_allowed])
        
        if level == "strict":
            profile = f"""(version 1)
(deny default)
(allow process-exec)
(allow sysctl-read)
(allow file-read* (subpath "/usr/lib"))
(allow file-read* (subpath "/usr/share"))
(allow file-read* (subpath "/lib"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/bin"))
(allow file-read* (subpath "/usr/bin"))
(allow file-read* {paths_str})
(allow file-write* {paths_str})
(deny network*)
(deny mach-lookup)"""
        else:
            # Permissive: Inherit user's read permissions (allow default), 
            # but strictly deny writing everywhere except specified paths.
            profile = f"""(version 1)
(allow default)
(deny file-write* (subpath "/"))
(allow file-write* {paths_str})"""
        
        # Clean up the profile string to remove extra newlines/whitespace
        profile = "\n".join([line.strip() for line in profile.strip().split("\n")])
        return f"sandbox-exec -p '{profile}' {command}", f"macOS {level}"

    return command, f"{system} (Unsandboxed)"

def notify(message, webhook_url=None):
    """Sends a notification to a webhook (Slack/Discord compatible)."""
    if not webhook_url:
        webhook_url = os.environ.get("GEMMA_NOTIFY_WEBHOOK")
    
    if not webhook_url:
        return "Error: No webhook URL provided and GEMMA_NOTIFY_WEBHOOK not set."
    
    import requests
    try:
        payload = {"text": f"Gemma CLI Alert: {message}"}
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        return "Notification sent successfully."
    except Exception as e:
        return f"Error sending notification: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)
    
    cmd = sys.argv[1]
    if cmd == "edit" and len(sys.argv) >= 5:
        path = sys.argv[2]
        with open(sys.argv[3], 'r') as f: old = f.read()
        with open(sys.argv[4], 'r') as f: new = f.read()
        print(edit_file(path, old, new))
    elif cmd == "subagent" and len(sys.argv) >= 3:
        print(run_subagent(sys.argv[2]))
    elif cmd == "notify" and len(sys.argv) >= 3:
        print(notify(sys.argv[2]))
