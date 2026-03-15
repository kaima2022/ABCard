"""
SQLite 数据库管理 - 兑换码 & 执行记录
"""
import sqlite3
import os
import threading
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get("ABC_DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接 (线程安全)"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


@contextmanager
def get_db():
    """数据库连接上下文管理器"""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    """初始化数据库表"""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS codes (
                code        TEXT PRIMARY KEY,
                total_uses  INTEGER NOT NULL DEFAULT 1,
                used_count  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                expires_at  TEXT,
                note        TEXT
            );

            CREATE TABLE IF NOT EXISTS executions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                code            TEXT NOT NULL,
                email           TEXT,
                plan_type       TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                reserved_amount INTEGER NOT NULL DEFAULT 1,
                error_msg       TEXT,
                result_json     TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT,
                FOREIGN KEY (code) REFERENCES codes(code)
            );

            CREATE INDEX IF NOT EXISTS idx_executions_code ON executions(code);
            CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(status);
        """)
        # 自动迁移: 添加 reserved_amount 列 (如果不存在)
        try:
            conn.execute("SELECT reserved_amount FROM executions LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE executions ADD COLUMN reserved_amount INTEGER NOT NULL DEFAULT 1")


# ── 初始化 ──
init_db()
