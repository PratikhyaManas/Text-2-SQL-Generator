import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture()
def sample_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?)",
        [(1, "Ava Chen", "ava@example.com"), (2, "Liam Patel", "liam@example.com")],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?, ?, ?)",
        [(1, "Mouse", 24.99), (2, "Keyboard", 89.99)],
    )
    conn.commit()
    conn.close()
    return db_path
