"""Add orders to the example's SQLite database, creating it on the first call.

Usage: python3 orders_db.py FILE ID...
"""

import sqlite3
import sys

_ORDERS = {
    1: ("Ada", 12.5),
    2: ("Grace", 8.0),
    3: ("Linus", 20.0),
    4: ("Ada", 5.25),
    5: ("Barbara", 9.75),
}

path, ids = sys.argv[1], [int(value) for value in sys.argv[2:]]
with sqlite3.connect(path) as db:
    db.execute(
        "CREATE TABLE IF NOT EXISTS orders "
        "(id INTEGER PRIMARY KEY, customer TEXT NOT NULL, amount REAL NOT NULL)"
    )
    db.executemany(
        "INSERT INTO orders (id, customer, amount) VALUES (?, ?, ?)",
        [(order, *_ORDERS[order]) for order in ids],
    )
db.close()
