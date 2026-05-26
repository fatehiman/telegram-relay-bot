from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events, utils
from telethon.errors import MessageNotModifiedError, ReactionsTooManyError
from telethon.tl.functions.messages import SendReactionRequest, SetTypingRequest
from telethon.tl.types import (
    Channel, Chat, InputReplyToMessage,
    UpdatePinnedMessages, UpdateReadHistoryOutbox,
    UpdateUserTyping, User,
)

from config import Delays
from db import DB
from deepseek import DeepSeek
from pairs import disable_pair, load_pairs, resolve_pair
from polish import load_polish_prompt, polish
from rules import resolve
from safety import is_safe_to_relay, load_rules_text

log = logging.getLogger("telbot.handler")


def _chat_type(chat) -> str:
    if isinstance(chat, User):
        return "private"
    if isinstance(chat, Chat):
        return "group"
    if isinstance(chat, Channel):
        return "megagroup" if getattr(chat, "megagroup", False) else "channel"
    return "unknown"


def _chat_title(chat) -> str | None:
    if isinstance(chat, User):
        name = " ".join(filter(None, [chat.first_name, chat.last_name])).strip()
        return name or chat.username
    return getattr(chat, "title", None)


async def _build_ctx(event) -> dict:
    msg = event.message
    chat = await event.get_chat()
    sender = await event.get_sender()
    from_id = getattr(sender, "id", None)
    from_username = getattr(sender, "username", None) if sender else None
    if isinstance(sender, User):
        from_name = " ".join(filter(None, [sender.first_name, sender.last_name])).strip() or None
    else:
        from_name = getattr(sender, "title", None)

    return {
        "tg_message_id": msg.id,
        "chat_id": event.chat_id,
        "chat_type": _chat_type(chat),
        "chat_title": _chat_title(chat),
        "from_id": from_id,
        "from_username": from_username,
        "from_name": from_name,
        "text": msg.message or "",
        "date_ts": int(msg.date.timestamp()) if msg.date else None,
        "raw": msg.to_dict(),
    }


def _format_relay(template: str, ctx: dict) -> str:
    return template.format(
        sender_name=ctx.get("from_name") or "",
        sender_username=ctx.get("from_username") or "",
        chat_title=ctx.get("chat_title") or "",
        chat_id=ctx.get("chat_id"),
        text=ctx.get("text") or "",
    )


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:80] or "unknown"


