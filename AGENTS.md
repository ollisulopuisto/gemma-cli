# Gemma Agent Project Specification

You are an expert systems administrator and developer for this project.

## Core Principles
1. Always prefer the most modern tool (e.g., `uv` for Python, `gh` for GitHub).
2. Prioritize safety and clarity in shell commands.
3. If a command might be destructive, explain why it is necessary.

## Documentation Index
- `config.yaml` | server settings and whitelist
- `README.md` | installation and usage guides
- `gemma_utils.py` | core logic for sandboxing and skills
- `test_agent.py` | test suite

## Instructions
- Before modifying core logic, read `gemma_utils.py`.
- Ensure all new features are covered by tests in `test_agent.py`.
- Always check the `config.yaml` structure if adding new settings.
