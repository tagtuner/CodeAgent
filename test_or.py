"""Smoke test Pollinations image tool (requires POLLINATIONS_API_KEY in env)."""

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from tools.image_gen import ImageGenTool  # noqa: E402


async def main() -> None:
    if not os.environ.get("POLLINATIONS_API_KEY"):
        print("Set POLLINATIONS_API_KEY in the environment first.")
        sys.exit(2)
    prompt = "A small glowing crystal on a wooden table, soft window light, shallow depth of field"
    out = await ImageGenTool().execute(prompt=prompt, num_outputs=1)
    print(out)


if __name__ == "__main__":
    asyncio.run(main())
