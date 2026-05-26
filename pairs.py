from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("telbot.pairs")

KNOWN_FLAGS = {"d", "p", "i"}
BIDIRECTIONAL_ONLY_FLAGS = {"d", "i"}
# d = disabled (line-start only — skips the pair entirely)
# p = polish via DeepSeek before relaying messages FROM this side
# i = ignore safety rules (line-start only — skips the stop-relay-rules.txt check)


class PairEntry(NamedTuple):
    target: str          # The other party's display id (kept in original casing).
    flags: frozenset     # Effective flags for THIS side (when this user sends).


_SIDE_RE = re.compile(r"^\s*([^\[\]=]+?)\s*(?:\[([^\]]*)\])?\s*$")


def _norm(s: str) -> str:
    return s.strip().lstrip("@").lower()


def _parse_flags(s: str) -> set[str]:
    return {c.lower() for c in (s or "") if c.isalpha()}


def _parse_side(part: str) -> tuple[str, set[str]] | None:
    m = _SIDE_RE.match(part)
    if not m:
        return None
    name = m.group(1).strip()
    if not name:
        return None
    return name, _parse_flags(m.group(2) or "")


def _parse_line(line: str) -> tuple[set[str], str, set[str], str, set[str]] | None:
    """Return (global_flags, a_name, a_flags, b_name, b_flags) or None on a
    comment/blank/malformed line."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    global_flags: set[str] = set()
    if s.startswith("["):
        end = s.find("]")
        if end == -1:
            return None
        global_flags = _parse_flags(s[1:end])
        s = s[end + 1:].lstrip()

    if "=" not in s:
        return None
    a_part, b_part = s.split("=", 1)
    a = _parse_side(a_part)
    b = _parse_side(b_part)
    if not a or not b:
        return None
    a_name, a_flags = a
    b_name, b_flags = b
    return global_flags, a_name, a_flags, b_name, b_flags


def load_pairs(path: str) -> dict[str, PairEntry]:
    """Parse the pair file and return a bidirectional lookup keyed by sender.

    Each value is a PairEntry(target, flags) where flags are the effective
    per-side flags for the *sender* (e.g. {'p'} means polish their messages
    before relaying). The 'd' global flag disables the pair entirely — the
    line is skipped and neither direction appears in the result.
    """
    pairs: dict[str, PairEntry] = {}
    if not os.path.exists(path):
        log.debug("no pair file at %s", path)
        return pairs

    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parsed = _parse_line(raw)
            if not parsed:
                log.warning("pair file %s:%d: failed to parse: %r", path, lineno, stripped)
                continue
            global_flags, a_name, a_flags, b_name, b_flags = parsed

            for fl in global_flags | a_flags | b_flags:
                if fl not in KNOWN_FLAGS:
                    log.warning("pair file %s:%d: unknown flag %r (ignored)",
                                path, lineno, fl)

            if "d" in global_flags:
                continue

            for side_flags, label in ((a_flags, a_name), (b_flags, b_name)):
                bad = side_flags & BIDIRECTIONAL_ONLY_FLAGS
                for fl in bad:
                    log.warning("pair file %s:%d: %r on side %r ignored — "
                                "use [%s] at line start", path, lineno, fl, label, fl)
                    side_flags.discard(fl)

            eff_a = frozenset(((a_flags | global_flags) & KNOWN_FLAGS) - {"d"})
            eff_b = frozenset(((b_flags | global_flags) & KNOWN_FLAGS) - {"d"})
            pairs[_norm(a_name)] = PairEntry(target=b_name, flags=eff_a)
            pairs[_norm(b_name)] = PairEntry(target=a_name, flags=eff_b)

    log.debug("loaded %d pair entries from %s", len(pairs) // 2, path)
    return pairs


def resolve_pair(pairs: dict[str, PairEntry], from_username: str | None,
                 from_id: int | None) -> PairEntry | None:
    """Return the PairEntry for this sender, or None if no pair is defined."""
    if not pairs:
        return None
    if from_username:
        e = pairs.get(_norm(from_username))
        if e:
            return e
    if from_id is not None:
        e = pairs.get(str(from_id))
        if e:
            return e
    return None


def disable_pair(pairs: dict[str, PairEntry], path: str, a_id: str, b_id: str,
                 reason: str = "") -> bool:
    """Mark the matching line as disabled by inserting a global 'd' flag.

    Behavior:
      - If the line already starts with ``[...]``, add 'd' inside those brackets
        (if not already present).
      - Otherwise prepend ``[d]`` to the line.
      - Per-side flags ([p], etc.) are preserved verbatim so re-enabling
        (remove [d]) restores the previous behavior.
      - Drops both directions from the in-memory pairs dict.
    """
    p = Path(path)
    if not p.exists():
        return False
    norm_a, norm_b = _norm(str(a_id)), _norm(str(b_id))
    lines = p.read_text(encoding="utf-8").splitlines()
    changed = False

    for i, raw in enumerate(lines):
        parsed = _parse_line(raw)
        if not parsed:
            continue
        global_flags, a_name, _, b_name, _ = parsed
        if {_norm(a_name), _norm(b_name)} != {norm_a, norm_b}:
            continue
        if "d" in global_flags:
            continue

        s = raw.lstrip()
        leading = raw[: len(raw) - len(s)]
        if s.startswith("["):
            end = s.find("]")
            inside = s[1:end]
            existing = _parse_flags(inside)
            if "d" not in existing:
                inside = inside + "d"
            new_line = f"[{inside}]" + s[end + 1:]
        else:
            new_line = "[d]" + s
        lines[i] = leading + new_line
        pairs.pop(norm_a, None)
        pairs.pop(norm_b, None)
        changed = True

    if changed:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = datetime.now().isoformat(timespec="seconds")
        log.warning("pair disabled at %s: %s <-> %s (%s) in %s",
                    stamp, a_id, b_id, reason, path)
    return changed
