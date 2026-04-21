from __future__ import annotations
import asyncio
import re
import shutil
from pathlib import Path

from .base import BaseTool

# Extra CLI tokens after `blender -b <file> [-P script.py] ...` — no shell; block injection / inline Python flags.
# Allow Blender output patterns like "render_####" in argv tokens.
_EXTRA_ARG_RE = re.compile(r"^[a-zA-Z0-9_./:=+@#-]+$")


def _safe_extra_arg(s: str) -> bool:
    if not isinstance(s, str) or not s or len(s) > 512:
        return False
    if not _EXTRA_ARG_RE.match(s):
        return False
    low = s.lower()
    if "--python" in low or "--python-expr" in low or low == "-c":
        return False
    if s in ("-P", "--python", "--python-expr"):
        return False
    return True


def _resolve_existing_file(label: str, path: str, exts: frozenset[str]) -> Path | str:
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return f"ERROR: {label} is not a valid path"
    if not p.is_file():
        return f"ERROR: {label} not found or not a file: {p}"
    if p.suffix.lower() not in exts:
        return f"ERROR: {label} must be one of {sorted(exts)} — got {p.suffix!r}"
    return p


class BlenderTool(BaseTool):
    name = "blender"
    description = (
        "Run Blender in background mode (-b) with no GUI. Use for rendering or batch tasks from a .blend file "
        "or from Blender's default startup scene when no blend_file is provided. "
        "Prefer this over raw bash for `blender` so arguments stay structured and timeouts apply. "
        "Paths must be absolute (e.g. under the session workspace). Optional `python_script` runs with Blender's `-P`. "
        "`extra_args` are appended as separate argv tokens only (e.g. '-o', '//render_####', '-F', 'PNG', '-f', '1'); "
        "no shell metacharacters; no --python / --python-expr (use `python_script` instead)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "blend_file": {
                "type": "string",
                "description": "Optional absolute path to an existing .blend file. Leave empty to use default startup scene.",
            },
            "python_script": {
                "type": "string",
                "description": "Optional absolute path to a .py script executed via Blender's -P flag",
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional extra argv tokens after -P (e.g. render flags). Max 48 tokens.",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds before killing the process (default from config, often 600)",
            },
            "purpose": {
                "type": "string",
                "description": "Short label for logs/UI (not passed to Blender)",
            },
        },
        "required": [],
    }

    def __init__(self, binary: str | None = None, default_timeout: int = 600):
        self.binary = (binary or shutil.which("blender") or "/usr/bin/blender").strip()
        self.default_timeout = max(30, int(default_timeout))

    async def execute(
        self,
        blend_file: str = "",
        python_script: str = "",
        extra_args: list | None = None,
        timeout: int | None = None,
        purpose: str = "",
    ) -> str:
        _ = purpose
        if not Path(self.binary).is_file():
            return (
                f"ERROR: Blender binary not found at {self.binary!r}. "
                "Install blender or set tools.blender.binary in config.yaml"
            )

        bf: Path | None = None
        if blend_file and str(blend_file).strip():
            bf_res = _resolve_existing_file("blend_file", blend_file, frozenset({".blend"}))
            if isinstance(bf_res, str):
                return bf_res
            bf = bf_res

        cmd: list[str] = [self.binary, "-b"]
        if bf:
            cmd.append(str(bf))
        if python_script and str(python_script).strip():
            ps = _resolve_existing_file("python_script", python_script, frozenset({".py"}))
            if isinstance(ps, str):
                return ps
            cmd.extend(["-P", str(ps)])

        extras = extra_args if isinstance(extra_args, list) else []
        for i, a in enumerate(extras[:48]):
            if not isinstance(a, str):
                return f"ERROR: extra_args[{i}] must be a string"
            if not _safe_extra_arg(a):
                return f"ERROR: disallowed extra_args token: {a!r}"
            cmd.append(a)

        tout = int(timeout) if timeout is not None else self.default_timeout
        tout = max(30, min(tout, 3600))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=(str(bf.parent) if bf else None),
            )
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=tout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"Blender timed out after {tout}s (command: {' '.join(cmd[:6])} ...)"
        except Exception as e:
            return f"Blender execution error: {e}"

        out = raw.decode(errors="replace").strip()
        lines = [f"[blender argv] {' '.join(cmd)}", f"[exit_code: {proc.returncode}]"]
        if out:
            lines.append(out)
        result = "\n".join(lines)
        if len(result) > 12000:
            result = result[:12000] + "\n... (truncated)"
        return result
