import re
import os
import platform
from datetime import datetime

def parse_tool_call(content):
    """Parses a tool_code block from the model's response."""
    match = re.search(r"```tool_code\n(.*?)\n```", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def get_system_context():
    """Gathers environmental context for the system prompt."""
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "username": os.getlogin(),
        "cwd": os.getcwd(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "shell": os.environ.get("SHELL", "unknown")
    }
