"""Quick CLI to test the safety check against a message without running the
full bot. Uses live stop-relay-rules.txt and DeepSeek.

    .venv/bin/python check_safety.py "your test message here"
    .venv/bin/python check_safety.py < message.txt
"""
from __future__ import annotations

import asyncio
import sys

from config import load_settings
from deepseek import DeepSeek
from safety import is_safe_to_relay, load_rules_text


async def main() -> None:
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
    else:
        msg = sys.stdin.read().strip()
    if not msg:
        print("usage: check_safety.py <message>  (or pipe via stdin)", file=sys.stderr)
        sys.exit(2)

    s = load_settings()
    rules = load_rules_text(s.stop_relay_rules_path)
    if not rules:
        print(f"warning: no rules in {s.stop_relay_rules_path}; will return True")
    ai = DeepSeek(s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model)
    decision, raw = await is_safe_to_relay(ai, rules, msg)

    label = {True: "SAFE — would relay",
             False: "BLOCKED — would disable pair",
             None: "INCONCLUSIVE — would skip this message"}[decision]
    print(f"message:  {msg!r}")
    print(f"decision: {label}")
    print(f"AI raw:   {raw!r}")


if __name__ == "__main__":
    asyncio.run(main())
