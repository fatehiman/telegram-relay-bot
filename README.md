# telbot

Personal Telegram middleman for research and automation. Runs as your own
user account (via MTProto / Telethon — **not** a bot account), listens to
incoming messages, optionally relays them between paired accounts, runs an
AI safety check, archives everything, and can polish outgoing relays.

> Userbot territory. Telegram's ToS prohibit account selling and large-scale
> automation; light personal use generally goes unnoticed. Keep traffic low,
> never relay spam, and don't run this against an account you can't lose.

---

## What it does, per incoming message

```
                  ┌──────────────────────┐
incoming message  │ on_message handler   │
─────────────────►│                      │
                  │  1. archive to JSON  │  msg/YYYYMMDD/<sender>.json
                  │  2. save to SQLite   │  telbot.db (messages table)
                  │  3. (analyze)        │  rule-controlled (rules.yaml)
                  │  4. (auto-reply)     │  rule-controlled (disabled by default)
                  │  5. pair relay       │  see below
                  │  6. (rule relay)     │  rule-controlled (commented out)
                  └──────────────────────┘

In addition to NewMessage, several more handlers mirror lifecycle events
across paired chats (see "Mirroring edits / deletes / pins / replies"):

  MessageEdited          → re-safety-check, then edit relayed copy
  MessageDeleted         → delete the relayed copy on the target side
  UpdatePinnedMessages   → pin / unpin the counterpart in the paired chat
  (reactions — polled, NOT event-driven; see notes)
  UpdateUserTyping       → mirror the partner's typing action to the paired chat
  UpdateReadHistoryOutbox → mirror the target's read receipt back to the source
```

Inside step 5 (`_maybe_pair_relay`):

```
  resolve pair from pair-accounts.txt   (re-read every message)
        │
        ▼
  no pair?  ──► done (chat stays unread)
        │
        ▼
  Step 1. safety check    (only on text/caption; skipped if no text,
            │              or if [i] flag on pair)
            ├─ false ──► add [d] to pair line, save safety_block action, done
            ├─ none  ──► skip this message, pair stays active, done
            └─ true  ──► continue
  Step 2. polish text     (only on text/caption; if 'p' flag for sender,
                           via DeepSeek)
  Step 3. random delay    (1–10s by default, anti-ban) → send
            │
            ├─ pure text   ──► client.send_message
            └─ has media   ──► client.send_file(file=message,
                                                caption=polished_text)

  (Mark-as-seen on the source side is NOT done here. It is driven by
   UpdateReadHistoryOutbox events from the target chat — the source only
   shows two checkmarks once the human on the other end actually reads.)
```

Decisions made deliberately:
- **Mark-as-seen mirrors the target's actual read state.** When the
  relay-side bot delivers Alice's message to Bob, it does *not*
  immediately mark Alice's message as read. Doing that would give Alice
  the misleading "they've read it" double-check while the relayed copy is
  still sitting unread in Bob's chat. Instead, when Bob opens his chat
  with the bot and reads, Telegram emits `UpdateReadHistoryOutbox` to the
  bot; the bot then walks the `relay_map` back to Alice's original message
  and marks her chat read up to that point (after the same 1–3s random
  delay we used for the old auto-mark, so receipts don't arrive
  suspiciously instantly). Blocked / unrelayed messages stay unread on
  the source side forever, which is the same as the old behavior.
- **Pair file is re-read every message.** Manual edits (re-enabling a `[d]`
  pair, adding a new pair) take effect immediately, no restart.
- **Polish is on the sender side**, not the recipient. `alice[p]=bob` means
  alice's outgoing messages get polished before going to bob.
- **Non-text messages relay as-is.** Photos, videos, audio, voice notes,
  documents, stickers, zip files, etc. are re-uploaded to the target with
  no AI processing. Only the **text part** (caption, if any) goes through
  the safety check and polish — so a photo with a caption gets the caption
  filtered/polished, then the photo is delivered with the (possibly
  polished) caption. Re-uploading (vs. forwarding) means no "Forwarded
  from" header appears on the target side. Albums arrive as separate
  events from Telegram and are relayed individually, not re-grouped.
