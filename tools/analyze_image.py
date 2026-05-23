from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Any

from tools.base import BaseTool
from core.config import Config, ModelConfig
from core.llm import LLMClient
from core.request_context import SESSION_WORKSPACE

_IMG_EXT = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class AnalyzeImageTool(BaseTool):
    """
    Sends an on-disk workspace image + text question to an OpenRouter vision-capable model.
    (Not img2img / not image editing — Q&A only.)
    """

    name = "analyze_image"
    description = (
        "Answer questions about an **already uploaded** workspace image using a vision model "
        "(Etsy titles, captions, descriptions, colors, OCR-style reading). "
        "Pass the absolute file path printed in the user's attachment hints (typically under uploads/). "
        "Do **not** use bash or identify for this."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to image file in this chat's workspace",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What to answer about the image (Etsy title, colors, OCR, …). "
                    "`query`/`question` are accepted too; if omitted, a sensible default is used."
                ),
            },
        },
        # Only path is strictly required — models often omit or rename `prompt`.
        "required": ["path"],
    }

    def _pick_path_prompt(self, kwargs: dict[str, Any]) -> tuple[str, str]:
        """Models sometimes omit `prompt` or use alternate keys (`query`, etc.)."""
        path_keys = ("path", "file", "image_path", "filepath", "src")
        path_val = ""
        for k in path_keys:
            v = kwargs.get(k)
            if v is not None and str(v).strip():
                path_val = str(v).strip()
                break

        prompt_keys = (
            "prompt",
            "query",
            "question",
            "instruction",
            "instructions",
            "task",
            "text",
            "message",
            "user_prompt",
        )
        prompt_val = ""
        for k in prompt_keys:
            v = kwargs.get(k)
            if v is not None and str(v).strip():
                prompt_val = str(v).strip()
                break

        if not prompt_val:
            prompt_val = (
                "Describe this image for a shopper: main subject, colors, materials, style, "
                "and notable text/logos visible. Then suggest one concise Etsy-style product title."
            )

        return path_val, prompt_val

    async def execute(self, **kwargs: Any) -> str:
        path_raw, prompt = self._pick_path_prompt(kwargs)
        if not path_raw:
            return "ERROR: Missing image path — pass `path` (absolute path from attachment hints)."

        p = Path(path_raw).expanduser()
        root = SESSION_WORKSPACE.get()
        if not root:
            return "ERROR: No session workspace bound (analyze_image runs only inside the web/agent loop)."
        root = Path(root).resolve()
        if not p.is_absolute():
            p = (root / p).resolve()
        else:
            p = p.resolve()
        try:
            p.relative_to(root)
        except ValueError:
            return f"ERROR: Path must stay inside workspace {root}"

        if not p.is_file():
            return f"ERROR: Not a file: {p}"

        ext = p.suffix.lower()
        if ext not in _IMG_EXT:
            return f"ERROR: Unsupported image type {ext}"

        cfg = Config.load()
        acfg = cfg.tools.get("analyze_image") if isinstance(cfg.tools, dict) else {}
        acfg = acfg if isinstance(acfg, dict) else {}

        max_b = int(acfg.get("max_bytes") or acfg.get("max_bytes_limit") or 12_582_912)
        try:
            size = p.stat().st_size
        except OSError as e:
            return f"ERROR: cannot stat file: {e}"
        if size > max_b:
            return f"ERROR: Image too large ({size} bytes, max {max_b}). Resize or compress first."

        try:
            raw = p.read_bytes()
        except OSError as e:
            return f"ERROR: cannot read file: {e}"

        mime = _MIME.get(ext, "application/octet-stream")
        data_uri = "data:{};base64,{}".format(mime, base64.standard_b64encode(raw).decode("ascii"))

        model_name_override = str(acfg.get("model") or acfg.get("vision_model") or "").strip()

        mc: ModelConfig
        pref = str(acfg.get("prefer") or "").lower().strip()
        if pref == "fast" and "fast" in cfg.models:
            mc = cfg.fast_model
        else:
            mc = cfg.main_model

        api_model_id = model_name_override or mc.name
        vc = replace(mc, name=api_model_id)
        cli = LLMClient(vc, timeout=min(240.0, float(acfg.get("timeout", 120) or 120)))

        vision_system = (
            "You describe and answer strictly from the user's image plus their question. "
            "Be factual; if unsure, say so. No hallucinated brand names unless visible."
        )

        payload_user = [
            {"type": "text", "text": str(prompt).strip()},
            {
                "type": "image_url",
                "image_url": {"url": data_uri},
            },
        ]

        try:
            out = await cli.chat(
                messages=[
                    {"role": "system", "content": vision_system},
                    {"role": "user", "content": payload_user},
                ],
                temperature=float(cfg.agent.get("temperature", 0.3)),
                top_p=float(cfg.agent.get("top_p", 0.9)),
                repeat_penalty=float(cfg.agent.get("repeat_penalty", 1.0)),
                model=api_model_id,
            )
            text = str(out.get("content") or "").strip()
            if not text:
                return "ERROR: Vision model returned empty content."
            return text
        except Exception as e:
            return f"ERROR: Vision request failed ({type(e).__name__}): {e}"
        finally:
            await cli.close()
