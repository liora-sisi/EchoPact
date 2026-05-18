import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "echo_pact.db")

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.executescript(
    "CREATE TABLE IF NOT EXISTS memories ("
    "id                INTEGER PRIMARY KEY AUTOINCREMENT,"
    "content           TEXT    NOT NULL,"
    "summary           TEXT,"
    "valence           REAL    DEFAULT 0.0,"
    "arousal           REAL    DEFAULT 0.0,"
    "direction         TEXT    DEFAULT 'self',"
    "tags              TEXT,"
    "is_done           INTEGER DEFAULT 0,"
    "decay_category    TEXT    DEFAULT 'fact',"
    "importance        REAL    DEFAULT 0.5,"
    "recall_count      INTEGER DEFAULT 0,"
    "calculated_weight REAL    DEFAULT 0.0,"
    "agent_id          TEXT    DEFAULT 'default',"
    "source_type       TEXT    DEFAULT 'user',"
    "confidence        REAL    DEFAULT 1.0,"
    "conflict_group_id TEXT,"
    "last_verified_at  TEXT,"
    "created_at        TEXT    NOT NULL,"
    "updated_at        TEXT"
    ");"
    "CREATE TABLE IF NOT EXISTS interaction_log ("
    "id                INTEGER PRIMARY KEY AUTOINCREMENT,"
    "timestamp         TEXT    NOT NULL,"
    "user_msg          TEXT,"
    "ai_reply          TEXT,"
    "toxicity_score    REAL    DEFAULT 0.0,"
    "agent_id          TEXT    DEFAULT 'default'"
    ");"
    "CREATE TABLE IF NOT EXISTS system_meta ("
    "key TEXT PRIMARY KEY,"
    "value TEXT"
    ");"
   "CREATE TABLE IF NOT EXISTS sagas ("
    "id          INTEGER PRIMARY KEY AUTOINCREMENT,"
    "title       TEXT    NOT NULL,"
    "status      TEXT    DEFAULT 'active',"
    "agent_id    TEXT    DEFAULT 'default',"
    "created_at  TEXT    NOT NULL,"
    "updated_at  TEXT"
    ");"
    "CREATE TABLE IF NOT EXISTS user_profile ("
    "agent_id           TEXT PRIMARY KEY,"
    "conscientiousness  REAL DEFAULT 0.5,"
    "last_updated       TEXT"
    ");"
    "CREATE TABLE IF NOT EXISTS memory_conflicts ("
    "conflict_id       TEXT    PRIMARY KEY,"
    "group_id          TEXT    NOT NULL,"
    "fact1_id          INTEGER,"
    "fact2_id          INTEGER,"
    "conflict_type     TEXT,"
    "created_at        TEXT,"
    "resolved          INTEGER DEFAULT 0"
    ");"
        )