- **TTL / view-once is preserved.** If the incoming media carries
  `ttl_seconds` (self-destruct timer or view-once flag), the value is
  forwarded through `send_file(..., ttl=ttl)` so the relayed copy expires
  the same way.
- **Geo, contact, venue, poll, dice messages.** These media types are
  handled by Telethon's `utils.get_input_media`, which is what `send_file`
  ends up calling when you pass `file=message`. Each gets re-sent as a
  fresh same-type message (a new poll inherits the question but starts
  with zero votes — Telegram has no API to copy a poll's existing vote
  state).

---

## Mirroring edits / deletes / pins / replies

Every successful pair-relay records a row in the `relay_map` SQLite table
(`a_chat_id, a_msg_id, b_chat_id, b_msg_id`), so the bot knows which
target-side message corresponds to which source-side message. That table
backs four extra handlers:

| Event | Behavior |
|---|---|
| **Reply** (incoming `reply_to_msg_id`) | Looked up bidirectionally. Alice replying to her own past message → relay lands as a reply to its counterpart in Bob's chat. Alice replying to a bot-relayed message of Bob's → relay lands as a reply to Bob's original. If the original was never relayed (or was relayed to a different pair), the reply context is dropped and the relay is sent standalone. |
| **Quote-reply** (Telegram quote-a-specific-snippet) | If the incoming reply carries `quote_text` (plus optional `quote_entities` / `quote_offset`), an `InputReplyToMessage` is constructed and passed through, so the relayed reply preserves the highlighted excerpt — not just the reply-arrow. |
| **Edit** (`MessageEdited`) | Looked up source-side only (Telegram only allows editing your own messages). New text is re-run through the safety check and polish exactly like a fresh relay — a blocked edit disables the pair with `[d]`. Caption-only edits are supported; media replacement is not. Edits on disabled / removed pairs are dropped. |
| **Delete** (`MessageDeleted`) | Looked up source-side only — when Alice deletes her own message, the relayed copy on Bob's side is deleted too. Deleting a bot-relayed message (e.g. Alice removing her local view of Bob's message) does **not** cascade back to Bob's original. Telegram doesn't send a chat_id for private-chat deletes, so the lookup falls back to msg_id alone (safe because private-chat msg_ids are globally sequential per user account). |
| **Pin / unpin** (`UpdatePinnedMessages`) | Looked up bidirectionally — pin works from either side. Mirrored silently (`notify=False`) to avoid spamming the other party with pin notifications. |
| **Reactions** (polled, NOT event-driven) | Telegram does **not** push `updateMessageReactions` to user accounts for 1-on-1 private chats (server-side restriction — the same asymmetry the Bot API documents). No event handler can catch them, even a catch-all `events.Raw`. We work around this with **activity-gated polling**: a background task (`start_reaction_polling` in [handlers.py](handlers.py)) runs roughly every `10±2 s` (random jitter so the cadence isn't a perfectly periodic fingerprint), but actually issues `client.get_messages(chat, ids=[...])` **only for chats that had observable activity in the last 5 s** — typing, new message, outbox read, or a relay just sent into the chat. Chats with no recent activity contribute zero API calls. If **every** chat has been idle for 5 minutes the loop skips its API calls entirely and switches to a longer 30 s sleep until any handler calls `mark_chat_active()` again. From each polled message we pull `m.reactions`, filter out our own reactions via `MessagePeerReaction.my` / `ReactionCount.chosen_order`, and mirror the partner's set onto the counterpart via raw `SendReactionRequest`. In-memory state tracks the last-seen reaction signature per `(chat_id, msg_id)` so unchanged reactions don't re-mirror. State is lost on restart — the first poll after a restart re-mirrors everything (idempotent: `SendReactionRequest` with the current reaction set is a no-op on Telegram's side). Tunables (constants in [handlers.py](handlers.py)): `REACTION_POLL_INTERVAL` (10 s base), `REACTION_POLL_JITTER` (±2 s), `REACTION_PAIR_IDLE_THRESHOLD` (5 s — bump to 30–60 s if reactions feel laggy), `REACTION_GLOBAL_IDLE_THRESHOLD` (300 s), `REACTION_GLOBAL_IDLE_SLEEP` (30 s), `REACTION_POLL_LOOKBACK_HOURS` (24 h). |
| **Typing** (`UpdateUserTyping`) | No `relay_map` involvement — these events don't reference a message. The handler resolves the pair (cached user_id → username lookup, then `pair-accounts.txt`), then calls `SetTypingRequest` against the paired target with the partner's exact action object (`typing`, `recording-voice`, `uploading-photo`, etc.). Failures are logged at `DEBUG` because typing events fire often and are cosmetic. |
| **Read receipt** (`UpdateReadHistoryOutbox`) | When the target side reads the bot's outgoing messages (`max_id` in the update is the new read horizon in the target chat), the handler looks up the largest `b_msg_id ≤ max_id` in `relay_map` for that chat, finds the corresponding source-side `(a_chat_id, a_msg_id)`, and fires `send_read_acknowledge` on the source chat up to that msg_id (after a 1–3s random delay). Telegram's read receipts are sequential per chat, so marking the latest matching `a_msg` implicitly marks every earlier source-side message in that chat as read too. |

