from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id   INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    chat_type       TEXT,
    chat_title      TEXT,
    from_id         INTEGER,
    from_username   TEXT,
    from_name       TEXT,
    text            TEXT,
    date_ts         INTEGER,
    raw_json        TEXT,
    received_at     INTEGER NOT NULL,
    UNIQUE (chat_id, tg_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, date_ts);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages (from_id, date_ts);

CREATE TABLE IF NOT EXISTS analyses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    response    TEXT,
    error       TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    target      TEXT,
    content     TEXT,
    status      TEXT NOT NULL,
    error       TEXT,
    created_at  INTEGER NOT NULL
);

-- Maps each pair-relayed message to its counterpart on the other side.
-- Always stored as (source -> target): a_* is the original sender's side,
-- b_* is the chat the relay landed in.
CREATE TABLE IF NOT EXISTS relay_map (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    a_chat_id    INTEGER NOT NULL,
    a_msg_id     INTEGER NOT NULL,
    b_chat_id    INTEGER NOT NULL,
    b_msg_id     INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE (a_chat_id, a_msg_id, b_chat_id, b_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_relay_map_a ON relay_map (a_chat_id, a_msg_id);
CREATE INDEX IF NOT EXISTS idx_relay_map_b ON relay_map (b_chat_id, b_msg_id);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "DB not connected"
        return self._conn

    async def save_message(self, m: dict[str, Any]) -> int:
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO messages
              (tg_message_id, chat_id, chat_type, chat_title,
               from_id, from_username, from_name, text, date_ts, raw_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                m["tg_message_id"], m["chat_id"], m.get("chat_type"), m.get("chat_title"),
                m.get("from_id"), m.get("from_username"), m.get("from_name"),
                m.get("text"), m.get("date_ts"),
                json.dumps(m.get("raw") or {}, default=str, ensure_ascii=False),
                int(time.time()),
            ),
        )
        await self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        # Already existed — fetch the existing row id.
        row = await (await self.conn.execute(
            "SELECT id FROM messages WHERE chat_id=? AND tg_message_id=?",
            (m["chat_id"], m["tg_message_id"]),
        )).fetchone()
        return int(row["id"])

    async def save_analysis(self, message_id: int, model: str,
                            response: str | None, error: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO analyses (message_id, model, response, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, model, response, error, int(time.time())),
        )
        await self.conn.commit()

    async def save_action(self, message_id: int, kind: str, target: str | None,
                          content: str | None, status: str, error: str | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO actions (message_id, kind, target, content, status, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, kind, target, content, status, error, int(time.time())),
        )
        await self.conn.commit()

    async def save_relay_map(self, a_chat_id: int, a_msg_id: int,
                             b_chat_id: int, b_msg_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO relay_map "
            "(a_chat_id, a_msg_id, b_chat_id, b_msg_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (a_chat_id, a_msg_id, b_chat_id, b_msg_id, int(time.time())),
        )
        await self.conn.commit()

    async def get_relay_counterpart(self, chat_id: int, msg_id: int
                                    ) -> tuple[int, int, bool] | None:
        """Return (other_chat_id, other_msg_id, source_side) for the relay
        counterpart of (chat_id, msg_id), or None if not relayed.

        source_side is True when (chat_id, msg_id) was the ORIGINAL sender's
        message (a_* row), False when it was the relayed copy on the target
        side (b_* row).
        """
        row = await (await self.conn.execute(
            "SELECT b_chat_id, b_msg_id FROM relay_map "
            "WHERE a_chat_id=? AND a_msg_id=? LIMIT 1",
            (chat_id, msg_id),
        )).fetchone()
        if row:
            return (int(row["b_chat_id"]), int(row["b_msg_id"]), True)
        row = await (await self.conn.execute(
            "SELECT a_chat_id, a_msg_id FROM relay_map "
            "WHERE b_chat_id=? AND b_msg_id=? LIMIT 1",
            (chat_id, msg_id),
        )).fetchone()
        if row:
            return (int(row["a_chat_id"]), int(row["a_msg_id"]), False)
        return None

    async def find_latest_relay_in_chat(self, b_chat_id: int, max_b_msg_id: int
                                        ) -> tuple[int, int] | None:
        """Find the largest b_msg_id <= max_b_msg_id in the given b_chat and
        return (a_chat_id, a_msg_id) — the source-side counterpart. Used to
        propagate read receipts: when the target side reads up to msg N in
        their chat with the bot, marking the corresponding a_msg as read in
        a_chat (which implicitly marks all earlier a_chat msgs as read too)
        gives the source user a faithful "they actually read it" signal.
        """
        row = await (await self.conn.execute(
            "SELECT a_chat_id, a_msg_id FROM relay_map "
            "WHERE b_chat_id=? AND b_msg_id<=? "
            "ORDER BY b_msg_id DESC LIMIT 1",
            (b_chat_id, max_b_msg_id),
        )).fetchone()
        if not row:
            return None
        return (int(row["a_chat_id"]), int(row["a_msg_id"]))

    async def get_recent_relay_rows(self, since_ts: int
                                    ) -> list[tuple[int, int, int, int]]:
        """Return `(a_chat_id, a_msg_id, b_chat_id, b_msg_id)` for every relay
        recorded at or after `since_ts`. Used by the reaction-polling loop:
        Telegram does not push `updateMessageReactions` to user accounts for
        1-on-1 private chats, so we re-fetch the relayed messages on a timer
        and mirror any reaction changes."""
        rows = await (await self.conn.execute(
            "SELECT a_chat_id, a_msg_id, b_chat_id, b_msg_id FROM relay_map "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (since_ts,),
        )).fetchall()
        return [(int(r["a_chat_id"]), int(r["a_msg_id"]),
                 int(r["b_chat_id"]), int(r["b_msg_id"])) for r in rows]

    async def find_relay_by_msg_id(self, msg_id: int
                                   ) -> list[tuple[int, int, bool]]:
        """Search the map by msg_id alone — used for MessageDeleted in private
        chats where Telegram does not include the chat_id. Within a user
        account, private-chat msg_ids are globally sequential, so collisions
        across rows are not expected. Each match is returned as
        (other_chat_id, other_msg_id, source_side).
        """
        rows = await (await self.conn.execute(
            "SELECT a_chat_id, a_msg_id, b_chat_id, b_msg_id FROM relay_map "
            "WHERE a_msg_id=? OR b_msg_id=?",
            (msg_id, msg_id),
        )).fetchall()
        out: list[tuple[int, int, bool]] = []
        for row in rows:
            if int(row["a_msg_id"]) == msg_id:
                out.append((int(row["b_chat_id"]), int(row["b_msg_id"]), True))
            elif int(row["b_msg_id"]) == msg_id:
                out.append((int(row["a_chat_id"]), int(row["a_msg_id"]), False))
        return out
