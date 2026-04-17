#!/usr/bin/env python3
"""
CodeAgent — Local LLM-powered coding assistant.
Replaces OpenCode with an optimized, context-aware agent.

Usage:
    python main.py tui          # Terminal UI
    python main.py web          # Web UI (FastAPI)
    python main.py chat "msg"   # Single-shot CLI chat
"""
from __future__ import annotations
import sys
import asyncio
import logging

from core.config import Config
from core.llm import LLMClient
from core.agent import Agent
from core.session import Session
from tools.base import ToolRegistry
from tools.bash_tool import BashTool
from tools.file_ops import ReadFileTool, WriteFileTool, EditFileTool, GlobSearchTool
from tools.git_tool import GitStatusTool, GitDiffTool, GitCommitTool
from tools.oracle import OracleQueryTool, OracleSchemaTool, SqlValidateTool, OracleExplainTool
from tools.ebs import EBSModuleGuideTool
from tools.web_search import WebSearchTool, WebFetchTool
from mcp.client import MCPClient
from mcp.registry import MCPRegistry
from skills.manager import SkillManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("codeagent")


def build_registry(config: Config) -> ToolRegistry:
    registry = ToolRegistry()
    tool_cfg = config.tools

    if tool_cfg.get("bash", {}).get("enabled", True):
        blocked = tool_cfg.get("bash", {}).get("blocked_commands", [])
        timeout = tool_cfg.get("bash", {}).get("timeout", 120)
        registry.register(BashTool(blocked=blocked, default_timeout=timeout))

    if tool_cfg.get("file_ops", {}).get("enabled", True):
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(EditFileTool())
        registry.register(GlobSearchTool())

    if tool_cfg.get("git", {}).get("enabled", True):
        registry.register(GitStatusTool())
        registry.register(GitDiffTool())
        registry.register(GitCommitTool())

    if tool_cfg.get("oracle", {}).get("enabled", True):
        registry.register(OracleQueryTool())
        registry.register(OracleSchemaTool())
        registry.register(SqlValidateTool())
        registry.register(OracleExplainTool())

    if tool_cfg.get("ebs", {}).get("enabled", True):
        registry.register(EBSModuleGuideTool())

    if tool_cfg.get("web", {}).get("enabled", True):
        registry.register(WebSearchTool())
        registry.register(WebFetchTool())

    return registry


async def connect_mcp(config: Config, registry: ToolRegistry) -> MCPClient:
    client = MCPClient()
    if config.mcp_servers:
        mcp_reg = MCPRegistry(client)
        await mcp_reg.connect_all(config.mcp_servers, registry)
    return client


def run_tui(config: Config, registry: ToolRegistry, skills_ctx: str):
    from ui.tui.app import CodeAgentTUI
    app = CodeAgentTUI(config=config, registry=registry, skills_context=skills_ctx)
    app.run()


def run_web(config: Config, registry: ToolRegistry, skills_ctx: str):
    import uvicorn
    from ui.web.app import create_app
    app = create_app(config=config, registry=registry, skills_context=skills_ctx)
    host = config.web.get("host", "0.0.0.0")
    port = config.web.get("port", 4200)
    log.info(f"Starting CodeAgent Web on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


async def run_chat(config: Config, registry: ToolRegistry, skills_ctx: str, message: str):
    llm_main = LLMClient(config.main_model)
    llm_fast = LLMClient(config.fast_model) if "fast" in config.models else None
    max_tok = config.session.get("max_history_tokens", 12000)
    session = Session(max_history_tokens=max_tok)
    agent = Agent(
        config=config,
        llm_main=llm_main,
        llm_fast=llm_fast,
        registry=registry,
        session=session,
        skills_context=skills_ctx,
    )
    async for event in agent.run(message):
        if event.type == "text":
            print(event.content)
        elif event.type == "tool_start":
            print(f"\n[Tool: {event.tool_name}]", file=sys.stderr)
        elif event.type == "tool_result":
            print(f"[Result: {event.content[:200]}]", file=sys.stderr)
        elif event.type == "error":
            print(f"[Error: {event.content}]", file=sys.stderr)
    await llm_main.close()
    if llm_fast:
        await llm_fast.close()


def main():
    config_path = "/opt/codeagent/config.yaml"
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = sys.argv[idx + 1]

    config = Config.load(config_path)
    registry = build_registry(config)

    skill_mgr = SkillManager(config.skills_dir)
    skills_ctx = skill_mgr.get_context()

    mcp_client = None
    if config.mcp_servers:
        mcp_client = asyncio.get_event_loop().run_until_complete(connect_mcp(config, registry))

    log.info(f"Tools registered: {registry.list_tools()}")
    log.info(f"Skills loaded: {len(skill_mgr.all_skills)}")

    mode = sys.argv[1] if len(sys.argv) > 1 else "tui"

    if mode == "tui":
        run_tui(config, registry, skills_ctx)
    elif mode == "web":
        run_web(config, registry, skills_ctx)
    elif mode == "chat":
        msg = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Hello!"
        asyncio.run(run_chat(config, registry, skills_ctx, msg))
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python main.py [tui|web|chat \"message\"]")
        sys.exit(1)


if __name__ == "__main__":
    main()