The `relay_map` table is append-only — rows are never deleted, so even
old pinning / editing / replying on long-relayed messages keeps working
indefinitely. If you ever need to reset it, drop the table; it will be
recreated empty on next start.

---

## Project layout

| Path | Role |
|---|---|
| `main.py` | Entrypoint. Loads settings, opens Telegram + DB + DeepSeek, registers handlers. |
| `handlers.py` | The single `NewMessage(incoming=True)` handler. Orchestrates archive → save → analyze → reply → relay. |
| `config.py` | Loads `.env`, `rules.yaml`, `app.conf`. Exposes `Settings` and `Delays` dataclasses. |
| `db.py` | SQLite schema + async helpers (`aiosqlite`). |
| `deepseek.py` | Thin async wrapper around the OpenAI SDK pointed at DeepSeek. |
| `pairs.py` | Pair file parser, flag handling, `resolve_pair`, `disable_pair`. |
| `safety.py` | AI relay-safety check (`is_safe_to_relay`) using `stop-relay-rules.txt`. |
| `polish.py` | AI text rewrite (`polish`) using `polish-prompt.txt`. |
| `rules.py` | Rule matching for the older `rules.yaml` analyze/auto-reply/relay engine. |
| `send_test.py` | One-off helper: log in interactively, send a single test message. |
| `check_safety.py` | CLI: paste a message, see what the safety check would decide. |
| `requirements.txt` | Python deps. |
| `.env` / `.env.example` | Secrets and file paths. **`.env` is gitignored.** |
| `app.conf` | Operational tuning knobs (currently: relay/mark-read delay ranges). |
| `rules.yaml` | Analyze/auto-reply/rule-based-relay routing (older mechanism). |
| `pair-accounts.txt` | Bidirectional relay pairs with flags. Re-read every message. |
| `stop-relay-rules.txt` | Free-form text the AI safety check uses. Re-read every message. |
| `polish-prompt.txt` | Free-form prompt for the polish rewrite. Re-read every message. |
| `msg/YYYYMMDD/<sender>.json` | Per-day per-sender archive (gitignored). |
| `telbot.db` | SQLite database (gitignored). |
| `telbot.session` | Telethon session file. Created on first login. **Treat as a credential.** |

---

## Configuration files

### `.env`

Loaded once at startup. Restart to apply.

