# CodeAgent

A local LLM-powered agentic coding assistant with smart 3-model routing (27B + 14B + 1.5B). Replaces cloud-dependent AI assistants with a fully self-hosted solution running on CPU — no GPU required.

## Why CodeAgent?

Most AI coding assistants (OpenCode, Cursor, etc.) waste **14,000+ tokens** on system prompts before you even type a word. CodeAgent uses a **smart router** that injects only relevant tools per request, keeping system prompts under **1,000 tokens** — making it practical to run on local quantized models.

### Key Features

- **Smart 3-Model Router**: 1.5B classifies → routes to **27B Opus** (coding/EBS), **14B** (system), or **1.5B** (greetings)
- **Agentic Tool Loop**: LLM calls tools, gets results, reasons, calls more tools — like a real agent
- **13 Built-in Tools**: bash, file read/write/edit, glob search, git, Oracle DB, EBS module guide
- **Parallel Multi-Worker Terminals**: Up to 5 concurrent bash workers with tabbed UI, each with its own persistent shell session — like tmux split panes in a browser
- **Tool Approval System**: Every tool call requires user approval before execution (Allow/Deny)
- **Stop/Cancel**: Abort any ongoing AI response or worker mid-stream
- **Message Actions**: Copy, Edit, Regenerate, Delete on every chat message
- **MCP Client**: Connect external MCP tool servers (stdio + SSE transport)
- **Skills System**: Load `.md` skill files that auto-activate based on keywords
- **Dual Interface**: Terminal UI (Textual) + Web UI (FastAPI + WebSocket)
- **Live Token Stats**: Real-time token count, elapsed time, tokens/sec — resets per request, just like llama.cpp's UI
- **Session Management**: Save/load/delete conversation history with token-aware trimming
- **Mid-Task Queries**: Ask questions while workers are running — AI responds based on live terminal state
- **Web UI responsiveness**: Streams assistant text as plain monospace during generation; markdown is applied when the assistant message completes. After a tool result, a short “next model step” status line shows that the agent is still working until the next tokens arrive.
- **llama.cpp tuning**: For faster 14B / Opus inference (GPU layers, quant, batch, context), see [docs/LLAMA_SPEED.md](docs/LLAMA_SPEED.md).

### Architecture

```
User Message
  → Smart Router (1.5B model, instant classification)
  → Category: simple | coding | ebs | system
  → Model Selection:
      coding/ebs → 27B Opus (superior reasoning)
      system     → 14B Coder (infrastructure expertise)
      simple     → 1.5B (greetings) or 27B Opus (writing/drafting)
  → Prompt Builder (inject only relevant 2-4 tools, ~800 tokens)
  → Selected LLM generates response or tool calls
  → If tool call: approval prompt → user Allow/Deny
  → If bash: WorkerPool assigns worker (W1-W5) → live terminal output
  → Tool result fed back → re-prompt → final response
  → Token stats displayed (prompt/completion/total/t/s)
```

## Hardware Requirements

CodeAgent runs entirely on **CPU** — no GPU required. Tested and working on:

### Minimum (for 14B + 1.5B models only)

| Component | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores / 4 threads | 8+ cores (Xeon / Ryzen) |
| RAM | 16 GB | 32 GB |
| Disk | 20 GB free | 50 GB+ |
| GPU | Not required | Optional (CUDA/ROCm for faster inference) |
| OS | Linux (Ubuntu 20.04+ / Oracle Linux 8+) | Ubuntu 24.04 / Oracle Linux 9 |

### Recommended (for all 3 models: 27B + 14B + 1.5B)

| Component | Recommended |
|---|---|
| CPU | 16+ cores / 32 threads |
| RAM | 64 GB |
| Disk | 80 GB+ free |
| GPU | Not required (CPU-only works) |
| OS | Ubuntu 24.04 / Oracle Linux 9 |

### Tested Configuration

This project was built and tested on:

```
CPU:    32 vCPUs (16 cores × 2 threads)
RAM:    64 GB DDR4
Disk:   146 GB
GPU:    None (CPU-only inference)
OS:     Ubuntu / Oracle Linux
Models: 3 concurrent llama.cpp servers (27B + 14B + 1.5B)
```

### Model RAM Usage

| Model | Port | File Size | RAM at Runtime | Role |
|---|---|---|---|---|
| Qwen3.5 27B Opus Distilled Q4_K_M | 8085 | ~16 GB | ~18 GB | Coding, EBS, Writing |
| Qwen 2.5 Coder 14B Q4_K_M | 8080 | ~8.4 GB | ~10 GB | System/Infrastructure |
| Qwen 2.5 1.5B Instruct Q4_K_M | 8090 | ~1.0 GB | ~1.5 GB | Router + Quick greetings |
| **Total (all 3 models)** | | **~25.4 GB** | **~30 GB** | |

