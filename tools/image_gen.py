from __future__ import annotations

import os
import random
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from tools.base import BaseTool
from core.config import Config

# Pollinations image model id for "Flux Schnell" (see GET /v1/models → `flux`).
# Intentionally fixed: this tool must not call any other Pollinations image model.
POLLINATIONS_FLUX_SCHNELL_MODEL = "flux"
_DEFAULT_BASE = "https://gen.pollinations.ai"


def get_latest_workspace() -> Path:
    ws_dir = Path("/opt/codeagent/workspaces")
    if not ws_dir.exists():
        ws_dir.mkdir(parents=True, exist_ok=True)
    sessions = [d for d in ws_dir.iterdir() if d.is_dir() and d.name != "default"]
    if not sessions:
        default_dir = ws_dir / "default"
        default_dir.mkdir(exist_ok=True)
        return default_dir
    sessions.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return sessions[0]


def _expand_env_ref(value: str) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value or ""


def _pollinations_secrets_file_candidates() -> list[Path]:
    """Optional key file paths (chmod 0600); not committed."""
    repo_root = Path(__file__).resolve().parents[1]
    return [
        Path("/opt/codeagent/secrets/pollinations.env"),
        repo_root / "secrets" / "pollinations.env",
    ]


def _parse_pollinations_key_from_dotenv(contents: str) -> str:
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if not line.upper().startswith("POLLINATIONS_API_KEY"):
            continue
        if "=" not in line:
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        return value
    return ""


def _pollinations_key_from_secrets_file() -> str:
    for candidate in _pollinations_secrets_file_candidates():
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        key = _parse_pollinations_key_from_dotenv(text)
        if key:
            return key
    return ""


def _pollinations_settings(cfg: Config) -> tuple[str, str]:
    poll: dict = {}
    if isinstance(cfg.tools, dict):
        raw = cfg.tools.get("pollinations")
        if isinstance(raw, dict):
            poll = raw
    base = (
        os.environ.get("POLLINATIONS_BASE_URL")
        or str(poll.get("base_url") or "").strip()
        or _DEFAULT_BASE
    ).rstrip("/")
    api_key = (
        os.environ.get("POLLINATIONS_API_KEY")
        or _expand_env_ref(str(poll.get("api_key", "") or ""))
        or _pollinations_key_from_secrets_file()
    )
    return base, api_key


def _ext_from_content_type(ct: str) -> str:
    cl = ct.lower()
    if "jpeg" in cl or "jpg" in cl:
        return ".jpg"
    if "webp" in cl:
        return ".webp"
    if "png" in cl:
        return ".png"
    return ".png"


class ImageGenTool(BaseTool):
    name = "image_generator"
    description = (
        "Generate images from a text prompt via Pollinations (Flux Schnell only). "
        "Uses only the configured Pollinations API key — not the chat LLM backend."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed text description of the image to generate.",
            },
            "num_outputs": {
                "type": "integer",
                "default": 1,
                "description": "Number of distinct images (max 4). Each uses a random seed.",
            },
        },
        "required": ["prompt"],
    }

    async def execute(self, prompt: str, num_outputs: int = 1, **kwargs: object) -> str:
        # Ignore legacy args (e.g. `model`) so OpenRouter IDs never hit Pollinations.
        _ = kwargs
        cfg = Config.load()
        base_url, api_key = _pollinations_settings(cfg)

        if not api_key.strip():
            return (
                "ERROR: Pollinations API key missing. Set POLLINATIONS_API_KEY, "
                "or tools.pollinations.api_key in config, "
                "or `/opt/codeagent/secrets/pollinations.env` (see codeagent/secrets/)."
            )

        if not isinstance(prompt, str) or not prompt.strip():
            return "ERROR: prompt must be non-empty text."

        n = max(1, min(int(num_outputs), 4))
        workspace = get_latest_workspace()
        workspace.mkdir(parents=True, exist_ok=True)
        results: list[str] = []

        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Accept": "image/*,*/*",
        }

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for _ in range(n):
                seed = random.randint(1, 2_147_483_647)
                path_enc = quote(prompt.strip(), safe="")
                req_url = f"{base_url}/image/{path_enc}"
                params = {
                    "model": POLLINATIONS_FLUX_SCHNELL_MODEL,
                    "seed": seed,
                }

                resp = await client.get(req_url, headers=headers, params=params)
                if resp.status_code != 200:
                    return (
                        f"ERROR: Pollinations image API ({resp.status_code}): "
                        f"{resp.text[:2000]!r}"
                    )

                ct = resp.headers.get("content-type") or ""
                ext = _ext_from_content_type(ct)
                filename = f"render_{uuid.uuid4().hex[:8]}{ext}"
                dest = workspace / filename
                dest.write_bytes(resp.content)
                results.append(filename)

        links = [
            f"![Generated image {i + 1}](/api/workspace/file?path={name})"
            for i, name in enumerate(results)
        ]
        return (
            f"Successfully generated {len(results)} image(s) via Pollinations (Flux Schnell):\n"
            + "\n".join(links)
        )
