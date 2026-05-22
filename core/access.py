"""Users, groups, tools/models allowlists (users.yaml)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from core.config import Config


def users_yaml_path() -> Path:
    env = os.environ.get("CODEAGENT_USERS_YAML")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    return root / "config" / "users.yaml"


def load_document() -> dict[str, Any]:
    path = users_yaml_path()
    if not path.is_file():
        return {"groups": {}, "users": []}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": {}, "users": []}
    if not isinstance(raw, dict):
        return {"groups": {}, "users": []}
    if raw.get("users") is None:
        raw["users"] = []
    if raw.get("groups") is None:
        raw["groups"] = {}
    return raw


def auth_enabled() -> bool:
    return bool(load_document().get("users"))


def normalize_groups(doc: dict) -> dict[str, dict[str, list[str]]]:
    raw = doc.get("groups") or {}
    out: dict[str, dict[str, list[str]]] = {}
    if not isinstance(raw, dict):
        return out
    for name, body in raw.items():
        gn = str(name).strip()
        if not gn or not isinstance(body, dict):
            continue
        tools = body.get("tools")
        models = body.get("models")
        out[gn] = {
            "tools": [str(x) for x in tools] if isinstance(tools, list) else [],
            "models": [str(x) for x in models] if isinstance(models, list) else [],
        }
    return out


def merge_lists(parts: list[list[str]]) -> tuple[bool, list[str]]:
    star = False
    acc: set[str] = set()
    for part in parts:
        for x in part:
            if x == "*":
                star = True
            else:
                acc.add(str(x))
    return star, sorted(acc)


def effective_permissions(
    user_row: dict, groups_map: dict[str, dict[str, list[str]]]
) -> tuple[bool, list[str], bool, list[str]]:
    gnames = [str(x) for x in (user_row.get("groups") or []) if str(x).strip()]
    g_tool_parts = [groups_map[gn]["tools"] for gn in gnames if gn in groups_map]
    g_model_parts = [groups_map[gn]["models"] for gn in gnames if gn in groups_map]
    u_tools = user_row.get("tools")
    u_models = user_row.get("models")
    ut = [str(x) for x in u_tools] if isinstance(u_tools, list) else []
    um = [str(x) for x in u_models] if isinstance(u_models, list) else []
    tw, tls = merge_lists(g_tool_parts + [ut])
    mw, mls = merge_lists(g_model_parts + [um])
    return tw, tls, mw, mls


def tool_allowset_for_agent(
    tools_wildcard: bool, tools_concrete: list[str], registry_names: list[str]
) -> frozenset[str] | None:
    if tools_wildcard:
        return None
    reg = set(registry_names)
    return frozenset(t for t in tools_concrete if t in reg)


def model_allowset_for_agent(models_wildcard: bool, models_concrete: list[str]) -> frozenset[str] | None:
    if models_wildcard:
        return None
    return frozenset(models_concrete)


def effective_agent_frozensets(
    user_row: dict, registry_names: list[str]
) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    doc = load_document()
    gm = normalize_groups(doc)
    tw, tls, mw, mls = effective_permissions(user_row, gm)
    ta = tool_allowset_for_agent(tw, tls, registry_names)
    ma = model_allowset_for_agent(mw, mls)
    return ta, ma


def session_model_override(user_row: dict | None) -> str | None:
    """OpenRouter `model` id for API calls when groups/users list concrete models (not `*`).
    First model id wins if several are listed. None → use config.yaml model name."""
    if not user_row:
        return None
    doc = load_document()
    gm = normalize_groups(doc)
    _tw, _tls, mw, mls = effective_permissions(user_row, gm)
    if mw or not mls:
        return None
    return str(mls[0]).strip() or None


def get_user_by_username(username: str) -> dict | None:
    if not username:
        return None
    doc = load_document()
    for u in doc.get("users") or []:
        if isinstance(u, dict) and str(u.get("username", "")) == str(username):
            return u
    return None


def verify_password(password: str, password_hash: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == str(password_hash or "")


def authenticate_user(username: str, password: str) -> dict | bool:
    user = get_user_by_username(username)
    if not user:
        return False
    if not verify_password(password, str(user.get("password_hash", ""))):
        return False
    return user


def is_admin(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_admin") is True:
        return True
    for g in user.get("groups") or []:
        if str(g).lower() == "admin":
            return True
    return False


def hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def config_model_names(cfg: Config) -> list[str]:
    names: list[str] = []
    for _k, mc in (cfg.models or {}).items():
        if mc and getattr(mc, "name", None):
            names.append(str(mc.name))
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def admin_config_public(doc: dict) -> dict:
    gm = normalize_groups(doc)
    groups_out = {k: {"tools": v["tools"][:], "models": v["models"][:]} for k, v in gm.items()}
    users_out: list[dict] = []
    for u in doc.get("users") or []:
        if not isinstance(u, dict):
            continue
        users_out.append({
            "username": u.get("username", ""),
            "groups": list(u.get("groups") or []),
            "tools": list(u.get("tools") or []),
            "models": list(u.get("models") or []),
            "password": "",
        })
    return {"groups": groups_out, "users": users_out}


def validate_usernames_unique(users: list) -> None:
    seen: set[str] = set()
    for u in users:
        if not isinstance(u, dict):
            continue
        un = str(u.get("username", "")).strip()
        if not un:
            raise ValueError("User without username")
        if un in seen:
            raise ValueError(f"Duplicate username: {un}")
        seen.add(un)


def merge_admin_put(body: dict, old_doc: dict) -> dict:
    old_users = {str(u.get("username")): u for u in (old_doc.get("users") or []) if isinstance(u, dict)}
    groups_in = body.get("groups") or {}
    users_in = body.get("users") or []
    if not isinstance(groups_in, dict):
        raise ValueError("groups must be an object")
    if not isinstance(users_in, list):
        raise ValueError("users must be a list")
    validate_usernames_unique(users_in)

    new_groups: dict[str, dict] = {}
    for gname, gbody in groups_in.items():
        gn = str(gname).strip()
        if not gn:
            continue
        if not isinstance(gbody, dict):
            continue
        tl = gbody.get("tools")
        ml = gbody.get("models")
        new_groups[gn] = {
            "tools": [str(x) for x in tl] if isinstance(tl, list) else [],
            "models": [str(x) for x in ml] if isinstance(ml, list) else [],
        }

    new_users: list[dict] = []
    admin_count = 0
    for u in users_in:
        if not isinstance(u, dict):
            continue
        un = str(u.get("username", "")).strip()
        if not un:
            continue
        g = u.get("groups")
        groups = [str(x) for x in g] if isinstance(g, list) else []
        if any(str(x).lower() == "admin" for x in groups):
            admin_count += 1
        tools = u.get("tools")
        models = u.get("models")
        row: dict[str, Any] = {"username": un, "groups": groups}
        if isinstance(tools, list) and tools:
            row["tools"] = [str(x) for x in tools]
        if isinstance(models, list) and models:
            row["models"] = [str(x) for x in models]
        pw = u.get("password")
        if isinstance(pw, str) and pw.strip():
            row["password_hash"] = hash_password(pw.strip())
        else:
            prev = old_users.get(un)
            if prev and prev.get("password_hash"):
                row["password_hash"] = prev["password_hash"]
            else:
                raise ValueError(f"Password required for new user {un}")
        new_users.append(row)

    if admin_count < 1:
        raise ValueError("At least one user must belong to the admin group")

    return {"groups": new_groups, "users": new_users}


def write_document(doc: dict) -> None:
    path = users_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
