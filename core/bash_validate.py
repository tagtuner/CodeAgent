"""Detect bash commands that would hang CodeAgent persistent workers (e.g. interactive-only ssh)."""

from __future__ import annotations

import re


def ssh_interactive_only_message(command: str) -> str | None:
    """
    Refuse obvious interactive-only ssh (no remote argv) so the persistent bash worker
    cannot sit inside an ssh login shell forever.
    """
    s = (command or "").strip()
    if not s or "\n" in s:
        return None

    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        low = seg.lower()
        if "ssh" not in low or low.startswith("scp ") or low.startswith("rsync "):
            continue
        if _segment_has_bare_ssh(seg):
            return (
                "REFUSED (would hang worker): SSH has no remote command. "
                "Use e.g. `ssh -o StrictHostKeyChecking=no user@host 'tail -n 200 /var/log/...'` "
                "or `ssh user@host hostname` — never bare `ssh user@host`."
            )
    return None


def _segment_has_bare_ssh(seg: str) -> bool:
    if "'" in seg or '"' in seg:
        return False
    for m in re.finditer(r"\bssh\b", seg, flags=re.I):
        toks = seg[m.end() :].split()
        if _tokens_after_ssh_are_bare(toks):
            return True
    return False


def _tokens_after_ssh_are_bare(toks: list[str]) -> bool:
    if not toks:
        return True
    i = _skip_ssh_options(toks, 0)
    if i >= len(toks):
        return True
    i += 1  # destination
    return i >= len(toks)


def _skip_ssh_options(toks: list[str], i: int) -> int:
    two_arg = frozenset({"-o", "-i", "-b", "-p", "-l", "-u", "-F", "-J", "-W", "-R", "-L", "-D", "-E", "-c", "-S"})
    while i < len(toks) and toks[i].startswith("-") and len(toks[i]) > 1:
        t = toks[i]
        if "=" in t[2:]:
            i += 1
            continue
        if t in two_arg and i + 1 < len(toks):
            i += 2
            continue
        i += 1
    return i