| Key | Default | Purpose |
|---|---|---|
| `TG_API_ID` | — (required) | From https://my.telegram.org/apps |
| `TG_API_HASH` | — (required) | From https://my.telegram.org/apps. Treat as a secret. |
| `TG_SESSION_NAME` | `telbot` | Telethon session-file basename. |
| `DEEPSEEK_API_KEY` | — (required) | From https://platform.deepseek.com. Treat as a secret. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Try `deepseek-reasoner` if safety check needs more nuance. |
| `DB_PATH` | `./telbot.db` | SQLite file. |
| `RULES_PATH` | `./rules.yaml` | Analyze/auto-reply rule config. |
| `PAIRS_PATH` | `./pair-accounts.txt` | Bidirectional pair definitions. |
| `STOP_RELAY_RULES_PATH` | `./stop-relay-rules.txt` | Safety rules text. |
| `POLISH_PROMPT_PATH` | `./polish-prompt.txt` | Polish system prompt. |
| `APP_CONF_PATH` | `./app.conf` | Operational knobs (delays). |
| `ARCHIVE_DIR` | `./msg` | Root of the per-day JSON archives. |
| `LOG_LEVEL` | `INFO` | `DEBUG` to see per-message archive paths, mark-read events, etc. |

### `app.conf` — operational knobs

INI format. Loaded **once at startup** — restart to apply.

```ini
[delays]
relay_min = 1          # seconds; min random delay before sending a relay
relay_max = 10         # seconds; max
mark_read_min = 1      # seconds; min random delay before marking as seen
mark_read_max = 3      # seconds; max
```

Missing file → defaults `(1, 10)` and `(1, 3)`. Non-numeric values fall back
to the default for that key with a warning. `max < min` warns but works
(`random.uniform` is order-agnostic).

### `pair-accounts.txt` — relay pairs

Re-read on every message. Full reference is in the file itself; quick recap:

```
[global-flags]<sender>[side-flags] = <target>[side-flags]
```

| Flag | Scope | Meaning |
|---|---|---|
| `d` | line-start only | Disabled — pair is skipped entirely. Set automatically by the safety check; remove to re-enable. |
| `p` | global or per-side | Polish messages from this side via DeepSeek before relay. |
| `i` | line-start only | Ignore safety rules — relay every message without filtering. |

Combined forms work: `[dpi]`, `[d][p][i]`, `[Di]` all parse the same.
Per-side `d` / `i` is warned and ignored (use global form). Unknown flag
letters warn and are ignored.

### `stop-relay-rules.txt` — safety rules

Plain English, free-form. Re-read on every message. Wrapped by the system
prompt in [safety.py](safety.py) which forces the AI to answer one
lowercase word: `true` or `false`. Edit and test with `check_safety.py`.

The current built-in prompt biases toward `true` (allow) when ambiguous —
the cost of letting through one borderline message is much lower than
permanently disabling a pair.

### `polish-prompt.txt` — polish system prompt

Plain English. Re-read on every message (only when a `p` flag actually
fires, so no I/O cost for unpolished pairs). Should instruct the model to
**return only the rewritten text**.

### `rules.yaml` — analyze / auto-reply / rule-relay routing

YAML. Loaded once at startup. Older subsystem from before the pair file
existed; still active for **archiving + DeepSeek analysis + optional
auto-reply + optional rule-based relay**. Pair-based middleman work is
independent of this file.

Match fields: `chat_type`, `chat_id`, `from_user`, `text_regex`,
`chat_title_regex`. Action fields: `save`, `analyze`,
`analyze_system_prompt`, `auto_reply`, `relay_to`, `relay_template`.
First matching rule wins; defaults merge from `defaults`.

---

## Database schema (`telbot.db`, SQLite)

```
messages    id, tg_message_id, chat_id, chat_type, chat_title,
            from_id, from_username, from_name, text, date_ts,
            raw_json, received_at
            UNIQUE (chat_id, tg_message_id)

analyses    id, message_id FK, model, response, error, created_at

actions     id, message_id FK, kind, target, content, status, error, created_at

relay_map   id, a_chat_id, a_msg_id, b_chat_id, b_msg_id, created_at
            UNIQUE (a_chat_id, a_msg_id, b_chat_id, b_msg_id)
            -- maps each pair-relayed message to its counterpart;
            -- a_* is the original sender side, b_* is the relayed side
```

`actions.kind` values written by the current code:
- `reply` — AI auto-reply (rule-controlled).
- `relay` — old rule-based relay (uses `rules.yaml relay_to`).
- `pair_relay` — pair-based relay (this is the common one).
- `safety_block` — pair-relay blocked by AI safety check; pair was disabled.

