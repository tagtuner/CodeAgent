from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {}

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        ...


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    async def execute(self, name: str, args: dict) -> Any:
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"
        try:
            return await tool.execute(**args)
        except TypeError as e:
            return f"Invalid arguments for {name}: {e}"

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_definitions(self, names: list[str] | None = None) -> list[dict]:
        result = []
        for n, t in self._tools.items():
            if names and n not in names:
                continue
            result.append({
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            })
        return result
