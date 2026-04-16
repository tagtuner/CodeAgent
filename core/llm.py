from __future__ import annotations
import json
import httpx
from dataclasses import dataclass
from typing import AsyncIterator

from .config import ModelConfig


@dataclass
class Chunk:
    type: str          # "text" | "tool_call_start" | "tool_call_arg" | "done"
    content: str = ""
    tool_name: str = ""
    tool_args: str = ""
    finish_reason: str | None = None


class LLMClient:
    def __init__(self, model_cfg: ModelConfig, timeout: float = 300):
        self.base_url = model_cfg.url.rstrip("/")
        self.model = model_cfg.name
        self.max_output = model_cfg.max_output
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10))

    async def close(self):
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        repeat_penalty: float = 1.15,
        top_p: float = 0.9,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_output,
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "top_p": top_p,
            "stream": False,
        }
        resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        return {
            "content": choice["message"].get("content", ""),
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": data.get("usage", {}),
        }

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        repeat_penalty: float = 1.15,
        top_p: float = 0.9,
    ) -> AsyncIterator[Chunk]:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_output,
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "top_p": top_p,
            "stream": True,
        }
        async with self._client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    yield Chunk(type="done")
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = data.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                finish = choice.get("finish_reason")
                content = delta.get("content")
                if content:
                    yield Chunk(type="text", content=content)
                if finish:
                    yield Chunk(type="done", finish_reason=finish)
