from __future__ import annotations
import json
import asyncio
import logging
import shutil
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.config import Config
from core.llm import LLMClient
from core.agent import Agent, AgentEvent
from core.session import Session
from core.router import Router
from skills.loader import SkillLoader
from tools.base import ToolRegistry

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("codeagent.web")

_config: Config | None = None
_registry: ToolRegistry | None = None
_llm_main: LLMClient | None = None
_llm_fast: LLMClient | None = None
_llm_opus: LLMClient | None = None
_skills_context: str = ""
_sessions: dict[str, Session] = {}

# Workspace uploads (composer → session uploads/)
_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
_UPLOAD_ALLOWED_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf", ".zip",
    ".txt", ".md", ".json", ".csv", ".blend", ".glb",
})
_cpu_prev_sample: tuple[int, int] | None = None


def _read_linux_cpu_sample() -> tuple[int, int] | None:
    """Return (total_jiffies, idle_jiffies) from /proc/stat."""
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        parts = first.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
        return total, idle
    except Exception:
        return None


def _linux_cpu_percent() -> float | None:
    """
    Compute host CPU usage % from /proc/stat deltas.
    First sample has no prior baseline, so returns None.
    """
    global _cpu_prev_sample
    cur = _read_linux_cpu_sample()
    if not cur:
        return None
    prev = _cpu_prev_sample
    _cpu_prev_sample = cur
    if not prev:
        return None
    total_delta = cur[0] - prev[0]
    idle_delta = cur[1] - prev[1]
    if total_delta <= 0:
        return None
    busy = max(0.0, min(1.0, 1.0 - (idle_delta / total_delta)))
    return round(busy * 100.0, 1)


def _linux_memory_usage() -> dict[str, float | int] | None:
    """Return Linux RAM metrics from /proc/meminfo."""
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            mem[k.strip()] = v.strip()
        total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
        avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
        if total_kb <= 0:
            return None
        used_kb = max(0, total_kb - avail_kb)
        pct = round((used_kb / total_kb) * 100.0, 1)
        return {
            "used_mb": round(used_kb / 1024.0),
            "total_mb": round(total_kb / 1024.0),
            "percent": pct,
        }
    except Exception:
        return None


def _workspace_root(config: Config) -> Path:
    return Path(config.session.get("workspace_dir", "/opt/codeagent/workspaces"))


def _workspace_for_session(config: Config, session_id: str) -> Path:
    safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in ("-", "_")) or "session"
    return _workspace_root(config) / safe_id


