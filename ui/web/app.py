from __future__ import annotations
import json
import asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import Config
from core.llm import LLMClient
from core.agent import Agent, AgentEvent
from core.session import Session
from core.router import Router
from tools.base import ToolRegistry

STATIC_DIR = Path(__file__).parent / "static"

_config: Config | None = None
_registry: ToolRegistry | None = None
_llm_main: LLMClient | None = None
_llm_fast: LLMClient | None = None
_skills_context: str = ""
_sessions: dict[str, Session] = {}


def create_app(
    config: Config,
    registry: ToolRegistry,
    skills_context: str = "",
) -> FastAPI:
    global _config, _registry, _llm_main, _llm_fast, _skills_context
    _config = config
    _registry = registry
    _skills_context = skills_context

    app = FastAPI(title="CodeAgent Web")

    @app.on_event("startup")
    async def startup():
        global _llm_main, _llm_fast
        _llm_main = LLMClient(config.main_model)
        if "fast" in config.models:
            _llm_fast = LLMClient(config.fast_model)

    @app.on_event("shutdown")
    async def shutdown():
        if _llm_main:
            await _llm_main.close()
        if _llm_fast:
            await _llm_fast.close()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = STATIC_DIR / "index.html"
        return HTMLResponse(html_path.read_text())

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "model": config.main_model.name}

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
        return {"session_id": s.id}

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
        return {"status": "deleted", "id": session_id}

    @app.websocket("/ws/{session_id}")
    async def ws_chat(websocket: WebSocket, session_id: str):
        await websocket.accept()
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

        agent = Agent(
            config=config,
            llm_main=_llm_main,
            llm_fast=_llm_fast,
            registry=_registry,
            session=session,
            skills_context=_skills_context,
        )

        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                user_text = msg.get("message", "")
                if not user_text:
                    continue

                async for event in agent.run(user_text):
                    payload = {"type": event.type, "content": event.content}
                    if event.tool_name:
                        payload["tool_name"] = event.tool_name
                    if event.tool_args:
                        payload["tool_args"] = event.tool_args
                    if event.metadata:
                        payload["metadata"] = event.metadata
                    await websocket.send_text(json.dumps(payload))

                session_dir = config.session.get("dir", "/opt/codeagent/sessions")
                session.save(session_dir)

        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
            except Exception:
                pass

    return app
