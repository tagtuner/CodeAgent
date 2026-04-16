from __future__ import annotations
import glob as globmod
from pathlib import Path
from .base import BaseTool


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read the contents of a file. Returns file content with line numbers."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file"},
            "offset": {"type": "integer", "description": "Start line (1-based, optional)"},
            "limit": {"type": "integer", "description": "Number of lines to read (optional)"},
        },
        "required": ["path"],
    }

    async def execute(self, path: str, offset: int = 0, limit: int = 0) -> str:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        if not p.is_file():
            return f"Not a file: {path}"
        try:
            text = p.read_text(errors="replace")
            lines = text.splitlines()
            if offset > 0:
                lines = lines[offset - 1:]
            if limit > 0:
                lines = lines[:limit]
            numbered = [f"{i + (offset or 1):>6}|{line}" for i, line in enumerate(lines)]
            result = "\n".join(numbered)
            if len(result) > 10000:
                result = result[:10000] + "\n... (truncated)"
            return result
        except Exception as e:
            return f"Error reading {path}: {e}"


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write content to a file. Creates the file if it doesn't exist, overwrites if it does."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, path: str, content: str) -> str:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"


class EditFileTool(BaseTool):
    name = "edit_file"
    description = "Replace a specific string in a file. The old_string must exist exactly once in the file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file"},
            "old_string": {"type": "string", "description": "Exact text to find and replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, path: str, old_string: str, new_string: str) -> str:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        try:
            text = p.read_text()
            count = text.count(old_string)
            if count == 0:
                return f"old_string not found in {path}"
            if count > 1:
                return f"old_string found {count} times — must be unique. Add more context."
            new_text = text.replace(old_string, new_string, 1)
            p.write_text(new_text)
            return f"Replaced in {path} (1 occurrence)"
        except Exception as e:
            return f"Error editing {path}: {e}"


class GlobSearchTool(BaseTool):
    name = "glob_search"
    description = "Search for files matching a glob pattern. Returns matching file paths."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
            "directory": {"type": "string", "description": "Base directory to search in (default: current dir)"},
        },
        "required": ["pattern"],
    }

    async def execute(self, pattern: str, directory: str = ".") -> str:
        try:
            base = Path(directory)
            matches = sorted(base.glob(pattern))[:100]
            if not matches:
                return f"No files matching '{pattern}' in {directory}"
            return "\n".join(str(m) for m in matches)
        except Exception as e:
            return f"Glob error: {e}"
