"""One-off helper: log in (first run prompts for phone + Telegram code), then
send a test message to a chosen recipient. After this runs once, telbot.session
exists and main.py can run non-interactively.

Usage:
    .venv/bin/python send_test.py @fatehiman "hello from telbot"
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from config import load_settings


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: send_test.py <@username_or_id> [message]", file=sys.stderr)
        sys.exit(2)
    target = sys.argv[1]
    text = sys.argv[2] if len(sys.argv) > 2 else "test from telbot"

    s = load_settings()
    client = TelegramClient(s.tg_session_name, s.tg_api_id, s.tg_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"logged in as @{getattr(me, 'username', None)} (id={me.id})")
    sent = await client.send_message(target, text)
    print(f"sent message id={sent.id} to {target}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
