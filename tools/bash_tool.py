from __future__ import annotations
import asyncio
import shlex
from .base import BaseTool

BLOCKED_PATTERNS = [
    "rm -rf /",
    "mkfs",
    "dd if=",
    "> /dev/sd",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    ":(){ :|:& };:",
]


class BashTool(BaseTool):
    name = "bash"
    description = "Execute a shell command and return stdout/stderr. Use for running scripts, checking system status, installing packages, etc."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
        },
        "required": ["command"],
    }

    def __init__(self, blocked: list[str] | None = None, default_timeout: int = 120):
        self.blocked = blocked or BLOCKED_PATTERNS
        self.default_timeout = default_timeout

    async def execute(self, command: str, timeout: int | None = None) -> str:
        for pattern in self.blocked:
            if pattern in command:
                return f"BLOCKED: Command contains dangerous pattern '{pattern}'"

        tout = timeout or self.default_timeout
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=tout)
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            result_parts = []
            if out.strip():
                result_parts.append(out.strip())
            if err.strip():
                result_parts.append(f"[stderr]\n{err.strip()}")
            result_parts.append(f"[exit_code: {proc.returncode}]")
            result = "\n".join(result_parts)
            if len(result) > 8000:
                result = result[:8000] + "\n... (output truncated)"
            return result
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"Command timed out after {tout}s"
        except Exception as e:
            return f"Execution error: {e}"
