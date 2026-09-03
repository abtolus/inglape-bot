from datetime import datetime, timezone, timedelta

import sqlite3

def __init__():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        utc_offset INTEGER DEFAULT 0
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS words (
        word_id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        pos TEXT NOT NULL,
        UNIQUE(word, pos)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
        user_id INTEGER NOT NULL,
        word_id TEXT NOT NULL,
        studied_at INTEGER,
        PRIMARY KEY (user_id, word_id),
        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
        FOREIGN KEY (word_id) REFERENCES words(word_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

def add_user(user: object) -> None:
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, joined_at)
        VALUES (?, ?, ?, ?, ?);
    """, (user.id, user.username, user.first_name, user.last_name, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    __init__()