`actions.status` values: `sent`, `error`, `blocked`, `skipped_inconclusive`.

---

## Message archive

Each incoming message is also written to:

```
msg/YYYYMMDD/<sender>.json
```

`<sender>` is the username (lowercased) or `id_<numeric_id>` when there's
no username. The file is a JSON array; new messages are appended via
atomic `*.tmp + rename`. Per-message read-modify-write is safe in asyncio
since the write block contains no awaits.

Each entry includes: `tg_message_id`, `chat_id`, `chat_type`,
`chat_title`, `from_id`, `from_username`, `from_name`, `text`, `date_ts`,
`date_iso`.

The archive runs **before** any AI calls, so even messages that fail
analysis or relay are saved.

---

## Setup

```bash
# 1. Get Telegram API credentials at https://my.telegram.org/apps
#    Get a DeepSeek key at https://platform.deepseek.com
# 2. Create venv and install deps:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 3. Configure:
cp .env.example .env
$EDITOR .env          # fill in TG_API_ID, TG_API_HASH, DEEPSEEK_API_KEY
# 4. First login (interactive; prompts for phone + Telegram code + 2FA password):
.venv/bin/python send_test.py @yourself "hello"
```

`send_test.py` creates `telbot.session`. After that, everything else
(`main.py`, `check_safety.py`) runs non-interactively against that
session.

---

## Running

```bash
.venv/bin/python main.py
```

Sits idle listening forever. Per-message log lines look like:

```
msg chat=Foo Bar(private) from=alice text='hey' rule=None
pair relay queued: alice -> bob in 4.32s
marked read chat=12345 up to msg_id=678 (from alice) after 1.87s
pair relayed to bob: 'hey'
```

Stop with Ctrl-C. Re-running picks up the same session.

For a long-lived deployment, run under a process supervisor (systemd
unit, Docker container, tmux, supervisord — any of them).

---

## Helpers

### `send_test.py`

```bash
.venv/bin/python send_test.py <@username_or_id> "message text"
```

Logs in (interactively on first run), sends one message, exits. Used to
seed the session file and to spot-check connectivity.

### `check_safety.py`

```bash
.venv/bin/python check_safety.py "your test message here"
.venv/bin/python check_safety.py < message.txt
```

Runs only the safety check against the current `stop-relay-rules.txt`,
prints the decision and the AI's raw response. Useful when tuning rules
or the prompt — no Telegram round-trip needed.

---

## Operational notes

- **Costs.** Each pair-relay candidate triggers one DeepSeek call for the
  safety check (skipped if `[i]`, or if the message is media-only with no
  caption), plus one more if `p` polish fires (also skipped on media-only
  messages). Channel and group messages are archived + saved + analyzed
  (rule defaults) but **not** relayed.

- **Rate-limit posture.** A random 1–10s pre-send delay is the only
  spacing applied. Telethon raises `FloodWaitError` if the user account
  is throttled; the current handler only logs the exception. If you
  start hitting flood waits, add backoff in `_maybe_pair_relay`.

- **Session security.** `telbot.session` is equivalent to a logged-in
  Telegram client. Anyone with the file can act as your account. The
  file is gitignored; back it up encrypted.

- **API hash security.** `TG_API_HASH` and `DEEPSEEK_API_KEY` in `.env`
  are credentials. `.env` is gitignored. Both can be rotated:
    - `api_hash` from https://my.telegram.org/apps (delete app, create new).
    - DeepSeek from the platform dashboard.

- **Disabled pairs from the safety check.** When the safety check blocks
  a relay, `[d]` is added in front of the existing flags on the pair
  line (so `alice[p]=bob` becomes `[d]alice[p]=bob`). To re-enable,
  remove the `[d]` — every next message will pick it up without a
  restart.

- **No anti-loop guard beyond `incoming=True`.** Your own outgoing
  relays are not re-processed, but if two paired accounts both auto-reply
  to each other through the relay, you can construct an infinite loop.
  Don't pair two automated chat participants together.