def _archive(ctx: dict, root: str) -> Path | None:
    """Append a message entry to msg/YYYYMMDD/<sender>.json (atomic per task)."""
    ts = ctx.get("date_ts") or int(datetime.now().timestamp())
    day = datetime.fromtimestamp(ts).strftime("%Y%m%d")
    sender_id = ctx.get("from_username") or (
        f"id_{ctx['from_id']}" if ctx.get("from_id") else "unknown"
    )
    folder = Path(root) / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(sender_id)}.json"

    entries: list = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8")) or []
            if not isinstance(entries, list):
                entries = []
        except json.JSONDecodeError:
            log.warning("archive file %s is corrupt; starting fresh", path)
            entries = []

    entries.append({
        "tg_message_id": ctx.get("tg_message_id"),
        "chat_id": ctx.get("chat_id"),
        "chat_type": ctx.get("chat_type"),
        "chat_title": ctx.get("chat_title"),
        "from_id": ctx.get("from_id"),
        "from_username": ctx.get("from_username"),
        "from_name": ctx.get("from_name"),
        "text": ctx.get("text"),
        "date_ts": ts,
        "date_iso": datetime.fromtimestamp(ts).isoformat(),
    })

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def register(client: TelegramClient, db: DB, ai: DeepSeek,
             rules_cfg: dict, pairs_path: str,
             stop_relay_rules_path: str, polish_prompt_path: str,
             archive_dir: str, delays: Delays) -> None:

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event):
        try:
            ctx = await _build_ctx(event)
        except Exception:
            log.exception("failed to build ctx")
            return

        # Mark this chat as active so the reaction-polling loop knows to
        # include it in upcoming cycles.
        mark_chat_active(ctx["chat_id"])

        actions = resolve(rules_cfg, ctx)
        log.info(
            "msg chat=%s(%s) from=%s text=%r rule=%s",
            ctx["chat_title"], ctx["chat_type"], ctx["from_username"] or ctx["from_id"],
            (ctx["text"] or "")[:80], actions.get("_matched_rule"),
        )

        # 1. Archive to msg/YYYYMMDD/<sender>.json
        try:
            archived = _archive(ctx, archive_dir)
            if archived:
                log.debug("archived -> %s", archived)
        except Exception:
            log.exception("archive failed")

        # 2. Persist to SQLite
        msg_db_id: int | None = None
        if actions.get("save", True):
            try:
                msg_db_id = await db.save_message(ctx)
            except Exception:
                log.exception("db save failed")

        # 3. DeepSeek analysis (rule-controlled)
        if msg_db_id is not None and actions.get("analyze") and ctx["text"]:
            try:
                resp = await ai.chat(actions["analyze_system_prompt"], ctx["text"],
                                     temperature=0.1, max_tokens=300)
                await db.save_analysis(msg_db_id, ai.model, resp, None)
            except Exception as e:
                log.exception("analysis failed")
                await db.save_analysis(msg_db_id, ai.model, None, str(e))

        # 4. AI auto-reply (rule-controlled)
        ar = actions.get("auto_reply") or {}
        if msg_db_id is not None and ar.get("enabled") and ctx["text"]:
            try:
                reply = await ai.chat(
                    ar.get("system_prompt") or "Reply briefly and politely.",
                    ctx["text"],
                    temperature=float(ar.get("temperature", 0.4)),
                    max_tokens=int(ar.get("max_tokens", 300)),
                )
                if reply:
                    await event.reply(reply)
                    await db.save_action(msg_db_id, "reply", str(ctx["chat_id"]), reply, "sent")
            except Exception as e:
                log.exception("auto-reply failed")
                await db.save_action(msg_db_id, "reply", str(ctx["chat_id"]), None, "error", str(e))

        # 5. Pair-based relay (bidirectional middleman) with random delay
        await _maybe_pair_relay(client, db, ai, pairs_path,
                                stop_relay_rules_path, polish_prompt_path,
                                delays, ctx, msg_db_id, event.message)

        # 6. Rule-based relay (still supported via rules.yaml relay_to)
        relay_to = actions.get("relay_to")
        if msg_db_id is not None and relay_to:
            try:
                payload = _format_relay(actions.get("relay_template") or "{text}", ctx)
                target = int(relay_to) if str(relay_to).lstrip("-").isdigit() else relay_to
                await client.send_message(target, payload)
                await db.save_action(msg_db_id, "relay", str(relay_to), payload, "sent")
            except Exception as e:
                log.exception("rule relay failed")
                await db.save_action(msg_db_id, "relay", str(relay_to), None, "error", str(e))

    @client.on(events.MessageEdited(incoming=True))
    async def on_edit(event):
        # Only edits to messages we previously pair-relayed are propagated.
        # The relay_map lookup is the gate: no map row → not a relayed
        # message → nothing to mirror.
        try:
            ctx = await _build_ctx(event)
        except Exception:
            log.exception("failed to build ctx for edit")
            return
        if ctx.get("chat_type") != "private":
            return

        counterpart = await db.get_relay_counterpart(ctx["chat_id"], ctx["tg_message_id"])
        if not counterpart:
            return
        other_chat, other_msg, source_side = counterpart
        # Telegram only allows editing your own messages, so all incoming
        # edits originate on the source side. If somehow we see a target-side
        # edit, ignore it to avoid feedback loops.
        if not source_side:
            return

        pairs = load_pairs(pairs_path)
        if not pairs:
            return
        sender_id = ctx.get("from_username") or (
            str(ctx["from_id"]) if ctx.get("from_id") else None
        )
        entry = resolve_pair(pairs, ctx.get("from_username"), ctx.get("from_id"))
        if not entry or not sender_id:
            # Pair gone (e.g. removed or [d]-disabled by safety) — don't
            # propagate further edits.
            return
        flags = entry.flags

        text = ctx.get("text") or ""
        has_media = event.message.media is not None

        # Re-run the safety check on the new text. A blocked edit disables
        # the pair just like a blocked initial relay would.
        if text and "i" not in flags:
            safety_rules_text = load_rules_text(stop_relay_rules_path)
            decision, ai_resp = await is_safe_to_relay(ai, safety_rules_text, text)
            if decision is False:
                log.warning("SAFETY BLOCK on edit: %s -> %s. AI=%r. Msg=%r",
                            sender_id, entry.target, ai_resp, text[:80])
                disable_pair(pairs, pairs_path, sender_id, entry.target, reason="safety-edit")
                return
            if decision is None:
                log.warning("safety check inconclusive on edit (%r) — skipping", ai_resp)
                return

        text_to_send = text
        if text and "p" in flags:
            polish_prompt = load_polish_prompt(polish_prompt_path)
            polished, perr = await polish(ai, polish_prompt, text)
            if perr:
                log.warning("polish failed on edit (%s); using original", perr)
            else:
                text_to_send = polished

        # For media without caption there's nothing meaningful to edit; skip.
        if has_media and not text_to_send:
            return
        try:
            await client.edit_message(other_chat, other_msg, text=text_to_send)
            log.info("pair edit propagated: %s -> chat=%s msg=%s",
                     sender_id, other_chat, other_msg)
        except MessageNotModifiedError:
            # Polish normalized the edit back to the same string we already
            # sent, OR Telegram fired MessageEdited for a non-text change
            # (reactions / view count). Both are no-ops for us.
            log.debug("edit skipped (content unchanged): chat=%s msg=%s",
                      other_chat, other_msg)
        except Exception:
            log.exception("pair edit failed (chat=%s msg=%s)", other_chat, other_msg)

    @client.on(events.MessageDeleted)
    async def on_delete(event):
        # MessageDeleted in private chats arrives without a chat_id (Telegram
        # only fills it for channels/supergroups). In that case we fall
        # back to searching the relay_map by msg_id alone — private-chat
        # msg_ids are globally sequential within the user account, so they
        # don't collide.
        chat_id = event.chat_id
        for msg_id in event.deleted_ids:
            if chat_id is not None:
                cp = await db.get_relay_counterpart(chat_id, msg_id)
                matches = [cp] if cp else []
            else:
                matches = await db.find_relay_by_msg_id(msg_id)
            for other_chat, other_msg, source_side in matches:
                # Only mirror when the deleter was the original sender —
                # don't chain-delete the source when someone deletes the
                # relayed copy on the target side.
                if not source_side:
                    continue
                try:
                    await client.delete_messages(other_chat, [other_msg])
                    log.info("pair delete propagated: chat=%s msg=%s",
                             other_chat, other_msg)
                except Exception:
                    log.exception("pair delete failed (chat=%s msg=%s)",
                                  other_chat, other_msg)

    # Cache of user_id -> username so the typing handler doesn't have to
    # round-trip resolve_pair-by-username through a get_entity every event.
    username_cache: dict[int, str | None] = {}

    @client.on(events.Raw(types=[UpdateUserTyping]))
    async def on_typing(update):
        user_id = update.user_id
        action = update.action

        # Typing in a private chat counts as activity on that chat (chat_id
        # == user_id for private chats from the bot's perspective).
        mark_chat_active(user_id)

        pairs = load_pairs(pairs_path)
        if not pairs:
            return

        username = username_cache.get(user_id)
        if user_id not in username_cache:
            try:
                u = await client.get_entity(user_id)
                username = getattr(u, "username", None)
            except Exception:
                username = None
            username_cache[user_id] = username

        entry = resolve_pair(pairs, username, user_id)
        if not entry:
            return

        target = entry.target
        try:
            resolved = int(target) if str(target).lstrip("-").isdigit() else target
            target_entity = await client.get_input_entity(resolved)
            await client(SetTypingRequest(peer=target_entity, action=action))
            log.debug("typing relayed: %s -> %s action=%s",
                      username or user_id, target, type(action).__name__)
        except Exception:
            # Typing relays fail frequently (flood waits, transient resolve
            # errors) and they're cosmetic — log at debug and move on.
            log.debug("typing relay failed", exc_info=True)

    @client.on(events.Raw(types=[UpdateReadHistoryOutbox]))
    async def on_outbox_read(update):
        # The target side opened their chat with the bot and read up to
        # `max_id`. Map that read horizon back to a source-side msg_id and
        # mark the source chat as read up to that point. Telegram's read
        # receipts are sequential per chat, so marking the latest matching
        # a_msg implicitly covers all earlier ones in the same a_chat.
        try:
            chat_id = utils.get_peer_id(update.peer)
        except Exception:
            log.exception("failed to derive chat_id from outbox-read update")
            return
        max_id = update.max_id

        # The other side opening their chat is a clear "they're online right
        # now" signal — likely to be followed by a reaction.
        mark_chat_active(chat_id)

        row = await db.find_latest_relay_in_chat(chat_id, max_id)
        if not row:
            return
        src_chat, src_msg = row

        # Fire-and-forget with the existing random mark-read delay so the
        # propagated receipt doesn't arrive suspiciously instantly.
        asyncio.create_task(_delayed_mark_read(
            client, src_chat, src_msg,
            f"propagated from b_chat={chat_id} max_id={max_id}", delays,
        ))

    # Reactions in 1-on-1 private chats are NOT pushed to user accounts by
    # Telegram (server-side restriction — see start_reaction_polling below).
    # No event handler can catch them. We poll the relayed messages on a
    # timer instead and mirror reaction changes from there. The
    # `on_reaction` event handler that used to live here was removed because
    # it never fired for the only chat type pair-relay supports.

    @client.on(events.Raw(types=[UpdatePinnedMessages]))
    async def on_pin(update):
        # UpdatePinnedMessages covers private chats and basic groups (the
        # channel/supergroup variant is UpdatePinnedChannelMessages, which
        # we ignore because pair-relay only runs for private chats).
        try:
            chat_id = utils.get_peer_id(update.peer)
        except Exception:
            log.exception("failed to derive chat_id from pin update")
            return
        pinned = bool(getattr(update, "pinned", True))
        for msg_id in update.messages:
            cp = await db.get_relay_counterpart(chat_id, msg_id)
            if not cp:
                continue
            other_chat, other_msg, _src_side = cp
            try:
                if pinned:
                    await client.pin_message(other_chat, other_msg, notify=False)
                    log.info("pair pin propagated: chat=%s msg=%s",
                             other_chat, other_msg)
                else:
                    await client.unpin_message(other_chat, other_msg)
                    log.info("pair unpin propagated: chat=%s msg=%s",
                             other_chat, other_msg)
            except Exception:
                log.exception("pair pin/unpin failed (chat=%s msg=%s)",
                              other_chat, other_msg)


