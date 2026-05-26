from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from config import load_delays, load_rules, load_settings
from db import DB
from deepseek import DeepSeek
from handlers import register, start_reaction_polling
from pairs import load_pairs
from polish import load_polish_prompt
from safety import load_rules_text


async def run() -> None:
    s = load_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("telbot")

    rules_cfg = load_rules(s.rules_path)
    log.info("loaded %d rule(s) from %s", len(rules_cfg.get("rules") or []), s.rules_path)

    # Pairs are reloaded from disk per-message inside the handler, so manual
    # edits to pair-accounts.txt take effect without a restart. We still do
    # a startup load here just to log the current count.
    startup_pairs = load_pairs(s.pairs_path)
    log.info("startup: %d active pair(s) in %s", len(startup_pairs) // 2, s.pairs_path)

    # Sanity-check the safety + polish files at startup; they're re-read
    # from disk on every message so live edits apply without a restart.
    safety_rules_text = load_rules_text(s.stop_relay_rules_path)
    if safety_rules_text:
        log.info("startup: %d chars in %s",
                 len(safety_rules_text), s.stop_relay_rules_path)
    else:
        log.warning("startup: safety rules file %s missing/empty — no filtering until you create it",
                    s.stop_relay_rules_path)
    polish_prompt = load_polish_prompt(s.polish_prompt_path)
    log.info("startup: %d chars in %s (default fallback if file is missing)",
             len(polish_prompt), s.polish_prompt_path)

    delays = load_delays(s.app_conf_path)
    log.info("delays: relay=%.1f-%.1fs mark_read=%.1f-%.1fs (from %s)",
             delays.relay_min, delays.relay_max,
             delays.mark_read_min, delays.mark_read_max, s.app_conf_path)

    db = DB(s.db_path)
    await db.connect()

    ai = DeepSeek(s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model)

    client = TelegramClient(s.tg_session_name, s.tg_api_id, s.tg_api_hash)
    register(client, db, ai, rules_cfg, s.pairs_path,
             s.stop_relay_rules_path, s.polish_prompt_path,
             s.archive_dir, delays)

    await client.start()
    me = await client.get_me()
    log.info("logged in as @%s (id=%s)", getattr(me, "username", None), me.id)

    # Telegram does not push reaction updates to user accounts in 1-on-1
    # private chats, so we poll the relayed messages on a timer and mirror
    # any reaction changes. See handlers.py for the rationale.
    start_reaction_polling(client, db)

    try:
        await client.run_until_disconnected()
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
