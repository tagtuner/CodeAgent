from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import AsyncIterator

from core.config import Config
from core.llm import LLMClient, Chunk
from core.prompt import PromptBuilder, SIMPLE_RESPONSE_SYSTEM
from core.session import Session
from core.router import Router
from core.worker import WorkerPool
from tools.base import ToolRegistry

TOOL_CALL_RE = re.compile(r"<(?:tool_call|tools)>\s*(\{.*?\})\s*</(?:tool_call|tools)>", re.DOTALL)
CODE_BLOCK_CALL_RE = re.compile(r'```(?:json)?\s*(\{\s*"name"\s*:.*?\})\s*```', re.DOTALL)
BARE_JSON_CALL_RE = re.compile(r'^\s*(\{\s*"name"\s*:.*?"arguments"\s*:\s*\{.*?\}\s*\})\s*$', re.DOTALL | re.MULTILINE)
MAX_TOOL_CALL_RETRIES = 2
log = logging.getLogger("codeagent.agent")


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
        self._blender_probe_last_ts: float = 0.0
        self._blender_probe_last_ok: bool = True
        self._blender_probe_target: str = "localhost:9876"

    async def run(self, user_message: str) -> AsyncIterator[AgentEvent]:
        self._last_shell_output = ""
        self.session.add_user(user_message)
        sid = getattr(self.session, "id", "unknown")
        log.info("run start session=%s msg='%s'", sid, user_message.replace("\n", " ")[:180])

        category = await self.router.classify(user_message)
        log.info("classified session=%s category=%s", sid, category)
        yield AgentEvent(type="status", content=f"category:{category}")

        tool_names = self.router.get_tools(category)
        # Include discovered MCP tools for practical categories, otherwise they stay unreachable.
        if category in ("coding", "system", "ebs"):
            mcp_tools = self._select_mcp_tools_for_message(user_message)
            for t in mcp_tools:
                if t not in tool_names:
                    tool_names.append(t)
            if (
                ("mcp_blender_" in user_message.lower() or "viewport" in user_message.lower())
                and not any(t.startswith("mcp_blender_") for t in mcp_tools)
            ):
                yield AgentEvent(
                    type="status",
                    content=(
                        "Blender MCP addon is not reachable at "
                        f"{self._blender_probe_target}; using local blender tool instead."
                    ),
                )
        log.info("tools active session=%s count=%s", sid, len(tool_names))

        if not tool_names:
            async for event in self._simple_response(user_message):
                yield event
            return

        if category == "simple" and not self._wants_web_tools(user_message):
            async for event in self._simple_response(user_message):
                yield event
            return

        system_prompt = self.prompt_builder.build_system(
            category, self.registry, tool_names, self.skills_context
        )

        # Fast mode: keep coding/system on main model; reserve opus only for EBS-heavy flows.
        llm = self.llm_opus if self.llm_opus and category == "ebs" else self.llm_main
        force_tool_note = ""
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
            async for chunk in llm.stream_chat(
                messages,
                max_tokens=max_tokens,
                temperature=self.temperature,
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
                    if tool_retry_count <= MAX_TOOL_CALL_RETRIES:
                        yield AgentEvent(
                            type="status",
                            content=f"Generating actionable tool call... retry {tool_retry_count}/{MAX_TOOL_CALL_RETRIES}",
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
                clean_text = self._clean_response(full_text)
                clean_text = self._strip_fake_approval_prompts(clean_text)
                if not clean_text.strip() and last_tool_result:
                    clean_text = await self._summary_from_fast_model(last_tool_name, last_tool_result)
                self.session.add_assistant(clean_text)
                yield AgentEvent(type="text", content=clean_text)
                if llm_stats:
                    yield AgentEvent(type="stats", metadata=llm_stats)
                break
            else:
                force_tool_note = ""
                tool_retry_count = 0
                self.session.add_assistant(full_text)
                if llm_stats:
                    yield AgentEvent(type="stats", metadata=llm_stats)
                for tc_name, tc_args in tool_calls:
                    if self._cancelled:
                        break

                    tc_args = self._fix_placeholder_paths(tc_name, tc_args)
                    if tc_name == "blender":
                        tc_args = self._normalize_blender_args(tc_args)
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
                        last_tool_name = tc_name
                        last_tool_result = result_str
                        tool_phase_done = True
                        continue

                    if tc_name == "bash":
                        command = tc_args.get("command", "")
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
                            tool_args=tc_args,
                        )
                        try:
                            result = await self.registry.execute(tc_name, tc_args)
                            result_str = result if isinstance(result, str) else json.dumps(result, default=str)
                            if len(result_str) > 4000:
                                result_str = result_str[:4000] + "\n... (truncated)"
                        except Exception as e:
                            result_str = f"Error: {e}"
                        else:
                            if tc_name == "blender" and "python_script not found" in result_str.lower():
                                force_tool_note = (
                                    "The blender python_script path does not exist. "
                                    "Create the script first using write_file with valid Python from user design requirements, "
                                    "then call blender again."
                                )
                            elif tc_name == "blender" and "blend_file not found" in result_str.lower():
                                force_tool_note = (
                                    "The blender blend_file path does not exist. "
                                    "Either create it first (save .blend via script) or run blender without blend_file if appropriate."
                                )
                            else:
                                last_successful_signature = tc_signature
                            if tc_name == "blender":
                                saved_abs = self._extract_saved_file_from_blender_result(result_str, tc_args)
                                if saved_abs:
                                    last_generated_file_abs = saved_abs
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
        text = resp["content"]
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
        """Keep MCP tool set focused; large tool lists significantly slow tool planning."""
        all_mcp = [t for t in self.registry.list_tools() if t.startswith("mcp_")]
        if not all_mcp:
            return []
        msg = message.lower()

        if "blender" in msg or "mcp_blender_" in msg or "viewport" in msg:
            if not self._is_blender_addon_reachable():
                # Avoid guaranteed MCP failures when addon socket is not up.
                return []
            core = [
                "mcp_blender_get_scene_info",
                "mcp_blender_get_object_info",
                "mcp_blender_get_viewport_screenshot",
                "mcp_blender_execute_blender_code",
            ]
            opt = []
            if any(k in msg for k in ("polyhaven", "hdri", "texture", "asset")):
                opt += [
                    "mcp_blender_get_polyhaven_categories",
                    "mcp_blender_search_polyhaven_assets",
                    "mcp_blender_download_polyhaven_asset",
                    "mcp_blender_set_texture",
                    "mcp_blender_get_polyhaven_status",
                ]
            if any(k in msg for k in ("sketchfab", "model preview")):
                opt += [
                    "mcp_blender_get_sketchfab_status",
                    "mcp_blender_search_sketchfab_models",
                    "mcp_blender_get_sketchfab_model_preview",
                    "mcp_blender_download_sketchfab_model",
                ]
            if any(k in msg for k in ("hyper3d", "rodin")):
                opt += [
                    "mcp_blender_get_hyper3d_status",
                    "mcp_blender_generate_hyper3d_model_via_text",
                    "mcp_blender_generate_hyper3d_model_via_images",
                    "mcp_blender_poll_rodin_job_status",
                    "mcp_blender_import_generated_asset",
                ]
            if "hunyuan" in msg:
                opt += [
                    "mcp_blender_get_hunyuan3d_status",
                    "mcp_blender_generate_hunyuan3d_model",
                    "mcp_blender_poll_hunyuan_job_status",
                    "mcp_blender_import_generated_asset_hunyuan",
                ]
            selected = [t for t in (core + opt) if t in all_mcp]
            return selected or [t for t in all_mcp if t.startswith("mcp_blender_")][:4]

        return all_mcp

    def _is_blender_addon_reachable(self, cache_ttl_sec: float = 6.0) -> bool:
        """
        Check Blender addon socket availability (default localhost:9876).
        Cached briefly to avoid repeated socket probes in a single run.
        """
        now = time.time()
        if (now - self._blender_probe_last_ts) < cache_ttl_sec:
            return self._blender_probe_last_ok

        host = "localhost"
        port = 9876
        try:
            for srv in (self.config.mcp_servers or []):
                if str(srv.get("name", "")).lower() != "blender":
                    continue
                env = srv.get("env") or {}
                host = str(env.get("BLENDER_HOST", host))
                port = int(env.get("BLENDER_PORT", port))
                break
        except Exception:
            pass

        self._blender_probe_target = f"{host}:{port}"
        ok = False
        try:
            with socket.create_connection((host, port), timeout=0.35):
                ok = True
        except Exception:
            ok = False

        self._blender_probe_last_ts = now
        self._blender_probe_last_ok = ok
        return ok

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

    def _session_workspace_root(self) -> str:
        root = str(self.config.session.get("workspace_dir", "/opt/codeagent/workspaces")).rstrip("/")
        sid = getattr(self.session, "id", "") or "session"
        return f"{root}/{sid}"

    def _normalize_blender_args(self, args: dict) -> dict:
        """Normalize blender args: resolve placeholders, stabilize output path, ensure render flags."""
        out = dict(args or {})
        sid = getattr(self.session, "id", "") or "session"

        for key in ("blend_file", "python_script"):
            v = out.get(key)
            if isinstance(v, str) and v:
                out[key] = (
                    v.replace("<session_id>", sid)
                    .replace("{session_id}", sid)
                    .replace("${session_id}", sid)
                )

        extras = out.get("extra_args")
        if not isinstance(extras, list) or not extras:
            return out
        norm = list(extras)
        has_output = False
        has_frame_flag = False
        has_format_flag = False
        out_idx = -1

        for i, tok in enumerate(norm):
            s = str(tok)
            if s == "-o" and i + 1 < len(norm):
                has_output = True
                out_idx = i + 1
            elif s in ("-f", "-a"):
                has_frame_flag = True
            elif s == "-F":
                has_format_flag = True

        if out_idx != -1:
            val = str(norm[out_idx] or "")
            val = (
                val.replace("<session_id>", sid)
                .replace("{session_id}", sid)
                .replace("${session_id}", sid)
            )
            if val.startswith("//"):
                ws = self._session_workspace_root()
                val = f"{ws}/{val[2:].lstrip('/')}"
            # Blender appends frame number if no #; enforce predictable naming.
            if "#" not in val:
                low = val.lower()
                for ext in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr"):
                    if low.endswith(ext):
                        val = val[: -len(ext)]
                        break
                val = val + "####"
            norm[out_idx] = val

        if has_output and not has_format_flag:
            norm.extend(["-F", "PNG"])
        if has_output and not has_frame_flag:
            norm.extend(["-f", "1"])
        out["extra_args"] = norm
        return out

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

    def _extract_saved_file_from_blender_result(self, result: str, tc_args: dict) -> str:
        """Resolve Blender 'Saved:' output into an absolute file path when possible."""
        text = str(result or "")
        m = re.search(r"Saved:\s*'([^']+)'", text)
        if not m:
            return ""
        saved = m.group(1).strip()
        if not saved:
            return ""
        if saved.startswith("/"):
            return saved
        out_spec = ""
        extras = tc_args.get("extra_args") if isinstance(tc_args, dict) else None
        if isinstance(extras, list):
            for i in range(len(extras) - 1):
                if str(extras[i]) == "-o":
                    out_spec = str(extras[i + 1] or "").strip()
                    break
        if out_spec.startswith("/"):
            base_dir = out_spec.rsplit("/", 1)[0]
            if base_dir:
                return f"{base_dir}/{saved.rsplit('/', 1)[-1]}"
        if out_spec.startswith("//"):
            ws = self._session_workspace_root()
            return f"{ws}/{saved.rsplit('/', 1)[-1]}"
        return f"{self._session_workspace_root()}/{saved.rsplit('/', 1)[-1]}"

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

    def _requires_tool_call(self, message: str, category: str) -> bool:
        if category in ("coding", "system", "ebs"):
            return True
        m = message.lower()
        keywords = (
            "create", "build", "run", "execute", "fix", "deploy", "install",
            "verify", "check", "show", "list", "find", "read", "write", "edit",
            "mkdir", "touch", "git ", "commit", "push", "service", "systemctl",
        )
        return any(k in m for k in keywords)

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
