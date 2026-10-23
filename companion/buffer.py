"""本地消息缓冲：只存白名单群里转换后的文字，不存图片、语音等原始内容。"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
import time
import typing

RETENTION_DAYS = 14
_PURGE_EVERY = 200  # 每写入这么多条清理一次过期记录

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    message_id  TEXT    NOT NULL,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT    NOT NULL DEFAULT '',
    ts          REAL    NOT NULL,
    text        TEXT    NOT NULL,
    is_self     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages (group_id, ts);
CREATE TABLE IF NOT EXISTS sent_media (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    tag      TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_media_group_ts ON sent_media (group_id, kind, ts);
CREATE TABLE IF NOT EXISTS triggers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    strength TEXT NOT NULL,
    outcome  TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_group_ts ON triggers (group_id, ts);
"""

# 0.2.0 建的库没有这些列：启动时补上
_MIGRATIONS = {
    'messages': {'reply_to': "ALTER TABLE messages ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''"},
}

_COLUMNS = 'group_id, message_id, sender_id, sender_name, ts, text, is_self, reply_to'


class Record(typing.NamedTuple):
    group_id: str
    message_id: str
    sender_id: str
    sender_name: str
    ts: float
    text: str
    is_self: bool
    reply_to: str = ''  # 这句话是对谁说的（@ 或回复的对象昵称，逗号分隔）


def _record(row: tuple) -> Record:
    return Record(row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]), row[7] or '')


