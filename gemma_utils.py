import re
import os
import platform
import getpass
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
    """Reads skills from local and global directories, and looks for AGENTS.md in CWD.
    Returns: (full_text, list_of_skill_names)
    """
    skills_text = ""
    skill_names = []
    
    # 1. High Priority: AGENTS.md in current directory (Vercel standard)
    if os.path.exists("AGENTS.md"):
        with open("AGENTS.md", 'r', encoding='utf-8') as f:
            content = f.read()
            skills_text += f"\n\n--- PROJECT AGENT SPEC (AGENTS.md) ---\n{content}\n"
            skill_names.append("AGENTS.md")
            # Inject Retrieval-led reasoning instruction
            skills_text += "\nIMPORTANT: Always prioritize retrieval-led reasoning. Consult the documentation index above before relying on pre-trained knowledge.\n"

    # 2. Local project skills/ directory
    local_dir = "skills"
    if os.path.exists(local_dir) and os.path.isdir(local_dir):
        for filename in sorted(os.listdir(local_dir)):
            if filename.endswith(".md"):
                # Avoid double loading if AGENTS.md was moved to skills/
                if filename == "AGENTS.md" and "AGENTS.md" in skill_names:
                    continue
                with open(os.path.join(local_dir, filename), 'r', encoding='utf-8') as f:
                    skills_text += f"\n\n--- LOCAL SKILL: {filename} ---\n{f.read()}\n"
                    skill_names.append(f"local:{filename}")

    # 3. Global agent skills (~/.agent/skills/skills/)
    if config:
        active_globals = config.get('sandbox', {}).get('active_skills', [])
        global_base = os.path.expanduser("~/.agent/skills/skills")
        
        if active_globals and os.path.exists(global_base):
            for skill_id in active_globals:
                skill_path = os.path.join(global_base, skill_id, "SKILL.md")
                if os.path.exists(skill_path):
                    with open(skill_path, 'r', encoding='utf-8') as f:
                        skills_text += f"\n\n--- GLOBAL SKILL: {skill_id} ---\n{f.read()}\n"
                        skill_names.append(f"global:{skill_id}")
                        
    return skills_text, skill_names

def get_base_binary(command):
    """Extracts the first word/binary from a command string."""
    first_part = command.split('|')[0].split(';')[0].split('&&')[0].strip()
    return first_part.split()[0] if first_part else ""

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
        with open(config_path, 'w') as f:
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
        allowed_paths = [os.path.abspath(os.path.expanduser(p)) for p in raw_paths]
        paths_str = "\n".join([f'    (subpath "{p}")' for p in allowed_paths + [cwd]])
        
        if level == "strict":
            profile = f"(version 1)\n(deny default)\n(allow process-exec)\n(allow sysctl-read)\n(allow file-read* {paths_str})\n(allow file-write* {paths_str})\n(deny network*)\n(deny mach-lookup)"
        else:
            profile = f"(version 1)\n(allow default)\n(deny file-write* (subpath \"/\"))\n(allow file-write* {paths_str})"
        return f"sandbox-exec -p '{profile}' {command}", f"macOS {level}"

    return command, f"{system} (Unsandboxed)"
