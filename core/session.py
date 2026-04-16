from __future__ import annotations
import json
import time
import uuid
from pathlib import Path


class Session:
    def __init__(self, session_id: str | None = None, max_history_tokens: int = 12000):
        self.id = session_id or uuid.uuid4().hex[:12]
        self.messages: list[dict] = []
        self.max_history_tokens = max_history_tokens
        self.created_at = time.time()

    def add_user(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_result(self, tool_name: str, result: str):
        self.messages.append({
            "role": "user",
            "content": f"<tool_response name=\"{tool_name}\">\n{result}\n</tool_response>",
        })

    def get_history(self) -> list[dict]:
        return self._trimmed()

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // 3 + 1

    def _trimmed(self) -> list[dict]:
        total = 0
        result = []
        for msg in reversed(self.messages):
            tokens = self._estimate_tokens(msg.get("content", ""))
            if total + tokens > self.max_history_tokens and result:
                break
            result.append(msg)
            total += tokens
        result.reverse()
        return result

    def clear(self):
        self.messages.clear()

    def save(self, session_dir: str | Path):
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.id}.json"
        path.write_text(json.dumps({
            "id": self.id,
            "created_at": self.created_at,
            "messages": self.messages,
        }, indent=2))
        return path

    @classmethod
    def load(cls, path: str | Path, max_history_tokens: int = 12000) -> Session:
        data = json.loads(Path(path).read_text())
        s = cls(session_id=data["id"], max_history_tokens=max_history_tokens)
        s.created_at = data.get("created_at", time.time())
        s.messages = data.get("messages", [])
        return s

    @classmethod
    def list_sessions(cls, session_dir: str | Path) -> list[dict]:
        d = Path(session_dir)
        if not d.exists():
            return []
        sessions = []
        for f in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text())
                first_msg = ""
                for m in data.get("messages", []):
                    if m["role"] == "user":
                        first_msg = m["content"][:80]
                        break
                sessions.append({
                    "id": data["id"],
                    "preview": first_msg,
                    "messages": len(data.get("messages", [])),
                    "file": str(f),
                })
            except Exception:
                continue
        return sessions
