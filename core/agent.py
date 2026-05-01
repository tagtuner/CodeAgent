from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from core.config import Config
from core.llm import LLMClient, Chunk
from core.prompt import PromptBuilder, SIMPLE_RESPONSE_SYSTEM
from core.session import Session
from core.router import Router
from core.bash_validate import ssh_interactive_only_message
from core.worker import WorkerPool
from tools.base import ToolRegistry

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
CODE_BLOCK_CALL_RE = re.compile(r'```(?:json)?\s*(\{\s*"name"\s*:.*?\})\s*```', re.DOTALL)
BARE_JSON_CALL_RE = re.compile(r'^\s*(\{\s*"name"\s*:.*?"arguments"\s*:\s*\{.*?\}\s*\})\s*$', re.DOTALL | re.MULTILINE)
MAX_TOOL_CALL_RETRIES = 3
log = logging.getLogger("codeagent.agent")


def _redact_tool_args_for_ui(tool_args: dict | None) -> dict:
    """Strip secrets from payloads sent with tool_start/events."""
    if not isinstance(tool_args, dict):
        return {}
    out = dict(tool_args)
    for k in list(out.keys()):
        lk = str(k).lower()
        if "password" in lk or lk in ("pass", "token", "secret", "api_key"):
            v = out.get(k)
            out[k] = "***redacted***" if v else ""
    return out


def _registry_tool_execution_succeeded(tc_name: str, result_str: str) -> bool:
    """Avoid stamping dedupe-after-success when ssh_remote exited non-zero (still returns stderr text)."""
    if tc_name != "ssh_remote":
        return True
    nums = [int(x) for x in re.findall(r"\[exit_code:\s*(-?\d+)\]", result_str or "")]
    return bool(nums) and nums[-1] == 0


def _bash_worker_tab_description(command: str, tc_args: dict | None) -> str:
    """Label for the web UI worker tab (model-supplied or command preview)."""
    if tc_args:
        for key in ("purpose", "label", "description", "title"):
            v = tc_args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:120]
    c = (command or "").strip()
    if len(c) > 72:
        return c[:72] + "…"
    return c or "(bash)"