async def _maybe_pair_relay(client: TelegramClient, db: DB, ai: DeepSeek,
                            pairs_path: str, stop_relay_rules_path: str,
                            polish_prompt_path: str, delays: Delays,
                            ctx: dict, msg_db_id: int | None,
                            message) -> None:
    if ctx.get("chat_type") != "private":
        return
    # Re-read every config file on EVERY message so manual edits to
    # pair-accounts.txt, stop-relay-rules.txt, or polish-prompt.txt take
    # effect immediately, no restart needed. All three are tiny text files.
    pairs = load_pairs(pairs_path)
    if not pairs:
        return
    sender_id = ctx.get("from_username") or (
        str(ctx["from_id"]) if ctx.get("from_id") else None
    )
    entry = resolve_pair(pairs, ctx.get("from_username"), ctx.get("from_id"))
    if not entry or not sender_id:
        return
    target = entry.target
    flags = entry.flags

    text = ctx.get("text") or ""
    has_media = message.media is not None
    if not text and not has_media:
        log.info("pair relay skipped (empty message) from=%s -> %s", sender_id, target)
        return

    # Step 1. SAFETY CHECK — only on the text/caption part. Pure-media
    # messages (no caption) bypass the check entirely, as do pairs with the
    # 'i' flag.
    if "i" in flags:
        log.info("safety check skipped (i flag): %s -> %s", sender_id, target)
    elif not text:
        log.info("safety check skipped (media-only): %s -> %s", sender_id, target)
    else:
        # Blocked messages are NOT marked as seen, NOT polished, and NOT
        # relayed. The pair gets a [d] flag in pair-accounts.txt until the
        # user manually clears it.
        safety_rules_text = load_rules_text(stop_relay_rules_path)
        decision, ai_resp = await is_safe_to_relay(ai, safety_rules_text, text)
        if decision is False:
            log.warning("SAFETY BLOCK: relay %s -> %s blocked. AI=%r. Msg=%r",
                        sender_id, target, ai_resp, text[:80])
            disable_pair(pairs, pairs_path, sender_id, target, reason="safety")
            if msg_db_id is not None:
                await db.save_action(msg_db_id, "safety_block", str(target), text,
                                     "blocked", ai_resp)
            return
        if decision is None:
            log.warning("safety check inconclusive (%r) — skipping this relay, "
                        "pair kept active", ai_resp)
            if msg_db_id is not None:
                await db.save_action(msg_db_id, "pair_relay", str(target), text,
                                     "skipped_inconclusive", ai_resp)
            return

    # NOTE: We deliberately do NOT mark the source message as seen here.
    # Doing so would give the source user a misleading "they've read it"
    # double-check while the relayed copy is still sitting unread in the
    # target's chat. Instead, the on_outbox_read handler below mirrors the
    # target side's actual read receipts back to the source via the
    # relay_map. The source stays at one checkmark until the human on the
    # other end opens their chat and reads.

    # Step 3. POLISH (if 'p' flag set and there is text to polish) via
    # DeepSeek. Media-only messages skip this step.
    text_to_send = text
    if text and "p" in flags:
        polish_prompt = load_polish_prompt(polish_prompt_path)
        polished, perr = await polish(ai, polish_prompt, text)
        if perr:
            log.warning("polish failed for %s -> %s (%s); sending original",
                        sender_id, target, perr)
        else:
            log.info("polished %s -> %s: %r -> %r",
                     sender_id, target, text[:60], polished[:60])
        text_to_send = polished

    # Resolve the target entity once. We need its canonical chat_id both
    # to record the relay_map entry and to validate any reply_to lookup
    # (the counterpart must live in the chat we're about to send to).
    try:
        resolved = int(target) if str(target).lstrip("-").isdigit() else target
        target_entity = await client.get_input_entity(resolved)
        target_chat_id = utils.get_peer_id(target_entity)
    except Exception as e:
        log.exception("failed to resolve relay target %s", target)
        if msg_db_id is not None:
            await db.save_action(msg_db_id, "pair_relay", str(target), None, "error", str(e))
        return

    # If the incoming message was a reply, look up the counterpart of the
    # replied-to message in the target chat so the relay can land as a
    # proper reply too. Best-effort: if the original wasn't relayed (or
    # was relayed to a different chat than the current target), drop the
    # reply context and send a standalone message. If the source reply
    # quotes a specific snippet (Telegram quote-replies), preserve it via
    # InputReplyToMessage.
    reply_to: int | InputReplyToMessage | None = None
    src_reply_id = getattr(message, "reply_to_msg_id", None)
    if src_reply_id:
        counterpart = await db.get_relay_counterpart(ctx["chat_id"], src_reply_id)
        if counterpart:
            other_chat, other_msg, _src_side = counterpart
            if other_chat == target_chat_id:
                rh = getattr(message, "reply_to", None)
                quote_text = getattr(rh, "quote_text", None) if rh else None
                if quote_text:
                    reply_to = InputReplyToMessage(
                        reply_to_msg_id=other_msg,
                        quote_text=quote_text,
                        quote_entities=getattr(rh, "quote_entities", None),
                        quote_offset=getattr(rh, "quote_offset", None),
                    )
                else:
                    reply_to = other_msg

    # Step 4. Random anti-ban delay, then SEND to the paired account.
    # Messages with media are relayed as-is (re-uploaded, no "forwarded
    # from" header) with the polished caption; pure-text messages go via
    # send_message. View-once / self-destruct TTL is forwarded through
    # send_file's `ttl` parameter, which sets ttl_seconds on the resulting
    # InputMediaPhoto / InputMediaDocument.
    reply_log = (reply_to.reply_to_msg_id
                 if isinstance(reply_to, InputReplyToMessage) else reply_to)
    delay = random.uniform(delays.relay_min, delays.relay_max)
    log.info("pair relay queued: %s -> %s in %.2fs (media=%s, reply_to=%s)",
             sender_id, target, delay, has_media, reply_log)
    await asyncio.sleep(delay)

    try:
        if has_media:
            ttl = getattr(message.media, "ttl_seconds", None)
            sent = await client.send_file(target_entity, file=message,
                                          caption=text_to_send or None,
                                          reply_to=reply_to,
                                          ttl=ttl if ttl else None)
            log.info("pair relayed media to %s: caption=%r ttl=%s",
                     target, (text_to_send or "")[:80], ttl)
            stored = text_to_send if text_to_send else "[media]"
        else:
            sent = await client.send_message(target_entity, text_to_send,
                                             reply_to=reply_to)
            log.info("pair relayed to %s: %r", target, text_to_send[:80])
            stored = text_to_send
        # Record the source -> target mapping so future edits/deletes/pins/
        # replies on this message can be propagated.
        try:
            await db.save_relay_map(ctx["chat_id"], ctx["tg_message_id"],
                                    target_chat_id, sent.id)
        except Exception:
            log.exception("save_relay_map failed (relay still sent)")
        # Both chats just had activity (source: sender; target: a new msg
        # landed there). Either side may add a reaction in the next few
        # seconds, so flag both for the reaction-polling loop.
        mark_chat_active(ctx["chat_id"])
        mark_chat_active(target_chat_id)
        if msg_db_id is not None:
            await db.save_action(msg_db_id, "pair_relay", str(target), stored, "sent")
    except Exception as e:
        log.exception("pair relay failed")
        if msg_db_id is not None:
            await db.save_action(msg_db_id, "pair_relay", str(target), None, "error", str(e))


