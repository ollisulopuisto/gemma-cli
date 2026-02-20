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
        allowed_paths = sandbox_config.get('allowed_paths', [])
        paths_str = "\n".join([f'    (subpath "{p}")' for p in allowed_paths + [cwd]])
        
        if level == "strict":
            profile = f"""(version 1)
(deny default)
(allow process-exec)
(allow sysctl-read)
(allow file-read* {paths_str})
(allow file-write* {paths_str})
(deny network*)
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
