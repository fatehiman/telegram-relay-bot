from __future__ import annotations

import logging
from pathlib import Path

from deepseek import DeepSeek

log = logging.getLogger("telbot.safety")

SYSTEM_TEMPLATE = """You are a relay safety checker. Two Telegram users chat through a middleman \
and each message is being relayed. Decide whether the user's message clearly and specifically \
discusses a topic listed in the rules.

Rules:
{rules}

CRITICAL: The match must be EXPLICIT, not implied. Default to "true" — only block when the \
message unambiguously discusses one of the listed topics.

Examples assuming rules about salary and job-seeking websites:
- "How much will I be paid?" -> false  (explicit salary)
- "What's the monthly rate?" -> false  (paraphrase of salary)
- "I saw your post on e-estekhdam" -> false  (explicit website)
- "Are there openings on Jobinja?" -> false  (explicit recruitment website)
- "When are you going to work with me?" -> true  (general work/cooperation, no salary/website)
- "Let's talk about the project" -> true  (casual)
- "Do you have a system?" -> true  (unrelated)
- "Can we collaborate?" -> true  (vague, no listed topic)
- "I'd like to hire you" alone -> true  (no salary or website discussed)

When in doubt, return "true". The cost of a wrong "true" is a single unfiltered message; the \
cost of a wrong "false" is permanently disabling the pair.

Respond with exactly one lowercase word: true or false. No quotes, no punctuation, no explanation."""


def load_rules_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        log.debug("no safety rules file at %s", path)
        return ""
    return p.read_text(encoding="utf-8").strip()


async def is_safe_to_relay(ai: DeepSeek, rules_text: str,
                           message_text: str) -> tuple[bool | None, str]:
    """Return (decision, raw_response).

    decision = True  -> safe, relay it.
    decision = False -> unsafe, block relay AND disable the pair.
    decision = None  -> inconclusive (AI error or unparseable output) — skip
                        this one message but leave the pair active.

    If rules_text is empty, always returns (True, '') — no check performed.
    """
    if not rules_text:
        return True, ""

    system = SYSTEM_TEMPLATE.format(rules=rules_text)
    try:
        resp = await ai.chat(system, message_text, temperature=0.0, max_tokens=8)
    except Exception as e:
        log.exception("safety check API call failed")
        return None, f"<error: {e}>"

    norm = resp.strip().lower().strip('"').strip("'").rstrip(".!?, ").strip()
    if norm.startswith("false") or norm in {"no", "0", "block", "stop"}:
        return False, resp
    if norm.startswith("true") or norm in {"yes", "1", "ok", "allow"}:
        return True, resp
    log.warning("safety check returned unexpected response %r — treating as inconclusive", resp)
    return None, resp