async def _delayed_mark_read(client: TelegramClient, chat_id: int,
                             max_id: int, sender_label: str,
                             delays: Delays) -> None:
    delay = random.uniform(delays.mark_read_min, delays.mark_read_max)
    await asyncio.sleep(delay)
    try:
        await client.send_read_acknowledge(chat_id, max_id=max_id)
        log.debug("marked read chat=%s up to msg_id=%s (from %s) after %.2fs",
                  chat_id, max_id, sender_label, delay)
    except Exception:
        log.exception("mark-read failed chat=%s msg_id=%s", chat_id, max_id)


# ---------------------------------------------------------------------------
# Reaction polling
# ---------------------------------------------------------------------------
#
# Why this exists: Telegram's MTProto layer does not deliver
# `updateMessageReactions` to user-account update streams for 1-on-1 private
# chats (server-side restriction; the same asymmetry applies to the Bot API,
# where reactions in private chats are explicitly never forwarded). Channel,
# supergroup, and basic-group reactions DO arrive via that update — but
# pair-relay only ever handles private chats, so the event handler path is
# dead for us. We fetch the reactions field directly from the relayed
# messages on a timer instead. Reference: Telegram core docs at
# https://core.telegram.org/api/reactions plus empirical confirmation.

REACTION_POLL_INTERVAL = 10.0           # seconds between poll cycles
REACTION_POLL_JITTER = 2.0              # ± seconds added to each sleep so the
                                        # call cadence isn't a perfect periodic
                                        # fingerprint
