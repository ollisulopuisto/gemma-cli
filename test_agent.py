import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

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

def test_call_gemma_async_unpacking(mocker):
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
    
    content, reasoning, duration = asyncio.run(gemma_cli.call_gemma_async([], config))
    
    assert content == "Hello"
    assert reasoning == "Thinking"
    assert duration >= 0

# --- Real HTTP client lifecycle regression tests (both entry points) ---



@pytest.fixture(params=[("gemma-cli.py", "GemmaApp"), ("gemma-tui.py", "GemmaTUI")])
def entry_point(request):
    filename, class_name = request.param
    spec = importlib.util.spec_from_file_location(class_name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, getattr(module, class_name)


@pytest.fixture
def model_server():
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self):
            self.connections = 0
            self.closed_connections = 0
            self.requests = []
            self.status = 200
            super().__init__(("127.0.0.1", 0), Handler)

        def get_request(self):
            sock, address = super().get_request()
            self.connections += 1
            return sock, address

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.server.requests.append(json.loads(body))
            data = json.dumps({"choices": [{"message": {
                "content": "Hello", "reasoning_content": "Thinking"
            }}]}).encode()
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def finish(self):
            try:
                super().finish()
            finally:
                self.server.closed_connections += 1

        def log_message(self, *args):
            pass

    server = Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"server": {
        "url": f"http://127.0.0.1:{server.server_port}/chat",
        "model": "test-model", "auth": {"username": "test", "password": "test"}
    }}
    try:
        yield server, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def assert_connections_closed(server, expected):
    import time
    deadline = time.monotonic() + 3
    while server.closed_connections < expected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.connections == expected
    assert server.closed_connections == expected


def test_imported_calls_work_across_event_loops(entry_point, model_server):
    module, _ = entry_point
    server, config = model_server
    for _ in range(2):
        content, reasoning, duration = asyncio.run(module.call_gemma_async([], config))
        assert (content, reasoning) == ("Hello", "Thinking")
        assert duration >= 0
    assert len(server.requests) == 2
    assert_connections_closed(server, 2)


@pytest.mark.parametrize("exit_mode", ["normal", "error", "cancelled"])
def test_app_run_reuses_and_closes_connections(entry_point, model_server, exit_mode):
    module, app_class = entry_point
    server, config = model_server
    app = app_class.__new__(app_class)
    app.config = config
    app.args = SimpleNamespace(show_reasoning=False, show_output=False, config="unused.yaml")
    app.messages = []
    app.http_client = None
    app.log = lambda text: None
    app._set_input_enabled = lambda enabled: None
    clients = []

    # Use the real request loop and Application task cleanup, without a terminal.
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    application = Application(input=DummyInput(), output=DummyOutput())
    app.app = application

    async def run_conversation():
        clients.append(app.http_client)
        try:
            await app.handle_input("first turn")
            await app.handle_input("second turn")
            assert app.http_client is clients[-1]
            assert not app.http_client.is_closed
            if exit_mode == "error":
                raise RuntimeError("simulated UI failure")
            if exit_mode == "cancelled":
                raise asyncio.CancelledError()
        finally:
            await application.cancel_and_wait_for_background_tasks()

    application.run_async = run_conversation

    async def run_app():
        if exit_mode == "normal":
            await app.run()
        elif exit_mode == "error":
            with pytest.raises(RuntimeError, match="simulated UI failure"):
                await app.run()
        else:
            with pytest.raises(asyncio.CancelledError):
                await app.run()
        assert app.http_client is None
        assert clients[-1].is_closed
        assert not application._background_tasks

    # The same app can start again on a fresh loop, without old transports.
    asyncio.run(run_app())
    assert_connections_closed(server, 1)
    asyncio.run(run_app())
    assert_connections_closed(server, 2)
    assert clients[0] is not clients[1]
    assert len(server.requests) == 4
    assert all(req["model"] == "test-model" for req in server.requests)
    assert [len(req["messages"]) for req in server.requests] == [1, 3, 5, 7]
    assert [msg["content"] for msg in app.messages] == [
        "first turn", "Hello", "second turn", "Hello",
        "first turn", "Hello", "second turn", "Hello"
    ]


def test_imported_call_closes_client_after_http_error(entry_point, model_server):
    import httpx
    module, _ = entry_point
    server, config = model_server
    server.status = 503
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(module.call_gemma_async([], config))
    assert_connections_closed(server, 1)
    server.status = 200
    assert asyncio.run(module.call_gemma_async([], config))[0] == "Hello"
    assert_connections_closed(server, 2)


def test_ui_exit_cancels_model_task_before_client_closes(entry_point, model_server):
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    module, app_class = entry_point
    server, config = model_server
    app = app_class.__new__(app_class)
    app.args = SimpleNamespace(config="unused.yaml")
    app.log = lambda text: None
    app.is_thinking = False
    clients = []
    cancelled_while_open = []

    async def exercise():
        with create_pipe_input() as pipe:
            application = Application(input=pipe, output=DummyOutput())
            app.app = application
            real_run_async = application.run_async

            async def model_task():
                clients.append(app.http_client)
                try:
                    assert (await module.call_gemma_async(
                        [], config, client=app.http_client
                    ))[0] == "Hello"
                    application.exit()
                    await asyncio.Event().wait()
                finally:
                    cancelled_while_open.append(not app.http_client.is_closed)

            async def run_ui():
                await real_run_async(pre_run=lambda: application.create_background_task(model_task()))

            application.run_async = run_ui
            await asyncio.wait_for(app.run(), timeout=5)
            assert not application._background_tasks
            assert app.http_client is None
            assert clients[0].is_closed

    asyncio.run(exercise())
    assert cancelled_while_open == [True]
    assert_connections_closed(server, 1)
