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
        conn.executescript('''
            conn.executescript('''
    CREATE TABLE IF NOT EXISTS memories (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        content           TEXT    NOT NULL,
        summary           TEXT,
        valence           REAL    DEFAULT 0.0,
        arousal           REAL    DEFAULT 0.0,
        direction         TEXT    DEFAULT 'self',
        tags              TEXT,
        is_done           INTEGER DEFAULT 0,
        decay_category    TEXT    DEFAULT 'fact',
        importance        REAL    DEFAULT 0.5,
        recall_count      INTEGER DEFAULT 0,
        calculated_weight REAL    DEFAULT 0.0,
        created_at        TEXT    NOT NULL,
        updated_at        TEXT
    );
''')