### Performance (CPU-only, 32 vCPUs)

| Metric | 27B Opus | 14B Coder | 1.5B Fast |
|---|---|---|---|
| Prompt processing | ~10-18 t/s | ~15-25 t/s | ~80-120 t/s |
| Token generation | ~2-4 t/s | ~3-6 t/s | ~20-40 t/s |
| Time to first token | ~3-8 sec | ~2-5 sec | ~0.5-1 sec |

> **Note**: With GPU acceleration (NVIDIA CUDA), models can achieve 30-80+ tokens/sec generation speed. llama.cpp supports CUDA, ROCm, Metal, and Vulkan backends.

### llama.cpp Server Configuration

For the **27B Opus model** (port 8085) — coding/EBS/writing:
```bash
llama-server \
  --model Qwen3.5-27B-Opus-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8085 \
  --ctx-size 32768 --batch-size 512 --ubatch-size 256 \
  --parallel 2 --cont-batching \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --threads 16 --no-mmap --jinja -ngl 0
```

For the **14B main model** (port 8080) — system/infrastructure:
```bash
llama-server \
  --model qwen2.5-coder-14b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 32768 \
  --parallel 2 \
  --threads 20
```

For the **1.5B fast model** (port 8090) — router/greetings:
```bash
llama-server \
  --model qwen2.5-1.5b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8090 \
  --ctx-size 8192 \
  --parallel 2 \
  --threads 4
```

> **Tip**: Adjust `--threads` based on your CPU core count. Use `--no-mmap` for the 27B model to prevent disk I/O bottlenecks. All 3 servers run concurrently — ensure enough RAM.

## Quick Start

### Prerequisites

