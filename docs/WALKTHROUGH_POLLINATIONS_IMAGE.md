# Walkthrough — Pollinations image tool (deploy → test → rotate)

Operational steps for Omnitech engineers. **Do not paste real API keys into tickets, chats, or committed files.**

## 1. One-time alignment

- Confirm Pollinations dashboard allows **Flux Schnell** (`flux`) for your API tier.
- On the server create secrets directory:

```bash
mkdir -p /opt/codeagent/secrets
chmod 700 /opt/codeagent/secrets
```

## 2. Place the Pollinations key (pick one approach)

### A. Secrets file (matches current CodeAgent loader)

Edit on server (nano/vi):

```bash
nano /opt/codeagent/secrets/pollinations.env
```

Single line format:

```
POLLINATIONS_API_KEY=your-key-here
```

Then lock it down:

```bash
chmod 600 /opt/codeagent/secrets/pollinations.env
```

### B. systemd Environment (no file)

Add `Environment=POLLINATIONS_API_KEY=...` via unit override / drop-in **without** committing to git — same security discipline as SSH keys.

## 3. Deploy code paths (from admin workstation)

Omnitech lab host **172.30.3.206** (`root` SSH). From your local clone root (`CodeAgent/`):

```bash
scp secrets/pollinations.env root@172.30.3.206:/opt/codeagent/secrets/pollinations.env
scp tools/image_gen.py root@172.30.3.206:/opt/codeagent/tools/image_gen.py
ssh root@172.30.3.206 "chmod 600 /opt/codeagent/secrets/pollinations.env 2>/dev/null; systemctl restart codeagent-web; systemctl is-active codeagent-web"
```

## 4. Web UI sanity test

1. Open CodeAgent in the browser (your nginx/front URL).
2. Send a tiny prompt like: **“Minimal flat vector icon — blue laptop, white background.”**
3. When **`image_generator`** appears → **Approve**.
4. Expect: success text + **`render_*.jpg`** / similar in workspace preview + downloadable asset.
5. Cross-check Pollinations **Activity**: one hit on **Flux Schnell**, expected pollen spend.

## 5. Optional CLI smoke test (on server)

```bash
cd /opt/codeagent && set -a && . ./secrets/pollinations.env && set +a && python3 test_or.py
```

(Tool writes under `/opt/codeagent/workspaces/…` server paths.)

## 6. Incident / hygiene

| Situation | Action |
|-----------|--------|
| Key pasted in Slack/Discord/Chat | Revoke → new key → update server only (not repo) |
| `401`/`403` from Pollinations | Key / entitlement / typo |
| Old Blender errors in logs | Stale binaries or old branch — current design is Pollinations-only |
| Wrong model billed | Confirm `tools/image_gen.py` still pins `flux` |

## 7. Where “memory” lives for Cursor

- **Rule:** `.cursor/rules/codeagent-pollinations-image.mdc` — short guardrails while editing `image_gen.py`.
- **Facts:** `docs/LONG_TERM_MEMORY_CODEAGENT_POLLINATIONS.md`.
- **This file:** procedural walkthrough for humans.