class MessageBuffer:
    def __init__(self, data_dir: str | pathlib.Path, *, retention_days: int = RETENTION_DAYS):
        self.path = pathlib.Path(data_dir) / 'companion.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._writes = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self.purge()

    def _migrate(self) -> None:
        for table, columns in _MIGRATIONS.items():
            existing = {row[1] for row in self._conn.execute(f'PRAGMA table_info({table})')}
            for column, ddl in columns.items():
                if column not in existing:
                    self._conn.execute(ddl)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 消息 ----------

    def add(
        self,
        *,
        group_id: object,
        message_id: object,
        sender_id: object,
        sender_name: str,
        text: str,
        ts: float | None = None,
        is_self: bool = False,
        reply_to: str = '',
    ) -> bool:
        """写入一条；同一条消息重复上报时忽略。返回是否真的写入。"""
        with self._lock:
            cur = self._conn.execute(
                f'INSERT OR IGNORE INTO messages ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(group_id), str(message_id), str(sender_id), sender_name or '',
                    ts or time.time(), text, int(is_self), reply_to or '',
                ),
            )
            self._conn.commit()
            self._writes += 1
            inserted = cur.rowcount == 1
        if self._writes % _PURGE_EVERY == 0:
            self.purge()
        return inserted

    def _select(self, where: str, args: tuple, limit: int) -> list[Record]:
        """按条件取最新的 limit 条，返回时按时间从早到晚排列。"""
        with self._lock:
            rows = self._conn.execute(
                f'SELECT {_COLUMNS} FROM messages WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?',
                (*args, limit),
            ).fetchall()
        return [_record(row) for row in reversed(rows)]

    def recent(self, group_id: object, limit: int = 30) -> list[Record]:
        return self._select('group_id = ?', (str(group_id),), limit)

    def after(self, group_id: object, ts: float, limit: int = 30) -> list[Record]:
        """ts 之后（不含）的最多 limit 条。"""
        return self._select('group_id = ? AND ts > ?', (str(group_id), ts), limit)

    def since(self, group_id: object, ts: float, limit: int = 40) -> list[Record]:
        """ts 之后（含）的最多 limit 条。"""
        return self._select('group_id = ? AND ts >= ?', (str(group_id), ts), limit)

    def get(self, group_id: object, message_id: object) -> Record | None:
        with self._lock:
            row = self._conn.execute(
                f'SELECT {_COLUMNS} FROM messages WHERE group_id = ? AND message_id = ?',
                (str(group_id), str(message_id)),
            ).fetchone()
        return _record(row) if row else None

    def last_self(self, group_id: object) -> Record | None:
        """自己在本群最近一次发言。"""
        found = self._select('group_id = ? AND is_self = 1', (str(group_id),), 1)
        return found[0] if found else None

    def count_self_since(self, ts: float, group_id: object | None = None) -> int:
        """ts 之后自己发了多少条；group_id 为空时统计所有群。"""
        with self._lock:
            if group_id is None:
                sql, args = 'SELECT COUNT(*) FROM messages WHERE is_self = 1 AND ts >= ?', (ts,)
            else:
                sql, args = (
                    'SELECT COUNT(*) FROM messages WHERE is_self = 1 AND ts >= ? AND group_id = ?',
                    (ts, str(group_id)),
                )
            return self._conn.execute(sql, args).fetchone()[0]

    def name_of(self, group_id: object, qq: object) -> str | None:
        """这个人在本群最近一次用的昵称。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT sender_name FROM messages WHERE group_id = ? AND sender_id = ? AND sender_name != '' "
                'ORDER BY ts DESC LIMIT 1',
                (str(group_id), str(qq)),
            ).fetchone()
        return row[0] if row else None

    def count(self, group_id: object | None = None) -> int:
        with self._lock:
            if group_id is None:
                return self._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            return self._conn.execute('SELECT COUNT(*) FROM messages WHERE group_id = ?', (str(group_id),)).fetchone()[0]

    # ---------- 点名记录（额度用） ----------

    def record_trigger(self, group_id: object, user_id: object, strength: str, outcome: str, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT INTO triggers (group_id, user_id, strength, outcome, ts) VALUES (?, ?, ?, ?, ?)',
                (str(group_id), str(user_id), strength, outcome, ts or time.time()),
            )
            self._conn.commit()

    def count_triggers(
        self,
        *,
        since: float,
        outcome: str,
        group_id: object | None = None,
        user_id: object | None = None,
        strength: str | None = None,
    ) -> int:
        where, args = ['ts >= ?', 'outcome = ?'], [since, outcome]
        for column, value in (('group_id', group_id), ('user_id', user_id), ('strength', strength)):
            if value is not None:
                where.append(f'{column} = ?')
                args.append(str(value))
        with self._lock:
            return self._conn.execute(f'SELECT COUNT(*) FROM triggers WHERE {" AND ".join(where)}', args).fetchone()[0]

    # ---------- 发出的表情包 ----------

    def record_media(self, group_id: object, kind: str, tag: str, ts: float | None = None) -> None:
        """记下自己发出的表情包/图片，用于额度和防重复。"""
        with self._lock:
            self._conn.execute(
                'INSERT INTO sent_media (group_id, kind, tag, ts) VALUES (?, ?, ?, ?)',
                (str(group_id), kind, tag, ts or time.time()),
            )
            self._conn.commit()

    def count_media_since(self, group_id: object, kind: str, ts: float) -> int:
        with self._lock:
            return self._conn.execute(
                'SELECT COUNT(*) FROM sent_media WHERE group_id = ? AND kind = ? AND ts >= ?',
                (str(group_id), kind, ts),
            ).fetchone()[0]

    def media_sent_since(self, group_id: object, kind: str, tag: str, ts: float) -> bool:
        with self._lock:
            row = self._conn.execute(
                'SELECT 1 FROM sent_media WHERE group_id = ? AND kind = ? AND tag = ? AND ts >= ? LIMIT 1',
                (str(group_id), kind, tag, ts),
            ).fetchone()
        return row is not None

    def purge(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            cur = self._conn.execute('DELETE FROM messages WHERE ts < ?', (cutoff,))
            self._conn.execute('DELETE FROM sent_media WHERE ts < ?', (cutoff,))
            self._conn.execute('DELETE FROM triggers WHERE ts < ?', (cutoff,))
            self._conn.commit()
            return cur.rowcount