- Linux server (tested on Ubuntu 24.04 / Oracle Linux)
- Python 3.10+
- [llama.cpp](https://github.com/ggerganov/llama.cpp) server running with a GGUF model
- (Optional) Oracle database for EBS tools

### Installation

```bash
# Clone
git clone https://github.com/tagtuner/CodeAgent.git
cd CodeAgent

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp config.yaml /opt/codeagent/config.yaml
# Edit config.yaml with your model URLs, Oracle DB details, etc.

# Deploy (creates systemd services, nginx proxy, skill files)
chmod +x deploy.sh
sudo bash deploy.sh
```

### Usage

```bash
# Web UI (browser)
python3 main.py web
# Then open http://your-server:4200

# Terminal UI (SSH)
python3 main.py tui

# Single-shot CLI
python3 main.py chat "Show me disk usage"
python3 main.py chat "Write a Python function to parse CSV"
```

### Configuration

Edit `config.yaml`:

```yaml
models:
  main:
    url: http://127.0.0.1:8080/v1    # 14B — system/infrastructure tasks
    name: qwen2.5-coder-14b
    ctx_size: 16384
    max_output: 4096
  fast:
    url: http://127.0.0.1:8090/v1    # 1.5B — router + quick greetings
    name: qwen2.5-1.5b
    ctx_size: 8192
    max_output: 512
  opus:
    url: http://127.0.0.1:8085/v1    # 27B — coding/EBS/writing (best quality)
    name: qwen3.5-27b-opus
    ctx_size: 16384
    max_output: 4096
```

## Project Structure

```
CodeAgent/
├── main.py                  # CLI entry point (tui | web | chat)
├── config.yaml              # Configuration
├── requirements.txt         # Python dependencies
├── deploy.sh                # Automated deployment script
├── core/                    # Core engine
│   ├── agent.py             # Agentic loop (tool call → approval → execute → re-prompt)
│   ├── config.py            # YAML config loader
│   ├── llm.py               # Async LLM client with streaming + stats
│   ├── prompt.py            # Smart prompt builder (<1000 token system prompts)
│   ├── router.py            # 1.5B-based request classifier
│   ├── session.py           # Conversation history with token-aware trimming
│   └── worker.py            # SubWorker + WorkerPool (max 5 parallel bash terminals)
├── tools/                   # Tool system
│   ├── base.py              # BaseTool + ToolRegistry
│   ├── bash_tool.py         # Shell command execution
│   ├── file_ops.py          # File read/write/edit/search
│   ├── git_tool.py          # Git operations
│   ├── oracle.py            # Oracle DB query/schema/validate/explain
│   └── ebs.py               # Oracle EBS module knowledge guide
├── mcp/                     # Model Context Protocol client
│   ├── client.py            # MCP protocol (stdio + SSE transport)
│   └── registry.py          # Auto-discover and register MCP tools
├── skills/                  # Skills system
│   ├── loader.py            # Parse .md skill files
│   ├── manager.py           # Activate/deactivate per session
│   └── library/             # Skill files directory
│       └── oracle_ebs_basics.md
├── ui/                      # User interfaces
│   ├── tui/
│   │   └── app.py           # Terminal UI (Textual library)
│   └── web/
│       ├── app.py           # FastAPI + WebSocket server
│       └── static/
│           └── index.html   # Modern dark-theme web UI
└── docs/
    └── PLAN.md              # Original architecture plan
```

## Token Efficiency Comparison

| Scenario | OpenCode | CodeAgent |
|---|---|---|
| "Hello, how are you?" | ~14,000 tokens (all tools loaded) | ~200 tokens (no tools, 1.5B model) |
| "Write a Python script" | ~14,000 tokens | ~800 tokens (bash + file tools only) |
| "Show me POs from vendor" | ~14,000 tokens | ~1,200 tokens (Oracle + EBS tools only) |
| "git status" | ~14,000 tokens | ~600 tokens (bash + git tools only) |

## Adding Custom Tools

Create a new tool in `tools/`:

```python
from tools.base import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "What this tool does"
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input parameter"}
        },
        "required": ["input"]
    }

    async def execute(self, input: str) -> str:
        return f"Result: {input}"
```

Register it in `main.py`:

```python
from tools.my_tool import MyTool
registry.register(MyTool())
```

Add it to the appropriate router category in `core/router.py`:

```python
TOOL_MAP = {
    "coding": ["bash", "read_file", "write_file", "my_tool"],
    ...
}
```

## Adding Skills

Create a `.md` file in `skills/library/`:

```markdown
---
name: my_skill
description: What this skill provides
tags: [python, web]
triggers: [flask, django, fastapi]
---

# My Skill Knowledge

Content that gets injected into the prompt when triggered...
```

Skills auto-activate when trigger keywords match the user's message.

## Adding MCP Servers

Add to `config.yaml`:

```yaml
mcp_servers:
  - name: filesystem
    command: npx @modelcontextprotocol/server-filesystem /opt
  - name: github
    url: http://localhost:3100/sse
```

MCP tools are auto-discovered and registered on startup.

## Recommended Models

| Model | Size | Use Case | HuggingFace |
|---|---|---|---|
| **Qwen3.5 27B Opus Distilled Q4_K_M** | 16 GB | Coding, EBS, writing (best reasoning) | [Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF](https://huggingface.co/Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF) |
| Qwen 2.5 Coder 14B Q4_K_M | 8.4 GB | System/infrastructure tasks | [bartowski/Qwen2.5-Coder-14B-Instruct-GGUF](https://huggingface.co/bartowski/Qwen2.5-Coder-14B-Instruct-GGUF) |
| Qwen 2.5 1.5B Instruct Q4_K_M | 1.0 GB | Fast router + simple greetings | [bartowski/Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF) |

## Changelog

### v0.6 — 3-Model Smart Routing with 27B Opus (Latest)
- **Qwen3.5-27B Opus Distilled**: Added as primary model for coding, EBS, and writing tasks — fine-tuned on Claude 4.6 Opus reasoning
- **Smart 3-model routing**: Coding/EBS → 27B Opus, System → 14B Coder, Greetings → 1.5B Fast
- **Non-greeting simple tasks** (draft, write, explain) now routed to 27B for superior quality
- **64GB RAM / 32 vCPU** tested configuration — all 3 models running concurrently on CPU

### v0.5 — Parallel Multi-Worker Terminals
- **WorkerPool**: Up to 5 concurrent bash workers, each with its own persistent shell
- **Tabbed Terminal UI**: W1, W2, W3... tabs with color-coded status dots (yellow=running, green=done, red=error)
- **Per-worker controls**: Kill individual workers via tab, clear output, minimize panel
- **Mid-task queries**: Ask the AI questions while workers execute — responds using live terminal context from all workers
- **Hard limit**: Max 5 workers enforced to protect server resources

### v0.4 — Sub-Worker Terminal System
- Persistent background bash shell for command execution
- Live terminal panel with streaming output
- Worker state awareness — AI knows what's running in the terminal
- Kill worker support from UI

### v0.3 — Tool Approval + Stop/Cancel + Message Actions
- **Tool approval**: Every tool call (bash, file ops, SQL) requires explicit user Allow/Deny
- **Stop button**: Cancel any ongoing AI response or tool execution mid-stream
- **Message actions**: Copy, Edit, Regenerate, Delete buttons on every message
- 120-second approval timeout with auto-deny

### v0.2 — Live Token Stats + Smart Routing
- Real-time streaming token stats (prompt/completion/total/t/s) — resets per request
- Smart simple response routing: greetings → 1.5B fast model, writing tasks → 14B for quality
- Improved router classification to prevent false tool triggers
- Session history loading and deletion from sidebar

### v0.1 — Initial Release
- Smart Router with 1.5B classification
- Agentic tool loop with 13 built-in tools
- MCP client (stdio + SSE)
- Skills system with keyword triggers
- Web UI + Terminal UI
- Token-aware session management

## License

MIT
