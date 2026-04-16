from __future__ import annotations
import asyncio
from .base import BaseTool


async def _run(cmd: str, cwd: str = ".") -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0 and err:
        return f"{out}\n[stderr] {err}" if out else f"[stderr] {err}"
    return out


class GitStatusTool(BaseTool):
    name = "git_status"
    description = "Show git status of the repository (working directory changes, staged files)."
    parameters = {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Repository path (default: current dir)"},
        },
        "required": [],
    }

    async def execute(self, directory: str = ".") -> str:
        return await _run("git status --short", directory)


class GitDiffTool(BaseTool):
    name = "git_diff"
    description = "Show git diff of uncommitted changes."
    parameters = {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Repository path"},
            "staged": {"type": "boolean", "description": "Show staged changes only (default: false)"},
        },
        "required": [],
    }

    async def execute(self, directory: str = ".", staged: bool = False) -> str:
        cmd = "git diff --staged" if staged else "git diff"
        result = await _run(cmd, directory)
        if len(result) > 6000:
            result = result[:6000] + "\n... (diff truncated)"
        return result or "(no changes)"


class GitCommitTool(BaseTool):
    name = "git_commit"
    description = "Stage all changes and create a git commit."
    parameters = {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Repository path"},
            "message": {"type": "string", "description": "Commit message"},
        },
        "required": ["message"],
    }

    async def execute(self, message: str, directory: str = ".") -> str:
        await _run("git add -A", directory)
        safe_msg = message.replace('"', '\\"')
        return await _run(f'git commit -m "{safe_msg}"', directory)
