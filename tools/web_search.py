from __future__ import annotations
import asyncio
import json
import re
from urllib.parse import quote_plus
from .base import BaseTool

try:
    import httpx
except ImportError:
    httpx = None


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. After getting results, use web_fetch on the best URL to get full content."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, max_results: int = 5) -> str:
        if not httpx:
            return "Error: httpx not installed"
        max_results = min(max_results, 10)
        try:
            return await self._ddg_search(query, max_results)
        except Exception as e:
            return f"Search error: {e}"

    async def _ddg_search(self, query: str, max_results: int) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            resp.raise_for_status()
            return self._parse_html(resp.text, max_results)

    def _parse_html(self, html: str, max_results: int) -> str:
        results = []
        blocks = re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        )
        for url, title, snippet in blocks[:max_results]:
            url = re.sub(r'//duckduckgo\.com/l/\?uddg=', '', url)
            url = url.split("&rut=")[0]
            from urllib.parse import unquote
            url = unquote(url)
            title = re.sub(r"<[^>]+>", "", title).strip()
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            if title and url:
                results.append(f"**{title}**\n{url}\n{snippet}\n")

        if not results:
            return "No results found."
        return f"Found {len(results)} results:\n\n" + "\n".join(results)


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "Fetch a URL and return its content as readable text (HTML stripped). Useful for reading documentation, articles, or API responses."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 6000)"},
        },
        "required": ["url"],
    }

    async def execute(self, url: str, max_chars: int = 6000) -> str:
        if not httpx:
            return "Error: httpx not installed"
        max_chars = min(max_chars, 15000)
        try:
            return await self._fetch(url, max_chars)
        except Exception as e:
            return f"Fetch error: {e}"

    async def _fetch(self, url: str, max_chars: int) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        }
        async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    data = resp.json()
                    text = json.dumps(data, indent=2, ensure_ascii=False)
                except Exception:
                    text = resp.text
            else:
                text = self._html_to_text(resp.text)

            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n... (truncated)"
            return f"[Fetched: {url}]\n\n{text}"

    def _html_to_text(self, html: str) -> str:
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<header[^>]*>.*?</header>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

        html = re.sub(r"<(h[1-6])[^>]*>(.*?)</\1>", r"\n\n## \2\n", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(p|div|br|tr|li)[^>]*/?>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 (\1)", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(b|strong)[^>]*>(.*?)</\1>", r"**\2**", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(code)[^>]*>(.*?)</\1>", r"`\2`", html, flags=re.DOTALL | re.IGNORECASE)

        html = re.sub(r"<[^>]+>", "", html)

        import html as html_mod
        text = html_mod.unescape(html)

        lines = [line.strip() for line in text.splitlines()]
        cleaned = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    cleaned.append("")
                    prev_blank = True
            else:
                cleaned.append(line)
                prev_blank = False

        return "\n".join(cleaned).strip()
