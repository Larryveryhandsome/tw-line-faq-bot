"""SQLite：聯絡人、訊息紀錄、待老闆決定的事。"""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT,
    role         TEXT NOT NULL DEFAULT 'customer',   -- owner / customer
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    direction  TEXT NOT NULL,          -- in / out
    text       TEXT,
    intent     TEXT,
    confidence REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, created_at);
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id TEXT,
    question     TEXT NOT NULL,
    context      TEXT,
    options      TEXT NOT NULL,        -- JSON
    chosen       TEXT,
    free_text    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / waiting_text / resolved
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status, id);
CREATE INDEX IF NOT EXISTS idx_decisions_user ON decisions(from_user_id, id);
-- LINE 重送同一事件（webhookEventId）只處理一次：避免重複回覆、重複推卡、重複扣 Claude 費用
CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY, ts INTEGER NOT NULL);
-- 老闆認證碼嘗試紀錄：防暴力猜測
CREATE TABLE IF NOT EXISTS claim_fails (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, ts INTEGER NOT NULL);
-- Claude 呼叫紀錄：每人／全店額度控管，避免被洗版燒錢
CREATE TABLE IF NOT EXISTS ai_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ai_calls_ts ON ai_calls(ts);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 30000")
        c.execute("PRAGMA journal_mode = WAL")  # 讀寫不互卡，多人同時傳訊也穩
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # 冪等：同一個 webhookEventId 只回 True 一次
    def claim_event(self, event_id) -> bool:
        now = int(time.time())
        with self.conn() as c:
            n = c.execute("INSERT OR IGNORE INTO processed_events (event_id, ts) VALUES (?,?)",
                          (event_id, now)).rowcount
            if n and now % 50 == 0:  # 偶爾清掉 7 天前的，表不會無限長大
                c.execute("DELETE FROM processed_events WHERE ts < ?", (now - 7 * 86400,))
            return bool(n)

    # 老闆認證碼嘗試
    def claim_fail_count(self, since, user_id=None) -> int:
        q, a = "SELECT COUNT(*) n FROM claim_fails WHERE ts >= ?", [since]
        if user_id:
            q, a = q + " AND user_id=?", a + [user_id]
        with self.conn() as c:
            return c.execute(q, a).fetchone()["n"]

    def add_claim_fail(self, user_id, now=None):
        with self.conn() as c:
            c.execute("INSERT INTO claim_fails (user_id, ts) VALUES (?,?)", (user_id, int(now or time.time())))

    # Claude 用量
    def ai_count(self, since, user_id=None) -> int:
        q, a = "SELECT COUNT(*) n FROM ai_calls WHERE ts >= ?", [since]
        if user_id:
            q, a = q + " AND user_id=?", a + [user_id]
        with self.conn() as c:
            return c.execute(q, a).fetchone()["n"]

    def add_ai_call(self, user_id, now=None):
        with self.conn() as c:
            c.execute("INSERT INTO ai_calls (user_id, ts) VALUES (?,?)", (user_id, int(now or time.time())))

    def decisions_since(self, user_id, since_iso) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) n FROM decisions WHERE from_user_id=? AND created_at >= ?",
                             (user_id, since_iso)).fetchone()["n"]

    def get_contact(self, user_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM contacts WHERE user_id=?", (user_id,)).fetchone()

    # contacts
    def touch_contact(self, user_id, display_name=None):
        now = _now()
        with self.conn() as c:
            c.execute(
                """INSERT INTO contacts (user_id, display_name, first_seen, last_seen) VALUES (?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen,
                     display_name=COALESCE(excluded.display_name, contacts.display_name)""",
                (user_id, display_name, now, now))

    def set_role(self, user_id, role):
        with self.conn() as c:
            c.execute("UPDATE contacts SET role=? WHERE user_id=?", (role, user_id))

    def owner_ids(self):
        with self.conn() as c:
            return [r["user_id"] for r in c.execute("SELECT user_id FROM contacts WHERE role='owner'")]

    # messages
    def log(self, user_id, direction, text, intent=None, confidence=None):
        with self.conn() as c:
            c.execute("INSERT INTO messages (user_id, direction, text, intent, confidence, created_at)"
                      " VALUES (?,?,?,?,?,?)", (user_id, direction, text, intent, confidence, _now()))

    def messages(self, user_id=None):
        q, a = "SELECT * FROM messages", ()
        if user_id:
            q, a = q + " WHERE user_id=?", (user_id,)
        with self.conn() as c:
            return c.execute(q + " ORDER BY id", a).fetchall()

    # decisions
    def create_decision(self, from_user_id, question, context, options, status="pending"):
        with self.conn() as c:
            cur = c.execute("INSERT INTO decisions (from_user_id, question, context, options, status, created_at)"
                            " VALUES (?,?,?,?,?,?)",
                            (from_user_id, question, context, json.dumps(options, ensure_ascii=False),
                             status, _now()))
            return cur.lastrowid

    def get_decision(self, did):
        with self.conn() as c:
            return c.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()

    def set_decision(self, did, status, chosen=None, free_text=None):
        with self.conn() as c:
            c.execute("UPDATE decisions SET status=?, chosen=COALESCE(?, chosen), free_text=?,"
                      " resolved_at=CASE WHEN ?='resolved' THEN ? ELSE resolved_at END WHERE id=?",
                      (status, chosen, free_text, status, _now(), did))

    def open_decisions(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM decisions WHERE status IN ('pending','waiting_text')"
                             " ORDER BY id").fetchall()

    def text_target(self):
        """老闆直接打字時要回給誰：先找按了「我來回」的，再找最新一筆待決。"""
        with self.conn() as c:
            row = c.execute("SELECT * FROM decisions WHERE status='waiting_text' ORDER BY id DESC LIMIT 1").fetchone()
            return row or c.execute("SELECT * FROM decisions WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
