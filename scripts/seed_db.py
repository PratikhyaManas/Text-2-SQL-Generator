"""
Creates data/sample.db with a small e-commerce schema and seed data,
so the whole pipeline can be exercised immediately after cloning.

Run with: python scripts/seed_db.py
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sample.db")
DB_PATH = os.path.abspath(DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    signup_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
"""

CUSTOMERS = [
    (1, "Ava Chen", "ava@example.com", "2025-01-10"),
    (2, "Liam Patel", "liam@example.com", "2025-02-14"),
    (3, "Noah Garcia", "noah@example.com", "2025-03-02"),
    (4, "Mia Rossi", "mia@example.com", "2025-03-20"),
    (5, "Sofia Müller", "sofia@example.com", "2025-04-05"),
]

PRODUCTS = [
    (1, "Wireless Mouse", "Electronics", 24.99),
    (2, "Mechanical Keyboard", "Electronics", 89.99),
    (3, "USB-C Hub", "Electronics", 34.50),
    (4, "Notebook", "Office", 4.25),
    (5, "Desk Lamp", "Office", 19.99),
]

ORDERS = [
    (1, 1, "2025-05-01"),
    (2, 2, "2025-05-03"),
    (3, 1, "2025-05-10"),
    (4, 3, "2025-05-12"),
    (5, 4, "2025-05-15"),
    (6, 5, "2025-05-18"),
]

ORDER_ITEMS = [
    (1, 1, 1, 2),
    (2, 1, 2, 1),
    (3, 2, 3, 1),
    (4, 3, 4, 5),
    (5, 4, 2, 1),
    (6, 5, 1, 1),
    (7, 5, 5, 2),
    (8, 6, 3, 1),
]


def main() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)", CUSTOMERS
        )
        conn.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?)", PRODUCTS
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)", ORDERS
        )
        conn.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?)", ORDER_ITEMS
        )
        conn.commit()
        print(f"Seeded sample database at {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
