import pytest
import os
from gemma_utils import (
    parse_tool_call, get_all_binaries, get_skills_context, 
    edit_file, get_base_binary
)

# --- Parser Tests ---

def test_parse_tool_call_simple():
    content = "I will list files.\n```tool_code\nls -la\n```"
    assert parse_tool_call(content) == "ls -la"

def test_parse_tool_call_multiple():
    content = "First check then do.\n```tool_code\nls\n```\nAnd then:\n```tool_code\ncat README.md\n```"
    assert parse_tool_call(content) == "ls"

def test_parse_tool_call_none():
    content = "No tool here!"
    assert parse_tool_call(content) is None

# --- Security & Binary Extraction Tests ---

def test_get_base_binary():
    assert get_base_binary("ls -la") == "ls"
    assert get_base_binary("  /usr/bin/python3 script.py") == "/usr/bin/python3"
    assert get_base_binary("") == ""

def test_get_all_binaries_piped():
    cmd = "find . -name '*.py' | xargs grep 'import' | sort -u"
    binaries = get_all_binaries(cmd)
    assert binaries == ["find", "xargs", "sort"]

def test_get_all_binaries_chained():
    cmd = "mkdir tmp && cd tmp && touch file.txt || echo 'failed'"
    binaries = get_all_binaries(cmd)
    assert binaries == ["mkdir", "cd", "touch", "echo"]

def test_get_all_binaries_complex():
    cmd = "ls; (cd .. && pwd) && echo done | cat"
    # Note: parentheses might be tricky, let's see how our current regex handles them
    binaries = get_all_binaries(cmd)
    assert "ls" in binaries
    assert "cd" in binaries
    assert "pwd" in binaries
    assert "echo" in binaries
    assert "cat" in binaries

# --- Skill & Context Tests ---

def test_get_skills_context_priority(tmp_path, monkeypatch):
    # Mock current directory to test priority file loading
    monkeypatch.chdir(tmp_path)
    
    (tmp_path / "IDENTITY.md").write_text("I am Gemma", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("I am helpful", encoding="utf-8")
    
    text, names = get_skills_context()
    assert "IDENTITY.md" in names
    assert "SOUL.md" in names
    assert "--- PROJECT IDENTITY ---" in text
    assert "I am Gemma" in text

# --- Tool Logic Tests ---

def test_edit_file_logic(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("line1\nline2\nline3", encoding="utf-8")
    
    # Successful edit
    res = edit_file(str(test_file), "line2", "modified_line")
    assert "Successfully edited" in res
    assert test_file.read_text(encoding="utf-8") == "line1\nmodified_line\nline3"
    
    # Missing text
    res = edit_file(str(test_file), "nonexistent", "new")
    assert "Error: old_text not found" in res
    
    # Non-unique text
    test_file.write_text("duplicate\ndata\nduplicate", encoding="utf-8")
# --- Async Logic Tests ---

@pytest.mark.asyncio
async def test_call_gemma_async_unpacking(mocker):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("gemma_cli", "gemma-cli.py")
    gemma_cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gemma_cli)
    
    mock_response = mocker.Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello", "reasoning_content": "Thinking"}}]
    }
    mock_response.raise_for_status = mocker.Mock()
    
    # Mock httpx.AsyncClient.post
    mocker.patch("httpx.AsyncClient.post", return_value=mock_response)
    
    config = {"server": {"url": "http://test", "model": "test", "auth": {}}}
    
    content, reasoning, duration = await gemma_cli.call_gemma_async([], config)
    
    assert content == "Hello"
    assert reasoning == "Thinking"
    assert duration >= 0
