from __future__ import annotations
import json
import re
from dataclasses import dataclass
from typing import AsyncIterator

from core.config import Config
from core.llm import LLMClient, Chunk
from core.prompt import PromptBuilder
from core.session import Session
from core.router import Router
from tools.base import ToolRegistry

TOOL_CALL_RE = re.compile(r"<(?:tool_call|tools)>\s*(\{.*?\})\s*</(?:tool_call|tools)>", re.DOTALL)
CODE_BLOCK_CALL_RE = re.compile(r'```(?:json)?\s*(\{\s*"name"\s*:.*?\})\s*```', re.DOTALL)
BARE_JSON_CALL_RE = re.compile(r'^\s*(\{\s*"name"\s*:.*?"arguments"\s*:\s*\{.*?\}\s*\})\s*$', re.DOTALL | re.MULTILINE)


@dataclass
class AgentEvent:
    type: str  # "text" | "tool_start" | "tool_result" | "error" | "status" | "done"
    content: str = ""
    tool_name: str = ""
    tool_args: dict | None = None
    metadata: dict | None = None


class Agent:
    def __init__(
        self,
        config: Config,
        llm_main: LLMClient,
        llm_fast: LLMClient | None,
        registry: ToolRegistry,
        session: Session,
        skills_context: str = "",
    ):
        self.config = config
        self.llm_main = llm_main
        self.llm_fast = llm_fast
        self.registry = registry
        self.router = Router(llm_fast)
        self.prompt_builder = PromptBuilder()
        self.session = session
        self.skills_context = skills_context
        self.max_iterations = config.agent.get("max_iterations", 10)
        self.temperature = config.agent.get("temperature", 0.7)
        self.repeat_penalty = config.agent.get("repeat_penalty", 1.15)
        self.top_p = config.agent.get("top_p", 0.9)

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        self.session.add_user(user_message)

        category = await self.router.classify(user_message)
        yield AgentEvent(type="status", content=f"category:{category}")

        tool_names = self.router.get_tools(category)

        if not tool_names:
            async for event in self._simple_response(user_message):
                yield event
            return

        system_prompt = self.prompt_builder.build_system(
            category, self.registry, tool_names, self.skills_context
        )

        for iteration in range(self.max_iterations):
            history = self.session.get_history()
            messages = self.prompt_builder.build_messages(system_prompt, history)

            full_text = ""
            llm_stats = None
            async for chunk in self.llm_main.stream_chat(
                messages,
                temperature=self.temperature,
                repeat_penalty=self.repeat_penalty,
                top_p=self.top_p,
            ):
                if chunk.type == "text":
                    full_text += chunk.content
                    yield AgentEvent(type="text_delta", content=chunk.content)
                elif chunk.type == "done" and chunk.stats:
                    llm_stats = chunk.stats

            tool_calls = self._extract_tool_calls(full_text)

            if not tool_calls:
                clean_text = self._clean_response(full_text)
                self.session.add_assistant(clean_text)
                yield AgentEvent(type="text", content=clean_text)
                if llm_stats:
                    yield AgentEvent(type="stats", metadata=llm_stats)
                break
            else:
                self.session.add_assistant(full_text)
                if llm_stats:
                    yield AgentEvent(type="stats", metadata=llm_stats)
                for tc_name, tc_args in tool_calls:
                    yield AgentEvent(
                        type="tool_start",
                        tool_name=tc_name,
                        tool_args=tc_args,
                    )
                    try:
                        result = await self.registry.execute(tc_name, tc_args)
                        result_str = result if isinstance(result, str) else json.dumps(result, default=str)
                        if len(result_str) > 4000:
                            result_str = result_str[:4000] + "\n... (truncated)"
                    except Exception as e:
                        result_str = f"Error: {e}"

                    yield AgentEvent(
                        type="tool_result",
                        tool_name=tc_name,
                        content=result_str,
                    )
                    self.session.add_tool_result(tc_name, result_str)
        else:
            yield AgentEvent(type="error", content="Max tool iterations reached")

        yield AgentEvent(type="done")

    async def _simple_response(self, message: str):
        llm = self.llm_fast or self.llm_main
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": "You are CodeAgent, a helpful assistant. Be concise and direct."},
                {"role": "user", "content": message},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        text = resp["content"]
        self.session.add_assistant(text)
        yield AgentEvent(type="text", content=text)
        if resp.get("stats"):
            yield AgentEvent(type="stats", metadata=resp["stats"])
        if resp.get("stats"):
            yield AgentEvent(type="stats", metadata=resp["stats"])
        yield AgentEvent(type="done")

    def _extract_tool_calls(self, text: str) -> list[tuple[str, dict]]:
        calls = []
        for pattern in (TOOL_CALL_RE, CODE_BLOCK_CALL_RE, BARE_JSON_CALL_RE):
            for match in pattern.finditer(text):
                try:
                    obj = json.loads(match.group(1))
                    name = obj.get("name", "")
                    args = obj.get("arguments", {})
                    if name and self.registry.get(name):
                        calls.append((name, args))
                except (json.JSONDecodeError, KeyError):
                    continue
            if calls:
                break
        return calls

    def _clean_response(self, text: str) -> str:
        text = TOOL_CALL_RE.sub("", text)
        text = CODE_BLOCK_CALL_RE.sub("", text)
        text = BARE_JSON_CALL_RE.sub("", text)
        text = re.sub(r"</?(?:tools?|tool_call)>", "", text)
        text = re.sub(r"</?tool_response[^>]*>", "", text)
        return text.strip()
