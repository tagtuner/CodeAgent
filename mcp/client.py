from __future__ import annotations
import json
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict
    server_name: str


@dataclass
class MCPServer:
    name: str
    transport: str  # "stdio" | "sse"
    command: str = ""
    url: str = ""
    env: dict = field(default_factory=dict)
    tools: list[MCPTool] = field(default_factory=list)
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _request_id: int = field(default=0, repr=False)


class MCPClient:
    """Minimal MCP client supporting stdio and SSE transports."""

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self._http = httpx.AsyncClient(timeout=30)

    async def connect_stdio(self, name: str, command: str, env: dict | None = None) -> MCPServer:
        parts = command.split()
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        server = MCPServer(name=name, transport="stdio", command=command, _proc=proc)
        self.servers[name] = server

        await self._jsonrpc_stdio(server, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "CodeAgent", "version": "1.0"},
        })
        await self._jsonrpc_stdio(server, "notifications/initialized", {}, notify=True)
        tools_resp = await self._jsonrpc_stdio(server, "tools/list", {})
        for t in tools_resp.get("tools", []):
            server.tools.append(MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("inputSchema", {}),
                server_name=name,
            ))
        log.info(f"MCP stdio [{name}]: {len(server.tools)} tools discovered")
        return server

    async def connect_sse(self, name: str, url: str) -> MCPServer:
        server = MCPServer(name=name, transport="sse", url=url.rstrip("/"))
        self.servers[name] = server
        resp = await self._http.post(f"{server.url}/initialize", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "CodeAgent", "version": "1.0"},
            },
        })
        resp.raise_for_status()
        await self._http.post(f"{server.url}/initialized", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        tools_resp = await self._http.post(f"{server.url}/tools/list", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        tools_data = tools_resp.json().get("result", {})
        for t in tools_data.get("tools", []):
            server.tools.append(MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("inputSchema", {}),
                server_name=name,
            ))
        log.info(f"MCP SSE [{name}]: {len(server.tools)} tools discovered")
        return server

    async def call_tool(self, server_name: str, tool_name: str, args: dict) -> Any:
        server = self.servers.get(server_name)
        if not server:
            return f"MCP server '{server_name}' not found"
        if server.transport == "stdio":
            result = await self._jsonrpc_stdio(server, "tools/call", {
                "name": tool_name, "arguments": args,
            })
        else:
            result = await self._jsonrpc_sse(server, "tools/call", {
                "name": tool_name, "arguments": args,
            })
        content_parts = result.get("content", [])
        texts = [p.get("text", "") for p in content_parts if p.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result, default=str)

    async def _jsonrpc_stdio(
        self, server: MCPServer, method: str, params: dict, notify: bool = False
    ) -> dict:
        server._request_id += 1
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"] = server._request_id
        line = json.dumps(msg) + "\n"
        server._proc.stdin.write(line.encode())
        await server._proc.stdin.drain()
        if notify:
            return {}
        data = await self._read_jsonrpc_response(server, timeout=30)
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

    async def _read_jsonrpc_response(self, server: MCPServer, timeout: int = 30) -> dict:
        """Read MCP stdio response, skipping non-JSON/noise lines from wrappers."""
        deadline = asyncio.get_running_loop().time() + timeout
        last_noise = ""
        while True:
            remain = deadline - asyncio.get_running_loop().time()
            if remain <= 0:
                if last_noise:
                    raise RuntimeError(f"MCP timeout waiting JSON response. Last output: {last_noise[:180]}")
                raise asyncio.TimeoutError("MCP stdio response timeout")
            raw = await asyncio.wait_for(server._proc.stdout.readline(), timeout=remain)
            if not raw:
                err = ""
                try:
                    if server._proc.stderr:
                        err_raw = await asyncio.wait_for(server._proc.stderr.readline(), timeout=0.25)
                        err = err_raw.decode(errors="replace").strip()
                except Exception:
                    pass
                if err:
                    raise RuntimeError(f"MCP server closed stream. stderr: {err}")
                raise RuntimeError("MCP server closed stream without response")
            txt = raw.decode(errors="replace").strip()
            if not txt:
                continue
            if not txt.startswith("{"):
                last_noise = txt
                continue
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                last_noise = txt
                continue

    async def _jsonrpc_sse(self, server: MCPServer, method: str, params: dict) -> dict:
        resp = await self._http.post(f"{server.url}/{method.replace('/', '_')}", json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result", {})

    async def disconnect_all(self):
        for server in self.servers.values():
            if server.transport == "stdio" and server._proc:
                try:
                    server._proc.terminate()
                    await asyncio.wait_for(server._proc.wait(), timeout=5)
                except Exception:
                    try:
                        server._proc.kill()
                    except Exception:
                        pass
        self.servers.clear()
        await self._http.aclose()