@dataclass
class AgentEvent:
    type: str  # "text" | "tool_start" | "tool_result" | "error" | "status" | "done" | "worker_released"
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
        worker_dir: str | None = None,
        allowed_tool_names: frozenset[str] | None = None,
        allowed_model_names: frozenset[str] | None = None,
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
        self.worker_pool = WorkerPool(work_dir=worker_dir or "/tmp/codeagent-worker")
        self._last_shell_output: str = ""
        self._run_category: str = ""
        self.allowed_tool_names = allowed_tool_names
        self.allowed_model_names = allowed_model_names

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        self._last_shell_output = ""
        self.session.add_user(user_message)
        sid = getattr(self.session, "id", "unknown")
        log.info("run start session=%s msg='%s'", sid, user_message.replace("\n", " ")[:180])

        if self.allowed_model_names is not None:
            for llm, label in (
                (self.llm_main, "main"),
                (self.llm_fast, "fast"),
                (self.llm_opus, "opus"),
            ):
                if llm and llm.model not in self.allowed_model_names:
                    yield AgentEvent(
                        type="error",
                        content=(
                            f"Model '{llm.model}' ({label}) is not allowed for your account. "
                            "An admin can update your groups in Admin."
                        ),
                    )
                    yield AgentEvent(type="done")
                    return

        category = await self.router.classify(user_message)
        self._run_category = category
        log.info("classified session=%s category=%s", sid, category)
        yield AgentEvent(type="status", content=f"category:{category}")

        tool_names = self.router.get_tools(category)
        if category in ("coding", "system", "ebs"):
            mcp_tools = self._select_mcp_tools_for_message(user_message)
            for t in mcp_tools:
                if t not in tool_names:
                    tool_names.append(t)
        if self.allowed_tool_names is not None:
            tool_names = [t for t in tool_names if t in self.allowed_tool_names]
        log.info("tools active session=%s count=%s", sid, len(tool_names))

        if not tool_names:
            async for event in self._simple_response(user_message):
                yield event
            return

        if category == "simple" and not self._wants_web_tools(user_message):
            async for event in self._simple_response(user_message):
                yield event
            return

        # Only web_search/web_fetch left (typical restricted user) but message is not a web lookup:
        # do not enter tool loop — require_tool_call would otherwise force a bogus search.
        if tool_names and all(t in ("web_search", "web_fetch") for t in tool_names):
            if not self._wants_web_tools(user_message):
                async for event in self._simple_response(user_message):
                    yield event
                return

        prompt_tool_names = tool_names
        skills_ctx = self.skills_context
        system_prompt = self.prompt_builder.build_system(
            category, self.registry, prompt_tool_names, skills_ctx
        )
        if self.allowed_tool_names is not None:
            allow_txt = ", ".join(sorted(self.allowed_tool_names))
            system_prompt += (
                "\n\n## Account tool restriction\n"
                "You may ONLY use <tool_call> for tools in this exact set: "
                f"{allow_txt}. "
                "Never output a tool_call for SSH, bash, remote servers, or any name not in that set — "
                "even if the user asks; explain you only have the listed tools."
            )

        if category == "ebs" and self.llm_opus:
            llm = self.llm_opus
        else:
            llm = self.llm_main
        force_tool_note = self._force_tool_note(user_message, category)
        require_tool_call = self._requires_tool_call(user_message, category)
        image_task = self._is_image_task(user_message)
        stream_text = True
        tool_retry_count = 0
        tool_phase_done = False
        last_successful_signature = ""
        finish_now = False
        finish_text = ""
        last_tool_name = ""
        last_tool_result = ""
        last_generated_file_abs = ""
        fast_finalize_after_tool = category in ("system", "coding") and not image_task
        finalize_after_tool = False

        for iteration in range(self.max_iterations):
            if self._cancelled:
                yield AgentEvent(type="status", content="Cancelled")
                break

            finalize_after_tool = False

            history = self.session.get_history()
            messages = self.prompt_builder.build_messages(system_prompt, history)
            if force_tool_note:
                messages.append({"role": "system", "content": force_tool_note})

            full_text = ""
            llm_stats = None
            max_tokens = None
            if require_tool_call:
                # Fast tool-call planning: avoid long prose generations.
                llm_cap = int(getattr(llm, "max_output", 4096) or 4096)
                max_tokens = max(192, min(900, llm_cap))
            blend_temp = float(self.temperature)
            try:
                async for chunk in llm.stream_chat(
                    messages,
                    max_tokens=max_tokens,
                    temperature=blend_temp,
                    repeat_penalty=self.repeat_penalty,
                    top_p=self.top_p,
                ):
                    if self._cancelled:
                        break
                    if chunk.type == "text":
                        full_text += chunk.content
                        if stream_text:
                            yield AgentEvent(type="text_delta", content=chunk.content)
                    elif chunk.type == "done" and chunk.stats:
                        llm_stats = chunk.stats
            except asyncio.TimeoutError:
                yield AgentEvent(
                    type="error",
                    content="Model request timed out. Check llama.cpp or retry with a shorter prompt.",
                )
                break
            except Exception as e:
                err_type = type(e).__name__
                detail = str(e).strip()
                err_msg = f"Model request failed ({err_type})."
                if "timeout" in err_type.lower():
                    err_msg += " Model timed out; retry with a shorter prompt or try again."
                elif detail:
                    err_msg += f" {detail[:220]}"
                yield AgentEvent(type="error", content=err_msg)
                break

            if self._cancelled:
                yield AgentEvent(type="status", content="Cancelled")
                break

            tool_calls = self._extract_tool_calls(full_text)
            if tool_calls:
                log.info(
                    "tool calls extracted session=%s iter=%s calls=%s",
                    sid, iteration + 1, [name for name, _ in tool_calls]
                )
            else:
                log.info("no tool calls session=%s iter=%s", sid, iteration + 1)

            if not tool_calls:
                if require_tool_call and not tool_phase_done:
                    tool_retry_count += 1
                    max_tc_retries = MAX_TOOL_CALL_RETRIES
                    if tool_retry_count <= max_tc_retries:
                        yield AgentEvent(
                            type="status",
                            content=(
                                f"Generating actionable tool call... retry {tool_retry_count}/{max_tc_retries}"
                            ),
                        )
                        force_tool_note = (
                            "You MUST call at least one real tool now. "
                            "Do not output prose, plans, or fake command snippets. "
                            "Return exactly one valid <tool_call>{...}</tool_call> for the next concrete step."
                        )
                        continue
                    yield AgentEvent(
                        type="error",
                        content="Could not produce a valid tool call quickly. Please retry with a shorter first step.",
                    )
                    break
                if not tool_calls:
                    clean_text = self._clean_response(full_text)
                    clean_text = self._strip_fake_approval_prompts(clean_text)
                    if not clean_text.strip() and last_tool_result:
                        clean_text = await self._summary_from_fast_model(last_tool_name, last_tool_result)
                    self.session.add_assistant(clean_text)
                    yield AgentEvent(type="text", content=clean_text)
                    if llm_stats:
                        yield AgentEvent(type="stats", metadata=llm_stats)
                    break

            if tool_calls:
                force_tool_note = ""
                tool_retry_count = 0
                self.session.add_assistant(full_text if (full_text or "").strip() else "(tool call)")
                if llm_stats:
                    yield AgentEvent(type="stats", metadata=llm_stats)
                for tc_name, tc_args in tool_calls:
                    if self._cancelled:
                        break

                    tc_args = self._replace_session_placeholders(tc_args)
                    tc_args = self._fix_placeholder_paths(tc_name, tc_args)
                    sig_obj = {"name": tc_name, "arguments": tc_args}
                    tc_signature = json.dumps(sig_obj, sort_keys=True, ensure_ascii=False)
                    if tc_signature == last_successful_signature:
                        result_str = "Skipped duplicate tool call (already executed successfully in this turn)."
                        yield AgentEvent(
                            type="tool_result",
                            tool_name=tc_name,
                            content=result_str,
                        )
                        self.session.add_tool_result(tc_name, result_str)
                        last_tool_name = tc_name
                        last_tool_result = result_str
                        tool_phase_done = True
                        continue

                    if not self.registry.get(tc_name):
                        result_str = f"Unknown or unavailable tool: {tc_name}"
                        yield AgentEvent(type="tool_result", tool_name=tc_name, content=result_str)
                        self.session.add_tool_result(tc_name, result_str)
                        last_tool_name = tc_name
                        last_tool_result = result_str
                        tool_phase_done = True
                        continue

                    if self.allowed_tool_names is not None and tc_name not in self.allowed_tool_names:
                        result_str = (
                            "Tool not permitted for your account. "
                            f"Allowed tools: {', '.join(sorted(self.allowed_tool_names))}."
                        )
                        yield AgentEvent(type="tool_result", tool_name=tc_name, content=result_str)
                        self.session.add_tool_result(tc_name, result_str)
                        last_tool_name = tc_name
                        last_tool_result = result_str
                        tool_phase_done = True
                        continue

                    # Read-only web tools: no secrets; waiting on approval makes weather/search feel "stuck".
                    approved = tc_name in ("web_search", "web_fetch")
                    approval_patch: dict[str, Any] = {}
                    if not approved:
                        yield AgentEvent(
                            type="tool_approval",
                            tool_name=tc_name,
                            tool_args=_redact_tool_args_for_ui(tc_args),
                        )

                        try:
                            raw_ap = await asyncio.wait_for(
                                self.approval_queue.get(), timeout=120
                            )
                        except asyncio.TimeoutError:
                            approved = False
                        else:
                            if isinstance(raw_ap, dict):
                                approved = bool(raw_ap.get("approved"))
                                p = raw_ap.get("patch")
                                approval_patch = p if isinstance(p, dict) else {}
                            else:
                                approved = bool(raw_ap)
                                approval_patch = {}

                    if self._cancelled or not approved:
                        result_str = "Denied by user"
                        yield AgentEvent(
                            type="tool_result",
                            tool_name=tc_name,
                            content=result_str,
                        )
                        self.session.add_tool_result(tc_name, result_str)
                        last_tool_name = tc_name
                        last_tool_result = result_str
                        tool_phase_done = True
                        continue

                    if approved and approval_patch:
                        tc_args = dict(tc_args)
                        tc_args.update(approval_patch)

                    if tc_name == "bash":
                        command = tc_args.get("command", "")
                        ssh_block = ssh_interactive_only_message(command)
                        if ssh_block:
                            result_str = ssh_block
                            yield AgentEvent(
                                type="tool_result",
                                tool_name=tc_name,
                                content=result_str,
                            )
                            self.session.add_tool_result(tc_name, result_str)
                            last_tool_name = tc_name
                            last_tool_result = result_str
                            tool_phase_done = True
                            continue
                        if last_generated_file_abs:
                            command = self._rewrite_bash_for_saved_file(command, last_generated_file_abs)
                        if image_task:
                            command = self._normalize_image_command(command)
                            tc_args = dict(tc_args)
                            tc_args["command"] = command
                        elif command != tc_args.get("command", ""):
                            tc_args = dict(tc_args)
                            tc_args["command"] = command
                        slot = self.worker_pool.create()
                        if slot is None:
                            cap = WorkerPool.MAX_WORKERS
                            cap_msg = f"{cap} parallel workers" if cap is not None else "worker pool"
                            result_str = f"Max {cap_msg} reached. Wait for one to finish."
                            yield AgentEvent(type="error", content=result_str)
                        else:
                            wid, worker = slot
                            tab_desc = _bash_worker_tab_description(command, tc_args)
                            yield AgentEvent(
                                type="worker_start",
                                metadata={"worker_id": wid, "description": tab_desc},
                            )
                            yield AgentEvent(type="worker_cmd", content=command, metadata={"worker_id": wid})

                            output_lines = []
                            try:
                                try:
                                    async for line in worker.execute(command):
                                        if self._cancelled:
                                            await worker.kill()
                                            break
                                        output_lines.append(line)
                                        yield AgentEvent(type="worker_output", content=line, metadata={"worker_id": wid})
                                except Exception as ex:
                                    output_lines.append(f"[bash worker error] {ex}")
                            finally:
                                ec = worker.exit_code if worker.exit_code is not None else -1
                                result_str = "\n".join(output_lines)
                                if ec != 0:
                                    result_str += f"\n[exit_code: {ec}]"
                                if len(result_str) > 4000:
                                    result_str = result_str[:4000] + "\n... (truncated)"
                                self._last_shell_output = result_str
                                yield AgentEvent(type="worker_done", metadata={"worker_id": wid, "exit_code": ec})
                                await self.worker_pool.release(wid)
                                yield AgentEvent(type="worker_released", metadata={"worker_id": wid})
                                log.info("tool done session=%s tool=bash exit=%s", sid, ec)
                                if ec == 0:
                                    last_successful_signature = tc_signature
                                    if image_task:
                                        finish_now = True
                                        img_path = self._extract_image_path_from_text(result_str)
                                        if img_path:
                                            finish_text = (
                                                "Image created successfully.\n"
                                                f"Path: {img_path}\n"
                                                + result_str
                                            )
                                        else:
                                            finish_text = "Image created successfully.\n" + result_str
                                    elif fast_finalize_after_tool:
                                        finalize_after_tool = True
                    else:
                        yield AgentEvent(
                            type="tool_start",
                            tool_name=tc_name,
                            tool_args=_redact_tool_args_for_ui(tc_args),
                        )
                        try:
                            result = await self.registry.execute(tc_name, tc_args)
                            result_str = result if isinstance(result, str) else json.dumps(result, default=str)
                            if len(result_str) > 4000:
                                result_str = result_str[:4000] + "\n... (truncated)"
                        except Exception as e:
                            result_str = f"Error: {e}"
                        else:
                            if _registry_tool_execution_succeeded(tc_name, result_str):
                                last_successful_signature = tc_signature
                        log.info("tool done session=%s tool=%s", sid, tc_name)

                    yield AgentEvent(
                        type="tool_result",
                        tool_name=tc_name,
                        content=result_str,
                    )
                    self.session.add_tool_result(tc_name, result_str)
                    last_tool_name = tc_name
                    last_tool_result = result_str
                    tool_phase_done = True
                if finish_now:
                    out = finish_text.strip() or "Done."
                    self.session.add_assistant(out)
                    yield AgentEvent(type="text", content=out)
                    break
                if finalize_after_tool and last_tool_result:
                    out = await self._summary_from_fast_model(last_tool_name, last_tool_result)
                    self.session.add_assistant(out)
                    yield AgentEvent(type="text", content=out)
                    break
        else:
            if last_tool_result:
                out = await self._summary_from_fast_model(last_tool_name, last_tool_result)
                self.session.add_assistant(out)
                yield AgentEvent(type="text", content=out)
            else:
                yield AgentEvent(type="error", content="Max tool iterations reached")

        yield AgentEvent(type="done")

    async def _simple_response(self, message: str):
        msg_lower = message.lower()
        is_greeting = len(message) < 30 and not any(
            w in msg_lower for w in ("draft", "write", "email", "letter", "explain", "summarize", "translate")
        )
        # Fast mode: greetings/simple chat should prefer the fast model.
        if is_greeting:
            llm = self.llm_fast or self.llm_main
        else:
            llm = self.llm_opus or self.llm_main
        max_tok = 200 if is_greeting else 1000
        temp = 0.2 if is_greeting else 0.4

        resp = await llm.chat(
            messages=[
                {"role": "system", "content": SIMPLE_RESPONSE_SYSTEM},
                {"role": "user", "content": message},
            ],
            max_tokens=max_tok,
            temperature=temp,
        )
        text = resp.get("content") or ""
        self.session.add_assistant(text)
        yield AgentEvent(type="text", content=text)
        if resp.get("stats"):
            yield AgentEvent(type="stats", metadata=resp["stats"])
        yield AgentEvent(type="done")

    def _wants_web_tools(self, message: str) -> bool:
        m = message.lower()
        keys = (
            "http://", "https://", "www.", ".com", ".org", "search ",
            "google", "duckduckgo", "weather", "mausam", "news ",
            "fetch ", "url ", "website", "online ", "look up", "lookup",
            "find online", "internet par", "web par", "latest ", "price of",
            "kya chal raha", "headlines",
        )
        return any(k in m for k in keys)

    def _select_mcp_tools_for_message(self, message: str) -> list[str]:
        """Registered MCP tools (Blender MCP removed from this product build)."""
        _ = message
        return [t for t in self.registry.list_tools() if t.startswith("mcp_")]

    def _extract_pseudo_xml_tool_calls(self, text: str) -> list[tuple[str, dict]]:
        """
        Some models emit a pseudo-XML form instead of JSON, e.g.:
        <tool_call>write_file
        <arg_key>path</arg_key><arg_value>/path/to/file.py</arg_value>
        <arg_key>content</arg_key><arg_value>...</arg_value></tool_call>
        """
        out: list[tuple[str, dict]] = []
        lower = text
        idx = 0
        opener = "<tool_call>"
        closer = "</tool_call>"
        while True:
            start = lower.find(opener, idx)
            if start == -1:
                break
            i = start + len(opener)
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tool_name = text[i:j].strip()
            if not tool_name:
                idx = start + len(opener)
                continue
            if not self.registry.get(tool_name):
                idx = start + len(opener)
                continue
            end_lower = lower.find(closer, j)
            if end_lower == -1:
                break
            block = text[j:end_lower]
            args: dict = {}
            pair_re = re.compile(
                r"<arg_key>\s*([^<]+?)\s*</arg_key>\s*<arg_value>(.*?)</arg_value>",
                re.DOTALL | re.IGNORECASE,
            )
            for m in pair_re.finditer(block):
                k = m.group(1).strip()
                v = m.group(2)
                args[k] = v
            if args:
                out.append((tool_name, args))
            idx = end_lower + len(closer)
        return out

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
        if not calls:
            for obj in self._extract_json_objects(text):
                name = obj.get("name", "")
                args = obj.get("arguments", {})
                if name and self.registry.get(name):
                    calls.append((name, args if isinstance(args, dict) else {}))
                    break
        if not calls:
            calls = self._extract_pseudo_xml_tool_calls(text)
        return calls

    def _extract_json_objects(self, text: str) -> list[dict]:
        objs: list[dict] = []
        start = 0
        while True:
            idx = text.find("{", start)
            if idx == -1:
                break
            depth = 0
            in_str = False
            esc = False
            for j in range(idx, len(text)):
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            block = text[idx : j + 1]
                            try:
                                obj = json.loads(block)
                                if isinstance(obj, dict) and "name" in obj and "arguments" in obj:
                                    objs.append(obj)
                            except Exception:
                                pass
                            start = j + 1
                            break
            else:
                break
        return objs

    def _fix_placeholder_paths(self, tool_name: str, args: dict) -> dict:
        if tool_name not in ("read_file", "edit_file", "write_file"):
            return args
        path = args.get("path") or args.get("file_path") or ""
        if not path or ("<" not in path and ">" not in path):
            return args
        resolved = self._path_from_last_shell_output(path)
        if not resolved:
            return args
        out = dict(args)
        out["path"] = resolved
        return out

    def _replace_session_placeholders(self, obj):
        sid = getattr(self.session, "id", "") or "session"
        if isinstance(obj, str):
            return (
                obj.replace("<session_id>", sid)
                .replace("{session_id}", sid)
                .replace("${session_id}", sid)
            )
        if isinstance(obj, list):
            return [self._replace_session_placeholders(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._replace_session_placeholders(v) for k, v in obj.items()}
        return obj

    def _session_workspace_root(self) -> str:
        root = str(self.config.session.get("workspace_dir", "/opt/codeagent/workspaces")).rstrip("/")
        sid = getattr(self.session, "id", "") or "session"
        return f"{root}/{sid}"

    def _rewrite_bash_for_saved_file(self, command: str, saved_abs_path: str) -> str:
        cmd = str(command or "")
        abs_path = str(saved_abs_path or "").strip()
        if not cmd or not abs_path or "/" not in abs_path:
            return cmd
        base = abs_path.rsplit("/", 1)[-1]
        if not base or base not in cmd:
            return cmd
        pat = rf"(?<![\w./-]){re.escape(base)}(?![\w./-])"
        return re.sub(pat, f'"{abs_path}"', cmd)

    def _path_from_last_shell_output(self, bad_path: str) -> str:
        text = self._last_shell_output
        if not text:
            return ""
        want_yaml = "yaml" in bad_path.lower() or "config" in bad_path.lower()
        pat = r"/(?:opt|home|root|etc|var|tmp|usr)(?:/[\w.\-]+)+\.ya?ml"
        yaml_paths = re.findall(pat, text, re.I)
        if want_yaml and yaml_paths:
            return yaml_paths[-1]
        if yaml_paths:
            return yaml_paths[-1]
        pat2 = r"/(?:opt|home|root|etc|var|tmp|usr)(?:/[\w.\-]+)+"
        paths = re.findall(pat2, text)
        paths = [p for p in paths if not p.endswith(":") and len(p) > 1]
        if paths:
            return paths[-1]
        return ""

    def _clean_response(self, text: str) -> str:
        text = TOOL_CALL_RE.sub("", text)
        text = CODE_BLOCK_CALL_RE.sub("", text)
        text = BARE_JSON_CALL_RE.sub("", text)
        text = re.sub(r"</?(?:tools?|tool_call)>", "", text)
        text = re.sub(r"</?tool_response[^>]*>", "", text)
        return text.strip()

    def _strip_fake_approval_prompts(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(r"(?im)^\s*next step:\s*approve for commit\??\s*$", "", text)
        text = re.sub(r"(?im)^\s*approve for commit\??\s*$", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _extract_code_blocks(self, text: str) -> list[tuple[str, str]]:
        """Extract code blocks as (language, code) tuples."""
        pattern = r'```(\w+)?\n(.*?)```'
        matches = re.findall(pattern, text, re.DOTALL)
        return [(lang or "", code.strip()) for lang, code in matches]

    def _auto_write_code_blocks(self, text: str) -> str:
        """Auto-write code blocks to files when no tool calls found."""
        import os
        code_blocks = self._extract_code_blocks(text)
        if not code_blocks:
            return text

        ws = os.environ.get("CODEAGENT_WS", "/tmp/codeagent-workspace")
        os.makedirs(ws, exist_ok=True)

        written_files = []
        for lang, code in code_blocks:
            if len(code) < 10 or len(code) > 5000:
                continue
            ext = ".py" if lang in ("python", "py") else f".{lang}" if lang else ".txt"
            filepath = os.path.join(ws, f"generated{ext}")
            counter = 1
            while os.path.exists(filepath):
                filepath = os.path.join(ws, f"generated{counter}{ext}")
                counter += 1
            try:
                with open(filepath, "w") as f:
                    f.write(code)
                written_files.append(filepath)
            except Exception:
                pass

        if written_files:
            return f"\n\n[Auto-created files: {', '.join(written_files)}]"
        return text

    def _requires_tool_call(self, message: str, category: str) -> bool:
        if category in ("coding", "system", "ebs"):
            return True
        if category == "simple" and self._wants_web_tools(message):
            return True
        m = message.lower()
        keywords = (
            "create", "build", "run", "execute", "fix", "deploy", "install",
            "verify", "check", "show", "list", "find", "read", "write", "edit",
            "mkdir", "touch", "git ", "commit", "push", "service", "systemctl",
        )
        return any(k in m for k in keywords)

    def _force_tool_note(self, message: str, category: str) -> str:
        """Generate a forced tool note for file operations."""
        m = message.lower()
        file_keywords = ("write", "create", "save", "make", "generate", "build")
        if category == "coding" and any(k in m for k in file_keywords):
            return (
                "CRITICAL: You MUST use write_file tool to create files. "
                "Do not output code as text - use the tool. "
                'Example: {tool_call_start}{"name": "write_file", "arguments": {"path": "/tmp/file.py", "content": "print(1)"}}{tool_call_end}}'
            )
        return ""

    def _is_image_task(self, message: str) -> bool:
        m = message.lower()
        keys = (
            "image", "logo", "design", "mockup", "png", "jpg", "jpeg",
            "gif", "webp", "svg", "thumbnail", "resize image", "create image",
            "generate image",
        )
        return any(k in m for k in keys)

    def _extract_image_path_from_text(self, text: str) -> str:
        if not text:
            return ""
        m = re.search(r"(/(?:opt|home|root|tmp|var)[^\s\"']+\.(?:png|jpg|jpeg|gif|webp|svg))", text, re.I)
        if m:
            return m.group(1)
        m2 = re.search(r"([A-Za-z0-9_.\-\/]+\.(?:png|jpg|jpeg|gif|webp|svg))", text, re.I)
        return m2.group(1) if m2 else ""

    def _normalize_image_command(self, command: str) -> str:
        cmd = (command or "").strip()
        if not cmd:
            return cmd
        # Force image outputs into the current session workspace root for reliable preview/download.
        repl = r"$WS/\1"
        pat_abs = r"/opt/codeagent/workspaces/[^\s\"']*?([A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|gif|webp|svg))"
        cmd2 = re.sub(pat_abs, repl, cmd, flags=re.I)
        if cmd2 != cmd and "WS=" not in cmd2:
            cmd2 = 'WS="$(dirname "$PWD")"; ' + cmd2
        return cmd2

    def _auto_summary_from_tool(self, tool_name: str, result: str) -> str:
        text = (result or "").strip()
        if not text:
            return "Task complete."
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        ec = ""
        for ln in lines:
            m = re.search(r"\[exit_code:\s*(-?\d+)\]", ln, re.I)
            if m:
                ec = m.group(1)
                break
        preview = []
        for ln in lines:
            if ln.lower().startswith("[exit_code:"):
                continue
            preview.append(ln)
            if len(preview) >= 4:
                break
        body = "\n".join(preview) if preview else "No output."
        status = f"exit_code={ec}" if ec else "completed"
        name = tool_name or "tool"
        return f"Done ({name}, {status}).\n{body}"

    async def _summary_from_fast_model(self, tool_name: str, result: str) -> str:
        fallback = self._auto_summary_from_tool(tool_name, result)
        llm = self.llm_fast or self.llm_main
        if not llm:
            return fallback
        text = (result or "").strip()
        if len(text) > 1400:
            text = text[:1400] + "\n... (truncated)"
        try:
            resp = await asyncio.wait_for(
                llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a concise technical assistant. "
                                "Summarize tool execution in 1-2 short lines. "
                                "Mention success/failure and key outputs only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Tool: {tool_name or 'tool'}\nResult:\n{text}",
                        },
                    ],
                    max_tokens=60,
                    temperature=0.1,
                ),
                timeout=3.0,
            )
            out = (resp.get("content") or "").strip()
            return out if out else fallback
        except Exception:
            return fallback
