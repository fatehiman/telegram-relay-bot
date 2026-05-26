from __future__ import annotations

import re
from typing import Any


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _norm_username(u: Any) -> str | None:
    if u is None:
        return None
    s = str(u).lstrip("@").lower()
    return s or None


def _user_matches(spec: Any, ctx: dict) -> bool:
    targets = _as_list(spec)
    if not targets:
        return True
    ctx_username = _norm_username(ctx.get("from_username"))
    ctx_id = ctx.get("from_id")
    for t in targets:
        if isinstance(t, int) and ctx_id == t:
            return True
        if isinstance(t, str):
            if t.isdigit() and ctx_id == int(t):
                return True
            if _norm_username(t) and ctx_username and _norm_username(t) == ctx_username:
                return True
    return False


def _chat_matches(spec: Any, ctx: dict) -> bool:
    targets = _as_list(spec)
    if not targets:
        return True
    cid = ctx.get("chat_id")
    return any(int(t) == cid for t in targets if str(t).lstrip("-").isdigit())


def _regex_matches(pattern: Any, value: str | None) -> bool:
    if not pattern:
        return True
    if value is None:
        return False
    return re.search(pattern, value, re.IGNORECASE | re.DOTALL) is not None


def matches(rule: dict, ctx: dict) -> bool:
    """Return True if every condition in rule['match'] is satisfied by ctx."""
    m = rule.get("match") or {}
    if not m:
        return True
    if "chat_type" in m and m["chat_type"] != ctx.get("chat_type"):
        return False
    if "chat_id" in m and not _chat_matches(m["chat_id"], ctx):
        return False
    if "from_user" in m and not _user_matches(m["from_user"], ctx):
        return False
    if "text_regex" in m and not _regex_matches(m["text_regex"], ctx.get("text")):
        return False
    if "chat_title_regex" in m and not _regex_matches(m["chat_title_regex"], ctx.get("chat_title")):
        return False
    return True


def resolve(rules_cfg: dict, ctx: dict) -> dict:
    """Return the merged action dict for this message: defaults <- first matching rule."""
    defaults = rules_cfg.get("defaults") or {}
    actions = {
        **defaults,
        "auto_reply": dict(defaults.get("auto_reply") or {}),
        "_matched_rule": None,
    }
    for rule in rules_cfg.get("rules") or []:
        if matches(rule, ctx):
            ra = rule.get("actions") or {}
            for k, v in ra.items():
                if k == "auto_reply" and isinstance(v, dict):
                    actions["auto_reply"] = {**actions["auto_reply"], **v}
                else:
                    actions[k] = v
            actions["_matched_rule"] = rule.get("name") or "<unnamed>"
            break
    return actions
