from __future__ import annotations
import asyncio
import json
import re
from dataclasses import dataclass
from typing import AsyncIterator

from core.config import Config
from core.llm import LLMClient, Chunk
from core.prompt import PromptBuilder
from core.session import Session
from core.router import Router
from core.worker import WorkerPool
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
        llm_opus: LLMClient | None = None,
    ):
        self.config = config
        self.llm_main = llm_main
        self.llm_fast = llm_fast
        self.llm_opus = llm_opus
        self.registry = registry
        self.router = Router(llm_fast)
        self.prompt_builder = PromptBuilder()
        self.session = session
        self.skills_context = skills_context
        self.max_iterations = config.agent.get("max_iterations", 10)
        self.temperature = config.agent.get("temperature", 0.7)
        self.repeat_penalty = config.agent.get("repeat_penalty", 1.15)
        self.top_p = config.agent.get("top_p", 0.9)
        self.approval_queue: asyncio.Queue = asyncio.Queue()
        self._cancelled = False
        self.worker_pool = WorkerPool()

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

        llm = self.llm_opus if self.llm_opus and category in ("coding", "ebs") else self.llm_main

        for iteration in range(self.max_iterations):
            if self._cancelled:
                yield AgentEvent(type="status", content="Cancelled")
                break

            history = self.session.get_history()
            messages = self.prompt_builder.build_messages(system_prompt, history)

            full_text = ""
            llm_stats = None
            async for chunk in llm.stream_chat(
                messages,
                temperature=self.temperature,
                repeat_penalty=self.repeat_penalty,
                top_p=self.top_p,
            ):
                if self._cancelled:
                    break
                if chunk.type == "text":
                    full_text += chunk.content
                    yield AgentEvent(type="text_delta", content=chunk.content)
                elif chunk.type == "done" and chunk.stats:
                    llm_stats = chunk.stats

            if self._cancelled:
                yield AgentEvent(type="status", content="Cancelled")
                break

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
                    if self._cancelled:
                        break

                    yield AgentEvent(
                        type="tool_approval",
                        tool_name=tc_name,
                        tool_args=tc_args,
                    )

                    try:
                        approved = await asyncio.wait_for(
                            self.approval_queue.get(), timeout=120
                        )
                    except asyncio.TimeoutError:
                        approved = False

                    if self._cancelled or not approved:
                        result_str = "Denied by user"
                        yield AgentEvent(
                            type="tool_result",
                            tool_name=tc_name,
                            content=result_str,
                        )
                        self.session.add_tool_result(tc_name, result_str)
                        continue

                    if tc_name == "bash":
                        command = tc_args.get("command", "")
                        slot = self.worker_pool.create()
                        if slot is None:
                            result_str = f"Max {WorkerPool.MAX_WORKERS} parallel workers reached. Wait for one to finish."
                            yield AgentEvent(type="error", content=result_str)
                        else:
                            wid, worker = slot
                            yield AgentEvent(type="worker_start", metadata={"worker_id": wid})
                            yield AgentEvent(type="worker_cmd", content=command, metadata={"worker_id": wid})

                            output_lines = []
                            async for line in worker.execute(command):
                                if self._cancelled:
                                    await worker.kill()
                                    break
                                output_lines.append(line)
                                yield AgentEvent(type="worker_output", content=line, metadata={"worker_id": wid})

                            ec = worker.exit_code if worker.exit_code is not None else -1
                            result_str = "\n".join(output_lines)
                            if ec != 0:
                                result_str += f"\n[exit_code: {ec}]"
                            if len(result_str) > 4000:
                                result_str = result_str[:4000] + "\n... (truncated)"

                            yield AgentEvent(type="worker_done", metadata={"worker_id": wid, "exit_code": ec})
                    else:
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
        msg_lower = message.lower()
        is_greeting = len(message) < 30 and not any(
            w in msg_lower for w in ("draft", "write", "email", "letter", "explain", "summarize", "translate")
        )
        if is_greeting:
            llm = self.llm_fast or self.llm_main
        else:
            llm = self.llm_opus or self.llm_main
        max_tok = 200 if is_greeting else 1000

        resp = await llm.chat(
            messages=[
                {"role": "system", "content": "You are CodeAgent, a helpful professional assistant. Write clear, well-formatted responses."},
                {"role": "user", "content": message},
            ],
            max_tokens=max_tok,
            temperature=0.7,
        )
        text = resp["content"]
        self.session.add_assistant(text)
        yield AgentEvent(type="text", content=text)
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
