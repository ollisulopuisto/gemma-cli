# Gemma-CLI: Local Agentic Tool-Use for Gemma 3

This tool provides a [Gemini-CLI](https://github.com/google-gemini/gemini-cli) like experience for a locally running Gemma 3 model. It allows the model to interact with your computer by executing shell commands and reading files through a ReAct (Reasoning and Acting) loop.

## Prerequisites

1.  **Local Model Server**: An OpenAI-compatible server running Gemma 3.
2.  **Authentication**: The server should be configured with Basic Auth.
3.  **Python Environment**: Python 3.11+ with `uv` installed.

## Local Model Setup

This tool is optimized for running **Gemma 3** locally on Apple Silicon using `mlx-openai-server`.

### 1. Install the Model Server
You need a Python environment with `mlx-lm` installed:
```bash
pip install mlx-lm
```

### 2. Launch the Server
Start the server with your chosen Gemma 3 variant. For example, to run the 27B model in 4-bit quantization:
```bash
python -m mlx_lm.server --model mlx-community/gemma-3-27b-it-qat-4bit --port 8000
```

### 3. VRAM / RAM Requirements
Depending on your Mac's Unified Memory, choose the appropriate model size:

| Model Size | Quantization | Required RAM (Approx.) | Recommended Mac |
| :--- | :--- | :--- | :--- |
| **Gemma 3 4B** | 4-bit | ~3-4 GB | Base M1/M2/M3 (8GB+) |
| **Gemma 3 12B** | 4-bit | ~8-10 GB | 16GB RAM or more |
| **Gemma 3 27B** | 4-bit | ~16-18 GB | 24GB-32GB RAM or more |
| **Gemma 3 27B** | 8-bit | ~28-30 GB | 36GB-64GB RAM or more |

*Note: Since Apple Silicon uses Unified Memory, the OS and other apps also consume this RAM. Always leave a few GBs of headroom.*

## Installation

1.  Ensure your local Gemma 3 server is running.
2.  Install `uv` (if not already installed).
3.  Setup the configuration:
    ```bash
    cp config.yaml.template config.yaml
    # Edit config.yaml with your server details and secrets
    ```
4.  Install dependencies:
    ```bash
    uv sync
    ```

## Configuration

The tools use `config.yaml` for settings and secrets.

### Sandboxing Levels
- **`permissive`** (Default): Allows full read access, but restricts writes to the current project directory and `/tmp`.
- **`strict`**: Restricts both read and write access EXCLUSIVELY to the current project directory. Disables network access for tool execution.
- **`off`**: No sandboxing applied.

### CLI Options
- `--config PATH`: Use a specific configuration file.
- `--sandbox {off,permissive,strict}`: Override the sandbox level.
- `--no-sandbox`: Completely disable sandboxing.
- `--show-output`: Display the STDOUT/STDERR of tools directly in the UI (normally only the model sees it).
- `--show-reasoning`: Display the model's internal thinking/reasoning process (if the model server provides `reasoning_content`).

## Environmental Context

At startup, the tool automatically injects current system information into the model's system prompt:
- OS and Version
- Current Username
- Working Directory
- Current Date/Time
- Shell Environment

This helps the model generate accurate, platform-specific commands.

## Testing

This project uses `pytest` for unit testing the core logic.

Run tests:
```bash
uv run pytest
```

The project follows a red-green testing workflow. Core utilities are located in `gemma_utils.py` and tested in `test_agent.py`.

## CI/CD

A GitHub Actions workflow is provided in `.github/workflows/ci.yml`.

- **On Push/PR**: Runs `uv run pytest`.
- **On Main Branch Push**: If tests pass, it automatically updates the version number in `pyproject.toml` and scripts to the current date (`YYYY.MM.DD`) and commits the change.

## Usage

### CLI Version
```bash
./gemma-cli.py
```

### TUI Version (Recommended)
```bash
./gemma-tui.py
```

### Example Commands
- "What files are in the current directory?"
- "Check the system load and tell me which process is using the most CPU."
- "Read the contents of gemma-cli.py and explain how the tool-calling works."

## How it Works

The tool implements a **ReAct loop**:

1.  **System Prompt**: Informs Gemma 3 that it can use the `tool_code` markdown block to execute shell commands.
2.  **Extraction**: The Python script parses the model's response for ```tool_code ... ``` blocks.
3.  **Execution**: If a block is found, the script executes the command via `subprocess.run` (optionally wrapped in `sandbox-exec`).
4.  **Feedback**: The command output (STDOUT/STDERR) is fed back to the model as an "Observation".
5.  **Iteration**: The model continues reasoning or calling more tools until it provides a final answer without a tool block.

## Security Warning

**Caution**: This script executes shell commands provided by the LLM. While sandboxing is provided via macOS `sandbox-exec`, it is not a perfect security boundary. Only use it with models you trust and in environments where you can safely run the allowed commands.
