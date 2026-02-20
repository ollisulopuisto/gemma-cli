import pytest
from gemma_utils import parse_tool_call

def test_parse_tool_call_simple():
    content = "I will list files.\n```tool_code\nls -la\n```"
    assert parse_tool_call(content) == "ls -la"

def test_parse_tool_call_multiple():
    content = "First check then do.\n```tool_code\nls\n```\nAnd then:\n```tool_code\ncat README.md\n```"
    # Should only return the first one found
    assert parse_tool_call(content) == "ls"

def test_parse_tool_call_none():
    content = "No tool here!"
    assert parse_tool_call(content) is None
