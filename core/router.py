from __future__ import annotations
import re
from .llm import LLMClient

TOOL_MAP: dict[str, list[str]] = {
    "simple": ["web_search", "web_fetch"],
    "coding": ["bash", "read_file", "write_file", "edit_file", "glob_search", "web_search", "web_fetch"],
    "ebs": ["bash", "ebs_module_guide", "oracle_query", "oracle_schema", "sql_validate", "oracle_explain", "web_search"],
    "system": ["bash", "read_file", "write_file", "git_status", "git_diff", "git_commit", "web_search", "web_fetch"],
}

CLASSIFY_PROMPT = """\
Classify the user message into exactly one category. Reply with ONLY the category name, nothing else.

Categories:
- simple: greetings, general questions, explanations, web searches, looking up information
- coding: writing code, scripts, files, debugging, programming tasks
- ebs: Oracle EBS, SQL queries, database tables, PO/AP/AR/GL/INV modules, suppliers, invoices
- system: server administration, git, services, disk, network, system commands

User message: {message}

Category:"""

KEYWORD_PATTERNS = {
    "ebs": re.compile(
        r"\b(oracle|ebs|sql|select\s|po_header|ap_invoice|ar_|gl_|inv_|vendor|supplier|"
        r"purchase.order|invoice|receipt|journal|ledger|mtl_|fnd_|hr_all|requisition)\b",
        re.IGNORECASE,
    ),
    "system": re.compile(
        r"\b(systemctl|journalctl|nginx|firewall|disk|mount|nfs|ssh|service|"
        r"git\s|git\b|commit|push|pull|reboot|cron|rsync|backup)\b",
        re.IGNORECASE,
    ),
    "coding": re.compile(
        r"\b(write\s+a?\s*(script|function|class|program|code|file)|"
        r"debug|refactor|implement|create\s+a?\s*(file|script)|python|bash|"
        r"javascript|typescript|html|css|api|endpoint|parse|regex)\b",
        re.IGNORECASE,
    ),
}


class Router:
    def __init__(self, llm_fast: LLMClient | None = None):
        self.llm_fast = llm_fast

    async def classify(self, message: str) -> str:
        kw_result = self._keyword_classify(message)
        if kw_result:
            return kw_result

        if not self.llm_fast:
            return "coding"

        try:
            resp = await self.llm_fast.chat(
                messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(message=message[:300])}],
                max_tokens=10,
                temperature=0.1,
            )
            cat = resp["content"].strip().lower().split()[0] if resp["content"] else "coding"
            cat = cat.strip(".,!:;\"'")
            if cat in TOOL_MAP:
                return cat
        except Exception:
            pass
        return "coding"

    def _keyword_classify(self, message: str) -> str | None:
        msg_lower = message.lower().strip()

        simple_patterns = (
            "email", "letter", "draft", "translate", "summarize", "summary",
            "explain", "what is", "what are", "how does", "define", "meaning",
            "tell me", "describe", "compare", "difference between",
            "thank you", "thanks", "hi ", "hello", "hey", "good morning",
            "good night", "bye", "help me write", "rewrite", "paraphrase",
        )
        if any(p in msg_lower for p in simple_patterns):
            if not any(w in msg_lower for w in (
                "sql", "oracle", "ebs", "select ", "table", "server",
                "systemctl", "nginx", "bash", "script", "function", "file",
            )):
                return "simple"

        if len(msg_lower) < 20 and not any(
            w in msg_lower for w in ("write", "create", "run", "show", "list", "get", "find", "fix")
        ):
            return "simple"

        for cat, pattern in KEYWORD_PATTERNS.items():
            if pattern.search(message):
                return cat
        return None

    def get_tools(self, category: str) -> list[str]:
        return TOOL_MAP.get(category, TOOL_MAP["coding"])