REACTION_POLL_LOOKBACK_HOURS = 24       # only re-poll relays from the last 24h
REACTION_POLL_PER_CHAT_LIMIT = 100      # cap msgs per get_messages call
REACTION_PAIR_IDLE_THRESHOLD = 60.0     # seconds; pair must have had activity
                                        # within this window to be polled.
                                        # Was 5s originally, but with a 10s
                                        # poll interval most reactions land
                                        # 10-60s after the message and got
                                        # missed by a too-tight window.
REACTION_GLOBAL_IDLE_THRESHOLD = 300.0  # seconds; once EVERY chat has been
                                        # idle this long the loop skips its
                                        # API calls entirely (still wakes on
                                        # the next interval to re-check)
REACTION_GLOBAL_IDLE_SLEEP = 30.0       # seconds; longer sleep while fully
                                        # idle, to drop API load to zero

# Module-level dict tracking the last-seen activity timestamp per chat_id.
# A chat is "active" when ANY observable event (incoming message, typing
# action, outbox read, or a relay we just sent INTO that chat) happens on
# it. The reaction-polling loop consults this dict to decide which chats to
# include in the next get_messages batch.
_chat_activity: dict[int, float] = {}


def mark_chat_active(chat_id: int) -> None:
    """Record observable activity in `chat_id`. Used by the reaction-polling
    loop to gate which chats it queries. Safe to call from anywhere; this is
    a plain dict assignment under the asyncio event-loop thread."""
    if chat_id is not None:
        _chat_activity[chat_id] = time.time()


