from __future__ import annotations
import re
from .llm import LLMClient

TOOL_MAP: dict[str, list[str]] = {
    "simple": ["web_search", "web_fetch"],
    "coding": [
        "bash",
        "ssh_remote",
        "read_file",
        "write_file",
        "edit_file",
        "glob_search",
        "web_search",
        "web_fetch",
        "image_generator",
    ],
    "ebs": ["bash", "ebs_module_guide", "ebs_concurrent_status", "oracle_query", "oracle_schema", "sql_validate", "oracle_explain", "web_search"],
    "system": [
        "bash",
        "ssh_remote",
        "read_file",
        "write_file",
        "git_status",
        "git_diff",
        "git_commit",
        "web_search",
        "web_fetch",
    ],
}

CLASSIFY_PROMPT = """\
Classify the user message into exactly one category. Reply with ONLY the category name, nothing else.

Categories:
- simple: greetings, general questions, explanations, web searches, looking up information
- coding: writing code, scripts, files, debugging, programming tasks; design/image work including uploaded attachments (patterns, variants, ImageMagick)
- ebs: Oracle EBS, SQL queries, database tables, PO/AP/AR/GL/INV modules, suppliers, invoices
- system: server administration, git, services, disk, network, system commands; VoIP/SIP (Asterisk/FreeSWITCH/FusionPBX), trunk/outgoing call **log** analysis; also workers/processes on THIS server (often CodeAgent bash worker tabs W1, W2, …, not OpenPAI/Kubernetes unless user names them)

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
        r"git\s|git\b|commit|push|pull|reboot|cron|rsync|backup|workers?|active\s+worker|"
        r"freeswitch|asterisk|fusionpbx|kamailio|pjsip|voip\b|sip[\s_-]?trunk|outgoing|inbound|cdr)\b",
        re.IGNORECASE,
    ),
    "coding": re.compile(
        r"\b(write\s+a?\s*(script|function|class|program|code|file)|"
        r"debug|refactor|implement|create\s+a?\s*(file|script)|python|bash|"
        r"javascript|typescript|html|css|api|endpoint|parse|regex|"
        r"blender|blender\s*mcp|viewport|\.blend|"
        r"image|logo|design|mockup|png|jpg|jpeg|gif|webp|svg|draw|"
        r"upload|uploaded|attachment|variation|pattern|variants?|"
        r"generate\s+(an?\s+)?image|create\s+(an?\s+)?image|resize|thumbnail)\b",
        re.IGNORECASE,
    ),
}


class Router:
    def __init__(self, llm_fast: LLMClient | None = None):
        self.llm_fast = llm_fast

    async def classify(self, message: str, model: str | None = None) -> str:
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
                model=model,
            )
            c_text = resp.get("content") or ""
            c_text = c_text.strip() if isinstance(c_text, str) else ""
            if not c_text:
                return "coding"
            cat = c_text.lower().split()[0]
            cat = cat.strip(".,!:;\"'")
            if cat in TOOL_MAP:
                return cat
        except Exception:
            pass
        return "coding"

    def _keyword_classify(self, message: str) -> str | None:
        msg_lower = message.lower().strip()

        # Explicit tool invocation style should never route to simple.
        if "tool_call" in msg_lower:
            return "coding"

        # Force image/design generation requests into coding so tools are available.
        image_words = (
            "image", "logo", "design", "mockup", "png", "jpg", "jpeg", "gif",
            "webp", "svg", "thumbnail", "draw",
        )
        action_words = (
            "create", "generate", "make", "build", "edit", "resize", "convert",
        )
        if any(w in msg_lower for w in image_words) and any(v in msg_lower for v in action_words):
            return "coding"

        if any(w in msg_lower for w in ("upload", "uploaded", "attachment")) and any(
            w in msg_lower for w in (
                "design", "pattern", "variant", "image", "logo", "mockup", "banner",
                "convert", "resize", "imagemagick",
            )
        ):
            return "coding"

        # VoIP/SIP/trunk log diagnosis must keep bash / ssh_remote (not "simple").
        voip_hints = (
            "outgoing call", "outbound", "sip", "voip", "asterisk", "freeswitch", "fusionpbx",
            "kamailio", "pjsip", "sip trunk", "trunk registration", "cdr",
        )
        diag_hints = ("log", "analyze", "analyse", "check", "tail", "grep", "issue", "error", "fail")
        if any(h in msg_lower for h in voip_hints) and any(h in msg_lower for h in diag_hints):
            return "system"

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
                "mcp_", "tool",
                "sip", "voip", "asterisk", "freeswitch", "trunk", "outgoing", "log",
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
