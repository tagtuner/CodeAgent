# Secrets (local + server)

- **`pollinations.env`** — one line: `POLLINATIONS_API_KEY=...`
- On Linux server: copy to `/opt/codeagent/secrets/pollinations.env` and run `chmod 600` (and `chmod 700` on the directory).

This file is gitignored; prefer env var `POLLINATIONS_API_KEY` in systemd if you do not use a file.

Full deploy/test steps: `../docs/WALKTHROUGH_POLLINATIONS_IMAGE.md`.