def _extract_partner_reactions(reactions) -> list:
    """Pick out the reactions added by the OTHER party (not us). Prefer
    `recent_reactions` because each `MessagePeerReaction` carries a `.my`
    flag; fall back to summary `results` with `chosen_order=None` (in
    private chats, "not chosen by us" implies "the partner did it"), which
    handles cases where the server omits per-user info."""
    out: list = []
    if not reactions:
        return out
    if getattr(reactions, "recent_reactions", None):
        for pr in reactions.recent_reactions:
            if not getattr(pr, "my", False):
                out.append(pr.reaction)
    if not out and getattr(reactions, "results", None):
        for rc in reactions.results:
            if getattr(rc, "chosen_order", None) is None and rc.count > 0:
                out.append(rc.reaction)
    return out


def _reactions_signature(reactions: list) -> tuple:
    """Hashable representation of a partner reaction set, for cheap
    change-detection between poll cycles. We discriminate emoji vs. custom
    emoji so a switch between the two doesn't get misread as unchanged."""
    sig = []
    for r in reactions:
        if hasattr(r, "emoticon") and r.emoticon is not None:
            sig.append(("e", r.emoticon))
        elif hasattr(r, "document_id") and r.document_id is not None:
            sig.append(("c", int(r.document_id)))
        else:
            sig.append(("?", repr(r)))
    return tuple(sorted(sig))


