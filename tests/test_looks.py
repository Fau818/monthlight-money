import sqlite3

from app import looks
from app.db import connect


def test_names_suggest_an_emoji_and_color():
    assert looks.suggest("Groceries") == ("🛒", "green")
    assert looks.suggest("Car & Gas") == ("🚗", "blue")  # the first hint that matches wins
    assert looks.suggest("Gas & Electric") == ("💡", "yellow")
    assert looks.suggest("Personal Care") == ("💅", "magenta")  # "care" isn't "car"
    assert looks.suggest("Bank Fees") == ("💸", "red")
    assert looks.suggest("Toffee") is None  # "fee" inside a word doesn't count
    assert looks.suggest("Zzyzx") is None


def test_category_looks_prefer_the_users_pick_then_the_group(conn):
    food = conn.execute("INSERT INTO categories (name) VALUES ('Food')").lastrowid
    snacks = conn.execute("INSERT INTO categories (name, parent_id) VALUES ('Snacks', ?)", (food,)).lastrowid
    mine = conn.execute("INSERT INTO categories (name, emoji, color) VALUES ('Groceries', '🥦', 'aqua')").lastrowid
    odd = conn.execute("INSERT INTO categories (name, color) VALUES ('Zzyzx', 'not-a-color')").lastrowid
    result = looks.category_looks(conn)
    assert result[snacks] == {"emoji": "🍔", "color": "orange"}  # nothing in "Snacks" itself, so its group's
    assert result[mine] == {"emoji": "🥦", "color": "aqua"}
    assert result[odd]["emoji"] == looks.FALLBACK_EMOJI and result[odd]["color"] in looks.FALLBACK_COLORS
    assert looks.transaction_look(result, "income", mine) == looks.INCOME
    assert looks.transaction_look(result, "transfer", None) == looks.TRANSFER
    assert looks.transaction_look(result, "regular", None) == looks.UNCATEGORIZED
    assert looks.transaction_look(result, "regular", mine) == result[mine]


def test_older_databases_get_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER, budget REAL, "
                "excluded INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO categories (name) VALUES ('Food')")
    old.commit()
    old.close()
    conn = connect(path)
    assert {"emoji", "color"} <= {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
    assert tuple(conn.execute("SELECT name, emoji, color FROM categories").fetchone()) == ("Food", None, None)
    conn.close()
    connect(path).close()  # the next time, the columns are already there
