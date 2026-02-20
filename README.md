# Gemma-CLI: Local Agentic Tool-Use for Gemma 3

This tool provides a [Gemini-CLI](https://github.com/google-gemini/gemini-cli) like experience for a locally running Gemma 3 model. It allows the model to interact with your computer by executing shell commands and reading files through a ReAct (Reasoning and Acting) loop.

## 🛡️ Security Features

**Security is paramount when running LLM-generated code.** This tool includes several layers of protection:

1.  **Human-in-the-loop (HITL)**: By default, the tool will **always ask for your confirmation** before executing any shell command.
2.  **macOS Sandboxing**: On macOS, commands are wrapped in `sandbox-exec` (Seatbelt) to restrict file system and network access.
3.  **Cross-Platform Awareness**: The tool detects your OS and shell to ensure commands are appropriate for your environment.

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
- **`strict`**: Restricts both read and write access EXCLUSIVELY to the current project directory. **Disables network access** and mach-lookup on macOS.
- **`off`**: No sandboxing applied.

### CLI Options
- `--config PATH`: Use a specific configuration file.
- `--sandbox {off,permissive,strict}`: Override the sandbox level.
- `--no-sandbox`: Completely disable sandboxing.
- `--show-output`: Display the STDOUT/STDERR of tools directly in the UI.
- `--show-reasoning`: Display the model's internal thinking process.
- `--yes`: **Auto-approve all commands.** Use with extreme caution!

## Testing

Run tests:
```bash
uv run pytest
```

## How it Works

The tool implements a **ReAct loop**:

1.  **System Prompt**: Informs Gemma 3 that it has shell access.
2.  **Extraction**: Parsers look for ```tool_code ... ``` blocks.
3.  **Human Approval**: The user is prompted to allow/deny the command.
4.  **Execution**: The script executes the command (optionally sandboxed).
5.  **Feedback**: Output is fed back to the model as an "Observation".

## Security Warning

**Caution**: While sandboxing and HITL are provided, they are not a perfect security boundary. Only use it with models you trust and in environments where you can safely run the allowed commands. **Never use --yes with untrusted prompts or data.**
