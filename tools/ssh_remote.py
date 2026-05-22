"""
One-shot SSH remote command execution (does not hang like interactive ssh in bash workers).

- Key-based login: omit `password` (uses ssh agent / ~/.ssh/id_*).
- Password login: omit `password` in the tool call JSON; approve in the UI and type the
  password there (stored only for that subprocess via sshpass).

Requires `sshpass` on the CodeAgent server for password auth: `apt-get install sshpass`.
"""

from __future__ import annotations

import asyncio
import os
import re
from shutil import which

from .base import BaseTool

_SAFE_HOST_USER = re.compile(r"^[a-zA-Z0-9_.:\[\]@-]+$")

# When the model omits `command`, run this on the remote host (connectivity / auth probe only).
_DEFAULT_REMOTE_CMD = "echo __CA_SSH_OK__ && uname -a"


def _is_placeholder_ssh_password(p: str) -> bool:
    """Reject redacted/UI-hint literals the model accidentally echoes into JSON."""
    s = (p or "").strip()
    if not s:
        return False
    sl = s.lower()
    if "redacted" in sl and "***" in s:
        return True
    if "enter in ssh" in sl:
        return True
    if "enter below if needed" in sl:
        return True
    if s.startswith("***") or s.endswith("***"):
        return True
    return False


class SSHRemoteTool(BaseTool):
    name = "ssh_remote"
    description = (
        "Run a **single** shell command **on a REMOTE** host via SSH (one shot, no PTY)."
        "\n• **NOT for `ssh-copy-id`**: copying your public key runs **on this CodeAgent server** — use the **bash** tool with "
        "`ssh-copy-id -o StrictHostKeyChecking=accept-new user@host` (then approve; may need password typed in the SSH approval box on a follow-up **ssh_remote** if you test the link first)."
        "\n• **ssh_remote** only runs the given `command` **on the remote machine** (or a tiny default probe if you omit `command`)."
        "\n• **Password**: omit `password` in JSON; approve in the UI and use the password box (needs `sshpass` on this server)."
        "\n• Interactive `ssh` sessions are not supported — use a one-line remote command."
    )

    parameters = {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "Remote hostname or IP (e.g. 172.30.3.206)",
            },
            "user": {
                "type": "string",
                "description": "SSH login user (default: root)",
            },
            "command": {
                "type": "string",
                "description": "Shell command to run **on the remote** (default if omitted: probe `echo __CA_SSH_OK__ && uname -a`)",
            },
            "password": {
                "type": "string",
                "description": "Leave empty unless automating badly — prefer approving with the UI password box",
            },
            "port": {
                "type": "integer",
                "description": "SSH TCP port (default 22)",
            },
        },
        "required": ["host"],
    }

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    async def execute(
        self,
        *,
        host: str,
        command: str | None = None,
        user: str = "root",
        password: str | None = None,
        port: int | None = None,
    ) -> str:
        host = (host or "").strip()
        user = (user or "root").strip() or "root"
        raw_cmd = (command or "").strip()
        used_default = False
        if not raw_cmd:
            raw_cmd = _DEFAULT_REMOTE_CMD
            used_default = True
        cmd = raw_cmd

        if not host or "\n" in host or "\n" in cmd:
            return "Invalid host or multi-line remote command."

        if not _SAFE_HOST_USER.match(host) or not _SAFE_HOST_USER.match(user):
            return "Rejected: host/user has invalid characters for this tool."

        target = f"{user}@{host}"
        ssh_base = []
        if port is not None and int(port) > 0 and int(port) < 65536:
            ssh_base.extend(["-p", str(int(port))])

        common_ssh = ssh_base + [
            "-o",
            "ConnectTimeout=20",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]

        env = os.environ.copy()
        passwd = (password or "").strip()

        if passwd and _is_placeholder_ssh_password(passwd):
            return (
                "Invalid `password` in tool JSON (placeholder/redacted text). Omit the password key entirely; "
                "enter the **real root password only** in the web UI approval dialog's SSH password field, "
                "then Allow — nothing is stored in chat."
            )

        if passwd:
            exe = which("sshpass")
            if not exe:
                return (
                    "`sshpass` is not installed on the CodeAgent server.\n"
                    "Install with: apt-get install -y sshpass\n"
                    "Alternatively use SSH public-key auth (`ssh-copy-id`) and omit the password entirely."
                )
            ssh_cmd = ["sshpass", "-e", "ssh"]
            ssh_cmd.extend(common_ssh)
            ssh_cmd.extend([
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
            ])
            # Do NOT pass BatchMode — sshpass provides the password instead of prompting.
            ssh_cmd.append(target)
            ssh_cmd.append(cmd)
            env["SSHPASS"] = passwd
        else:
            ssh_cmd = ["ssh"]
            ssh_cmd.extend(common_ssh)
            ssh_cmd.extend(["-o", "BatchMode=yes"])
            ssh_cmd.append(target)
            ssh_cmd.append(cmd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"ssh_remote timed out after {self.timeout:.0f}s"
        except FileNotFoundError:
            return "ssh binary not found on CodeAgent PATH."

        stdout = out_b.decode(errors="replace").strip()
        stderr = err_b.decode(errors="replace").strip()
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append("[stderr]\n" + stderr)
        ec = proc.returncode
        parts.append(f"[exit_code: {ec}]")

        txt = "\n".join(parts)

        hint = ""
        if passwd and ec != 0:
            stderr_l = stderr.lower()
            if "permission denied" in stderr_l:
                hint = (
                    "\n[HINT ssh_remote] Wrong password OR server refuses root/password login "
                    "(check `PermitRootLogin` and `PasswordAuthentication` on the remote SSH server). "
                    "Try `ssh-copy-id` via **bash** from this host, or SSH as a sudo user "
                    "(change `user` in the tool)."
                )

        txt = txt + hint

        if used_default:
            txt = f"[remote probe: default command]\n{txt}"
        if len(txt) > 8000:
            return txt[:8000] + "\n... (truncated)"
        return txt