def _ensure_workspace(config: Config, session_id: str) -> Path:
    ws = _workspace_for_session(config, session_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _safe_workspace_file(config: Config, session_id: str, rel_path: str) -> Path | None:
    ws = _workspace_for_session(config, session_id).resolve()
    cand = (ws / rel_path).resolve()
    try:
        cand.relative_to(ws)
    except Exception:
        return None
    return cand


def _resolve_workspace_file(config: Config, session_id: str, rel_path: str) -> Path | None:
    p = _safe_workspace_file(config, session_id, rel_path)
    if p and p.exists() and p.is_file():
        return p
    # Fallback: if only basename provided, resolve to latest matching file anywhere in session workspace.
    base = Path(rel_path).name
    if not base:
        return None
    ws = _workspace_for_session(config, session_id)
    if not ws.exists():
        return None
    latest: Path | None = None
    latest_mtime = -1.0
    for m in ws.rglob(base):
        if not m.is_file():
            continue
        try:
            mt = m.stat().st_mtime
        except Exception:
            continue
        if mt > latest_mtime:
            latest_mtime = mt
            latest = m
    return latest


def _sanitize_upload_filename(name: str) -> str:
    base = Path(name).name
    out = []
    for ch in base:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
    s = "".join(out).strip("._")[:120]
    if not s:
        return ""
    suf = Path(s).suffix.lower()
    if not suf or suf not in _UPLOAD_ALLOWED_SUFFIXES:
        return ""
    return s


def _unique_upload_path(uploads_dir: Path, name: str) -> Path:
    p = uploads_dir / name
    if not p.exists():
        return p
    stem, suf = Path(name).stem, Path(name).suffix
    for i in range(2, 10_000):
        cand = uploads_dir / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
    return uploads_dir / f"{stem}_x{name}"


def _validate_client_attachments(config: Config, session_id: str, items: list) -> list[dict]:
    """Only accept paths under uploads/ that exist (client cannot inject arbitrary paths)."""
    if not items:
        return []
    ws = _workspace_for_session(config, session_id).resolve()
    out: list[dict] = []
    for it in items[:24]:
        if not isinstance(it, dict):
            continue
        rel = str(it.get("path", "")).strip().lstrip("/").replace("\\", "/")
        if ".." in rel or not rel.startswith("uploads/"):
            continue
        p = (ws / rel).resolve()
        try:
            p.relative_to(ws)
        except Exception:
            continue
        if p.is_file():
            try:
                sz = p.stat().st_size
            except Exception:
                sz = 0
            out.append({
                "path": rel,
                "name": str(it.get("name") or p.name),
                "size": sz,
            })
    return out


def _triggered_skills_context(config: Config, user_message: str, max_chars: int = 2000) -> str:
    """Inject skill bodies when trigger keywords appear in the user message (per request)."""
    skills = SkillLoader.load_dir(config.skills_dir)
    if not skills:
        return ""
    msg_l = user_message.lower()
    parts: list[str] = []
    total = 0
    for s in skills:
        hit = any(kw.lower() in msg_l for kw in s.trigger_keywords)
        if not hit:
            continue
        block = f"## Skill: {s.name}\n{s.compact}\n"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


def _augment_user_message_with_attachments(user_text: str, attachments: list[dict]) -> str:
    if not attachments:
        return user_text
    lines = [
        "[User attached files — already saved in this chat's workspace. "
        "Bash workers run under .../wN; use WS=\"$(dirname \"$PWD\")\" for workspace root. "
        "Use identify/file/ls on images — do not read_file on binary images.]",
    ]
    for a in attachments:
        lines.append(f"- {a['path']} (original: {a.get('name', '')})")
    lines.append("")
    lines.append(user_text.strip())
    return "\n".join(lines)


def create_app(
    config: Config,
    registry: ToolRegistry,
    skills_context: str = "",
) -> FastAPI:
    global _config, _registry, _llm_main, _llm_fast, _llm_opus, _skills_context
    _config = config
    _registry = registry
    _skills_context = skills_context

    app = FastAPI(title="CodeAgent Web")

    @app.on_event("startup")
    async def startup():
        global _llm_main, _llm_fast, _llm_opus
        _llm_main = LLMClient(config.main_model)
        if "fast" in config.models:
            _llm_fast = LLMClient(config.fast_model)
        if config.opus_model:
            _llm_opus = LLMClient(config.opus_model)

    @app.on_event("shutdown")
    async def shutdown():
        if _llm_main:
            await _llm_main.close()
        if _llm_fast:
            await _llm_fast.close()
        if _llm_opus:
            await _llm_opus.close()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = STATIC_DIR / "index.html"
        return HTMLResponse(html_path.read_text())

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "model": config.main_model.name}

    @app.get("/api/system/metrics")
    async def system_metrics():
        cpu_pct = _linux_cpu_percent()
        mem = _linux_memory_usage()
        load1 = load5 = load15 = None
        try:
            load1, load5, load15 = os.getloadavg()
            load1, load5, load15 = round(load1, 2), round(load5, 2), round(load15, 2)
        except Exception:
            pass
        return {
            "cpu_percent": cpu_pct,
            "ram_percent": (mem or {}).get("percent"),
            "ram_used_mb": (mem or {}).get("used_mb"),
            "ram_total_mb": (mem or {}).get("total_mb"),
            "load_avg_1m": load1,
            "load_avg_5m": load5,
            "load_avg_15m": load15,
        }

    @app.get("/api/tools")
    async def list_tools():
        return {"tools": _registry.list_tools()}

    @app.get("/api/sessions")
    async def list_sessions():
        session_dir = config.session.get("dir", "/opt/codeagent/sessions")
        return {"sessions": Session.list_sessions(session_dir)}

    @app.post("/api/session/new")
    async def new_session():
        max_tok = config.session.get("max_history_tokens", 12000)
        s = Session(max_history_tokens=max_tok)
        _sessions[s.id] = s
        ws = _ensure_workspace(config, s.id)
        return {"session_id": s.id, "workspace": str(ws)}

    @app.get("/api/session/{session_id}")
    async def get_session(session_id: str):
        session_dir = config.session.get("dir", "/opt/codeagent/sessions")
        session_file = Path(session_dir) / f"{session_id}.json"
        if not session_file.exists():
            return JSONResponse({"error": "Session not found"}, status_code=404)
        data = json.loads(session_file.read_text())
        return {"id": data["id"], "messages": data.get("messages", [])}

    @app.delete("/api/session/{session_id}")
    async def delete_session(session_id: str):
        session_dir = config.session.get("dir", "/opt/codeagent/sessions")
        session_file = Path(session_dir) / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
        _sessions.pop(session_id, None)
        ws = _workspace_for_session(config, session_id)
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
        return {"status": "deleted", "id": session_id}

    @app.get("/api/workspace/{session_id}")
    async def workspace_info(session_id: str):
        ws = _ensure_workspace(config, session_id)
        files = []
        for p in sorted(ws.rglob("*")):
            if p.is_file():
                try:
                    rel = p.relative_to(ws).as_posix()
                except Exception:
                    rel = p.name
                files.append(rel)
            if len(files) >= 200:
                break
        return {"session_id": session_id, "workspace": str(ws), "files": files}

    @app.get("/api/workspace/{session_id}/download")
    async def workspace_download(session_id: str):
        ws = _workspace_for_session(config, session_id)
        if not ws.exists():
            return JSONResponse({"error": "Workspace not found"}, status_code=404)
        downloads_dir = Path("/tmp/codeagent-downloads")
        downloads_dir.mkdir(parents=True, exist_ok=True)
        archive_base = downloads_dir / f"{session_id}-workspace"
        archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=str(ws))
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{session_id}-workspace.zip",
        )

    @app.get("/api/workspace/{session_id}/latest-image")
    async def workspace_latest_image(session_id: str):
        ws = _workspace_for_session(config, session_id)
        if not ws.exists():
            return {"session_id": session_id, "path": None}
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        latest: Path | None = None
        latest_mtime = -1.0
        for p in ws.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = p
        if not latest:
            return {"session_id": session_id, "path": None}
        try:
            rel = latest.relative_to(ws).as_posix()
        except Exception:
            rel = latest.name
        return {"session_id": session_id, "path": rel}

    @app.get("/api/workspace/{session_id}/file")
    async def workspace_file(session_id: str, path: str):
        p = _resolve_workspace_file(config, session_id, path)
        if not p:
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(str(p))

    @app.get("/api/workspace/{session_id}/download-file")
    async def workspace_download_file(session_id: str, path: str):
        p = _resolve_workspace_file(config, session_id, path)
        if not p:
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(str(p), filename=p.name)

    @app.post("/api/workspace/{session_id}/upload")
    async def workspace_upload(
        session_id: str,
        files: Annotated[list[UploadFile], File(...)],
    ):
        """Save composer uploads under <workspace>/uploads/ (images + small design files)."""
        ws = _ensure_workspace(config, session_id)
        uploads = ws / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        saved: list[dict] = []
        errors: list[str] = []
        for uf in files[:16]:
            raw_name = uf.filename or ""
            safe = _sanitize_upload_filename(raw_name)
            if not safe:
                errors.append(f"rejected type/name: {raw_name or '(empty)'}")
                await uf.close()
                continue
            dest = _unique_upload_path(uploads, safe)
            size = 0
            try:
                with dest.open("wb") as out:
                    while True:
                        chunk = await uf.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _UPLOAD_MAX_BYTES:
                            raise OSError("file too large")
                        out.write(chunk)
                rel = dest.relative_to(ws).as_posix()
                saved.append({"path": rel, "name": raw_name, "size": size})
            except OSError as e:
                if dest.exists():
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                errors.append(f"{raw_name}: {e}")
            except Exception as e:
                if dest.exists():
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                errors.append(f"{raw_name}: {e}")
            await uf.close()
        return {"ok": bool(saved), "files": saved, "errors": errors}

    @app.websocket("/ws/{session_id}")
    async def ws_chat(websocket: WebSocket, session_id: str):
        await websocket.accept()
        log.info("ws accepted session=%s", session_id)
        session = _sessions.get(session_id)
        if not session:
            max_tok = config.session.get("max_history_tokens", 12000)
            session_dir = config.session.get("dir", "/opt/codeagent/sessions")
            session_file = Path(session_dir) / f"{session_id}.json"
            if session_file.exists():
                session = Session.load(str(session_file), max_history_tokens=max_tok)
            else:
                session = Session(session_id=session_id, max_history_tokens=max_tok)
            _sessions[session_id] = session
        ws_dir = _ensure_workspace(config, session_id)

        agent = Agent(
            config=config,
            llm_main=_llm_main,
            llm_fast=_llm_fast,
            llm_opus=_llm_opus,
            registry=_registry,
            session=session,
            skills_context=_skills_context,
            worker_dir=str(ws_dir),
        )

        input_queue: asyncio.Queue = asyncio.Queue()
        mid_task_queue: asyncio.Queue = asyncio.Queue()

        async def ws_receiver():
            try:
                while True:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "message")
                    if msg_type in ("message", "mid_task_query"):
                        txt = str(msg.get("message") or "").strip().replace("\n", " ")
                        log.info(
                            "ws recv session=%s type=%s len=%s attachments=%s text='%s'",
                            session_id,
                            msg_type,
                            len(txt),
                            len(msg.get("attachments") or []),
                            txt[:140],
                        )
                    else:
                        log.info("ws recv session=%s type=%s", session_id, msg_type)

                    if msg_type == "tool_response":
                        await agent.approval_queue.put(msg.get("approved", False))
                    elif msg_type == "cancel":
                        agent._cancelled = True
                        try:
                            agent.approval_queue.put_nowait(False)
                        except asyncio.QueueFull:
                            pass
                    elif msg_type == "worker_kill":
                        kill_id = msg.get("worker_id")
                        if kill_id and agent.worker_pool:
                            await agent.worker_pool.kill(kill_id)
                            try:
                                await websocket.send_text(
                                    json.dumps({"type": "worker_released", "metadata": {"worker_id": kill_id}})
                                )
                            except Exception:
                                pass
                        elif agent.worker_pool:
                            released = await agent.worker_pool.kill_all()
                            agent._cancelled = True
                            try:
                                agent.approval_queue.put_nowait(False)
                            except asyncio.QueueFull:
                                pass
                            for rid in released:
                                try:
                                    await websocket.send_text(
                                        json.dumps({"type": "worker_released", "metadata": {"worker_id": rid}})
                                    )
                                except Exception:
                                    pass
                    elif msg_type == "mid_task_query":
                        await mid_task_queue.put(msg)
                    else:
                        await input_queue.put(msg)
            except (WebSocketDisconnect, Exception):
                log.info("ws receiver closed session=%s", session_id)
                await input_queue.put(None)
                await mid_task_queue.put(None)

        async def mid_task_handler():
            try:
                while True:
                    msg = await mid_task_queue.get()
                    if msg is None:
                        break
                    user_text = msg.get("message", "")
                    if not user_text:
                        continue

                    buffers = ""
                    if agent.worker_pool:
                        buffers = agent.worker_pool.all_buffers(last_n=20)

                    llm = _llm_fast or _llm_main
                    ctx = f"Active workers output:\n```\n{buffers}\n```" if buffers else "No command currently running."
                    try:
                        resp = await llm.chat(
                            messages=[
                                {"role": "system", "content": f"You are CodeAgent. A worker is running commands. {ctx}\nAnswer the user's question briefly."},
                                {"role": "user", "content": user_text},
                            ],
                            max_tokens=300,
                            temperature=0.7,
                        )
                        await websocket.send_text(json.dumps({
                            "type": "text",
                            "content": resp["content"],
                        }))
                    except Exception as e:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "content": str(e),
                        }))
            except Exception:
                pass

        receiver = asyncio.create_task(ws_receiver())
        mid_handler = asyncio.create_task(mid_task_handler())

        try:
            while True:
                msg = await input_queue.get()
                if msg is None:
                    break

                user_text = (msg.get("message") or "").strip()
                raw_attachments = msg.get("attachments") or []
                validated = _validate_client_attachments(config, session_id, raw_attachments)
                log.info(
                    "queue message session=%s user_len=%s raw_attach=%s valid_attach=%s",
                    session_id, len(user_text), len(raw_attachments), len(validated)
                )
                if not user_text and not validated:
                    log.info("drop empty message session=%s", session_id)
                    continue
                if not user_text and validated:
                    user_text = (
                        "Use the uploaded files in this workspace and complete the design task "
                        "(variants, patterns, or edits as appropriate)."
                    )
                user_text = _augment_user_message_with_attachments(user_text, validated)

                agent._cancelled = False
                agent.approval_queue = asyncio.Queue()
                triggered_skills = _triggered_skills_context(config, user_text)
                base_ctx = (_skills_context or "").strip()
                if triggered_skills:
                    agent.skills_context = (base_ctx + "\n\n" + triggered_skills).strip() if base_ctx else triggered_skills
                else:
                    agent.skills_context = base_ctx
                log.info(
                    "run start session=%s skills_triggered=%s",
                    session_id, bool(triggered_skills)
                )
                try:
                    await websocket.send_text(json.dumps({"type": "workspace", "metadata": {"path": str(ws_dir)}}))
                except Exception:
                    pass

                try:
                    async for event in agent.run(user_text):
                        if agent._cancelled and event.type not in (
                            "status", "done", "worker_done", "worker_released",
                        ):
                            continue

                        payload = {"type": event.type, "content": event.content}
                        if event.tool_name:
                            payload["tool_name"] = event.tool_name
                        if event.tool_args:
                            payload["tool_args"] = event.tool_args
                        if event.metadata:
                            payload["metadata"] = event.metadata
                        try:
                            await websocket.send_text(json.dumps(payload))
                        except Exception:
                            break
                except Exception as e:
                    log.exception("agent.run failed session=%s err=%s", session_id, e)
                    try:
                        await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
                    except Exception:
                        pass
                finally:
                    # Ensures UI unlocks if agent.run errors before yielding done (duplicate done is harmless).
                    try:
                        await websocket.send_text(json.dumps({"type": "done"}))
                    except Exception:
                        pass

                session_dir = config.session.get("dir", "/opt/codeagent/sessions")
                session.save(session_dir)

        except (WebSocketDisconnect, Exception):
            log.info("ws outer loop ended session=%s", session_id)
            pass
        finally:
            receiver.cancel()
            mid_handler.cancel()
            if agent.worker_pool:
                await agent.worker_pool.close_all()
            log.info("ws finalized session=%s", session_id)

    return app
