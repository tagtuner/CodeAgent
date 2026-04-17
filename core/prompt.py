from __future__ import annotations
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.base import ToolRegistry

from tools.oracle import get_available_dbs

SYSTEM_BASE = """\
You are CodeAgent, a senior software engineer running on a local server.
You write clean, production-ready code. Be direct and concise.
When you need to perform an action, call the appropriate tool.
Never fabricate tool results — always call the tool first."""

TOOL_PREAMBLE = """
# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tool_defs}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": "function_name", "arguments": {{"param": "value"}}}}
</tool_call>

IMPORTANT: After receiving a tool result, analyze it and provide a clear answer to the user. Do NOT repeat tool definitions or your own instructions."""

WEB_HINT = """
When using web tools: ALWAYS call web_fetch on the most relevant URL after web_search to get actual content. Never respond with placeholder text like "[Not provided]" — fetch the data first, then summarize it."""

CATEGORY_HINTS = {
    "simple": "\nYou can search the web and fetch URLs to answer questions with real data." + WEB_HINT,
    "coding": "\nYou are in coding mode. You can run commands, read/write files, use git, and search the web for documentation." + WEB_HINT,
    "ebs": "\nYou are in Oracle EBS mode. Use the EBS tools to query tables and generate SQL. Always use ebs_module_guide first to understand table structures before writing SQL.\n{ebs_db_hint}",
    "system": "\nYou are in system administration mode. Run commands to diagnose and fix issues. You can search the web for solutions." + WEB_HINT,
}


class PromptBuilder:
    def build_system(
        self,
        category: str,
        registry: ToolRegistry,
        tool_names: list[str] | None = None,
        skills_context: str = "",
    ) -> str:
        parts = [SYSTEM_BASE]
        hint = CATEGORY_HINTS.get(category, "")
        if hint:
            if "{ebs_db_hint}" in hint:
                dbs = get_available_dbs()
                if dbs:
                    db_hint = f"Available database connections: {', '.join(dbs)}. Use the 'db' parameter in Oracle tools to specify which database. If user doesn't specify, ask which database to use."
                else:
                    db_hint = ""
                hint = hint.replace("{ebs_db_hint}", db_hint)
            parts.append(hint)
        if skills_context:
            parts.append(f"\n# Active Skills\n{skills_context}")
        if tool_names:
            defs = self._build_tool_defs(registry, tool_names)
            if defs:
                parts.append(TOOL_PREAMBLE.format(tool_defs=defs))
        return "\n".join(parts)

    def _build_tool_defs(self, registry: ToolRegistry, names: list[str]) -> str:
        lines = []
        for name in names:
            tool = registry.get(name)
            if not tool:
                continue
            spec = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            lines.append(json.dumps(spec, separators=(",", ":")))
        return "\n".join(lines)

    def build_messages(
        self,
        system_prompt: str,
        history: list[dict],
    ) -> list[dict]:
        return [{"role": "system", "content": system_prompt}] + history