async def _poll_chat_reactions(client: TelegramClient, db: DB, chat_id: int,
                               msg_ids: list[int],
                               state: dict[tuple[int, int], tuple]) -> None:
    try:
        msgs = await client.get_messages(chat_id, ids=msg_ids)
    except Exception:
        log.debug("reaction poll: get_messages failed chat=%s",
                  chat_id, exc_info=True)
        return
    # client.get_messages with ids=[...] returns a list aligned to ids;
    # missing entries come back as None.
    for m in msgs:
        if not m:
            continue
        partner = _extract_partner_reactions(getattr(m, "reactions", None))
        sig = _reactions_signature(partner)
        key = (chat_id, m.id)
        if state.get(key) == sig:
            continue
        state[key] = sig

        cp = await db.get_relay_counterpart(chat_id, m.id)
        if not cp:
            continue
        other_chat, other_msg, _src_side = cp
        try:
            target_entity = await client.get_input_entity(other_chat)
            await client(SendReactionRequest(
                peer=target_entity,
                msg_id=other_msg,
                reaction=partner,
                big=False,
                add_to_recent=False,
            ))
            log.info("reaction (poll): chat=%s msg=%s -> chat=%s msg=%s count=%d",
                     chat_id, m.id, other_chat, other_msg, len(partner))
        except MessageNotModifiedError:
            # Target already in the reaction state we'd set. Happens after a
            # restart (in-memory state lost; first cycle re-sends what's
            # already in sync), or when two poll cycles race to mirror the
            # same change. Treat as success — `state[key]` was already
            # written above, so we won't retry next cycle.
            log.debug("reaction in-sync (target already matches): "
                      "chat=%s msg=%s", other_chat, other_msg)
        except ReactionsTooManyError:
            # The target message has reached `reactions_uniq_max` distinct
            # reactions (free user accounts cap at 1, premium at 3). We
            # can't add ours. Mark as "handled" so we don't log a stack
            # every cycle for this msg.
            log.warning("reaction cap reached: chat=%s msg=%s — partner's "
                        "set will not be mirrored here", other_chat, other_msg)
        except Exception:
            log.exception("reaction propagation failed (chat=%s msg=%s)",
                          other_chat, other_msg)


