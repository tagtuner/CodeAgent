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
    stats: dict | None = None


class LLMClient:
    def __init__(self, model_cfg: ModelConfig, timeout: float = 300):
        self.base_url = model_cfg.url.rstrip("/")
        self.model = model_cfg.name
        self.max_output = model_cfg.max_output
        self.timeout = timeout
        self.api_key = model_cfg.api_key
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["HTTP-Referer"] = "http://localhost:4200"
            headers["X-Title"] = "CodeAgent"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), headers=headers)

    async def close(self):
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        repeat_penalty: float = 1.15,
        top_p: float = 0.9,
        model: str | None = None,
    ) -> dict:
        payload = {
            "model": model if model else self.model,
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
        usage = data.get("usage", {})
        raw_content = choice.get("message", {}).get("content")
        if raw_content is None:
            content_str = ""
        elif isinstance(raw_content, str):
            content_str = raw_content
        elif isinstance(raw_content, list):
            # Multimodal / some providers return list of {type, text} blocks
            parts: list[str] = []
            for part in raw_content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
            content_str = "".join(parts)
        else:
            content_str = str(raw_content)
        return {
            "content": content_str,
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": usage,
            "stats": {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0), "total_tokens": usage.get("total_tokens", 0)},
        }

    async def stream_chat(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        repeat_penalty: float = 1.15,
        top_p: float = 0.9,
        model: str | None = None,
    ) -> AsyncIterator[Chunk]:
        payload = {
            "model": model if model else self.model,
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
