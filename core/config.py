from __future__ import annotations
import yaml
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    url: str
    name: str
    ctx_size: int = 16384
    max_output: int = 4096


@dataclass
class Config:
    models: dict[str, ModelConfig] = field(default_factory=dict)
    router: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    mcp_servers: list = field(default_factory=list)
    skills_dir: str = "/opt/codeagent/skills/library"
    web: dict = field(default_factory=lambda: {"host": "0.0.0.0", "port": 4200})
    permissions: dict = field(default_factory=dict)
    session: dict = field(default_factory=lambda: {
        "dir": "/opt/codeagent/sessions",
        "max_history_tokens": 12000,
    })
    agent: dict = field(default_factory=lambda: {
        "max_iterations": 10,
        "repeat_penalty": 1.15,
        "temperature": 0.7,
        "top_p": 0.9,
    })

    @classmethod
    def load(cls, path: str | Path = "/opt/codeagent/config.yaml") -> Config:
        p = Path(path)
        if not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text()) or {}
        models = {}
        for key, m in raw.get("models", {}).items():
            models[key] = ModelConfig(
                url=m.get("url", "http://127.0.0.1:8080/v1"),
                name=m.get("name", "default"),
                ctx_size=m.get("ctx_size", 16384),
                max_output=m.get("max_output", 4096),
            )
        return cls(
            models=models,
            router=raw.get("router", {}),
            tools=raw.get("tools", {}),
            mcp_servers=raw.get("mcp_servers", []),
            skills_dir=raw.get("skills_dir", "/opt/codeagent/skills/library"),
            web=raw.get("web", {"host": "0.0.0.0", "port": 4200}),
            permissions=raw.get("permissions", {}),
            session=raw.get("session", {"dir": "/opt/codeagent/sessions", "max_history_tokens": 12000}),
            agent=raw.get("agent", {}),
        )

    @property
    def main_model(self) -> ModelConfig:
        return self.models.get("main", ModelConfig(url="http://127.0.0.1:8080/v1", name="default"))

    @property
    def fast_model(self) -> ModelConfig:
        return self.models.get("fast", self.main_model)
