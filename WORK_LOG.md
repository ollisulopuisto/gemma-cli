# Work log

## 2026-11-19: PR #13 HTTP connection pooling repair

Removed the process-global client from both entry points. Each application run
owns an async client and closes it before its event loop ends. Imported calls
without an explicit client keep their independent per-call lifetime.
Application background tasks now include the model request and spinner, so
prompt_toolkit cancels and awaits them before client shutdown.

Added local HTTP/1.1 tests for two model turns on one connection per app run,
repeated runs on separate event loops, direct imported calls on separate loops,
HTTP errors, normal exit, UI errors, and cancellation. A headless real
prompt_toolkit run also checks that a pending background model task is cancelled
while the client is still open. Both entry points are covered.

Validation: uv run pytest -q -W error (22 passed), py_compile for both entry
points and test_agent.py, and git diff --check. The cross-loop tests were also
run against the previous PR head and both failed with Event loop is closed.
Not run: a real model server or an interactive terminal session.
