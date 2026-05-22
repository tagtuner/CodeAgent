# Long-term memory — CodeAgent × Pollinations (image tool)

_Last updated context for Omnitech / CodeAgent. **No secrets in this file.**_

## Product intent

- **Chat:** OpenRouter (or configured LLM); user-facing model stays for **text/tool reasoning only**.
- **Images:** Dedicated **`image_generator`** tool → Pollinations **`gen.pollinations.ai`**, **`model=flux`** (Flux Schnell). One locked image backend; conversation chat model IDs must never be sent as Pollinations `model`.
- Blender / script-generation pipeline for images was removed; Pollinations-only.

## Canonical paths & service

| Item | Typical value |
|------|----------------|
| Deploy root | `/opt/codeagent/` |
| Image tool implementation | `/opt/codeagent/tools/image_gen.py` |
| Pollinations secrets file | `/opt/codeagent/secrets/pollinations.env` |
| systemd unit | `codeagent-web.service` |
| Deploy host | `root@172.30.3.206` (Omnitech lab) |

Web UI port may vary behind nginx (`:4200` in README vs `:8083` in some installs); same app.

## Key storage (never in git)

1. Prefer **`POLLINATIONS_API_KEY`** in systemd Environment or Drop-In.
2. Or **`/opt/codeagent/secrets/pollinations.env`** with `chmod 600` / directory `chmod 700`.
3. Optional `config.yaml`: `tools.pollinations.api_key: ${POLLINATIONS_API_KEY}`.

If a key appeared in Slack/Chat/issue — **assume compromised** → rotate on enter.pollinations.ai.

## Behaviour verified in production-style test

- User approves **`image_generator`** in web UI → tool hits Pollinations → workspace file `render_*.jpg`/`png` → preview + download wired in UI.
- Pollinations dashboard should show Flux Schnell + API key fingerprint / activity.

## Related files

| File | Role |
|------|------|
| `tools/image_gen.py` | Pollinations HTTP client + workspace save + markdown links |
| `core/agent.py` | Must **not** inject chat `model` into `image_generator` |
| `secrets/pollinations.env` | Gitignored optional local/server key file |
| `test_or.py` | Env-only smoke script |
| `README.md` | High-level Pollinations section |