def _next_sleep(base: float, jitter: float) -> float:
    """Compute a sleep interval with ±jitter so call timing doesn't form a
    perfectly periodic fingerprint."""
    return max(0.0, base + random.uniform(-jitter, jitter))


async def _reaction_poll_loop(client: TelegramClient, db: DB) -> None:
    log.info("reaction polling started (interval=%.0fs±%.0fs, pair-idle=%.0fs, "
             "global-idle=%.0fs, lookback=%dh)",
             REACTION_POLL_INTERVAL, REACTION_POLL_JITTER,
             REACTION_PAIR_IDLE_THRESHOLD, REACTION_GLOBAL_IDLE_THRESHOLD,
             REACTION_POLL_LOOKBACK_HOURS)
    state: dict[tuple[int, int], tuple] = {}
    while True:
        try:
            now = time.time()

            # Global-idle gate: if nothing anywhere has shown activity in
            # the last REACTION_GLOBAL_IDLE_THRESHOLD seconds, skip ALL
            # API calls this cycle and sleep longer. Resumes naturally as
            # soon as any handler calls mark_chat_active().
            latest = max(_chat_activity.values()) if _chat_activity else 0.0
            if latest and (now - latest) > REACTION_GLOBAL_IDLE_THRESHOLD:
                log.debug("reaction polling fully idle (%.0fs since last "
                          "activity) — skipping cycle",
                          now - latest)
                await asyncio.sleep(_next_sleep(REACTION_GLOBAL_IDLE_SLEEP,
                                                REACTION_POLL_JITTER))
                continue

            # Per-pair gate: only consider chats that had activity in the
            # last REACTION_PAIR_IDLE_THRESHOLD seconds. This is the main
            # API-load reducer — quiet pairs contribute zero traffic.
            active_chats = {
                cid for cid, ts in _chat_activity.items()
                if (now - ts) <= REACTION_PAIR_IDLE_THRESHOLD
            }
            if active_chats:
                since = int(now) - REACTION_POLL_LOOKBACK_HOURS * 3600
                rows = await db.get_recent_relay_rows(since)
                # Group msgs by chat so each chat needs at most one
                # get_messages call per cycle. Filter both sides by the
                # activity-gated set.
                by_chat: dict[int, list[int]] = {}
                for a_chat, a_msg, b_chat, b_msg in rows:
                    if a_chat in active_chats:
                        by_chat.setdefault(a_chat, []).append(a_msg)
                    if b_chat in active_chats:
                        by_chat.setdefault(b_chat, []).append(b_msg)
                for chat_id, msg_ids in by_chat.items():
                    unique = sorted(set(msg_ids), reverse=True)[:REACTION_POLL_PER_CHAT_LIMIT]
                    await _poll_chat_reactions(client, db, chat_id, unique, state)
        except asyncio.CancelledError:
            log.info("reaction polling cancelled")
            return
        except Exception:
            log.exception("reaction poll loop iteration failed")
        await asyncio.sleep(_next_sleep(REACTION_POLL_INTERVAL,
                                        REACTION_POLL_JITTER))


def start_reaction_polling(client: TelegramClient, db: DB) -> asyncio.Task:
    """Spawn the reaction-polling background task. Must be called from an
    async context with a running event loop (i.e. after `client.start()`)."""
    return asyncio.create_task(_reaction_poll_loop(client, db))
