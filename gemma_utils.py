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

def get_base_binary(command):
    """Extracts the first word/binary from a command string, ignoring pipes etc."""
    # Handle simple cases, might need more robust parsing for complex chains
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
        # macOS Seatbelt
        cwd = os.getcwd()
        raw_paths = sandbox_config.get('allowed_paths', [])
        # Expand ~ and make absolute
        allowed_paths = [os.path.abspath(os.path.expanduser(p)) for p in raw_paths]
        paths_str = "\n".join([f'    (subpath "{p}")' for p in allowed_paths + [cwd]])
        
        if level == "strict":
            profile = f"""(version 1)
(deny default)
(allow process-exec)
(allow sysctl-read)
(allow file-read* {paths_str})
(allow file-write* {paths_str})
(deny network*)
(deny mach-lookup)
"""
        else:
            profile = f"""(version 1)
(allow default)
(deny file-write*
    (subpath "/")
)
(allow file-write*
{paths_str}
)
"""
        return f"sandbox-exec -p '{profile}' {command}", f"macOS {level}"

    elif system == "Linux":
        # Check for bubblewrap (common on Linux)
        import shutil
        if shutil.which("bwrap"):
            # Simple bubblewrap implementation
            # --dev-bind / /: bind-mount host root to sandbox root
            # --tmpfs /tmp: create a fresh tmpfs for /tmp (restricting writes if not bound)
            # This is complex to get right, so we'll just return a warning for now
            # or a very basic sandbox.
            pass
            
    # Fallback for Windows and Linux without bwrap
    return command, f"{system} (Unsandboxed)"
