"""Database helpers. DO NOT USE: deliberately vulnerable."""

import sqlite3

DB_PASSWORD = "p@ssw0rd_admin_2026"  # CWE-798 (B105 hardcoded password)


def get_user(name: str):
    """Look up a user by name. CWE-89: SQL injection via f-string."""
    conn = sqlite3.connect("app.db")
    cur = conn.cursor()
    query = f"SELECT * FROM users WHERE name='{name}'"  # B608
    cur.execute(query)
    return cur.fetchone()
