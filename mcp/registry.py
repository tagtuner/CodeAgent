from __future__ import annotations
import logging
from typing import Any

from .client import MCPClient, MCPTool
from tools.base import BaseTool, ToolRegistry

log = logging.getLogger(__name__)


class MCPToolWrapper(BaseTool):
    """Wraps an MCP tool as a BaseTool so it can be used in the ToolRegistry."""

    def __init__(self, mcp_tool: MCPTool, mcp_client: MCPClient):
        self.name = f"mcp_{mcp_tool.server_name}_{mcp_tool.name}"
        self.description = mcp_tool.description
        self.parameters = mcp_tool.parameters
        self._mcp_tool = mcp_tool
        self._client = mcp_client

    async def execute(self, **kwargs) -> Any:
        return await self._client.call_tool(
            self._mcp_tool.server_name,
            self._mcp_tool.name,
            kwargs,
        )


class MCPRegistry:
    """Discovers MCP servers from config and registers their tools."""

    def __init__(self, mcp_client: MCPClient):
        self.client = mcp_client

    async def connect_all(self, mcp_configs: list[dict], registry: ToolRegistry):
        for cfg in mcp_configs:
            name = cfg.get("name", "unknown")
            try:
                if "command" in cfg:
                    server = await self.client.connect_stdio(
                        name, cfg["command"], cfg.get("env")
                    )
                elif "url" in cfg:
                    server = await self.client.connect_sse(name, cfg["url"])
                else:
                    log.warning(f"MCP server '{name}': no command or url specified")
                    continue

                for tool in server.tools:
                    wrapper = MCPToolWrapper(tool, self.client)
                    registry.register(wrapper)
                    log.info(f"Registered MCP tool: {wrapper.name}")

            except Exception as e:
                log.error(f"Failed to connect MCP server '{name}': {e}")
