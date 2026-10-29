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
    assert result[food] == {"emoji": "🍔", "color": "orange"}
    assert result[snacks]["emoji"] == "🍔"  # nothing in "Snacks" itself, so its group's
    assert result[snacks]["color"] not in ("orange", "aqua")  # but a color of its own
    assert result[mine] == {"emoji": "🥦", "color": "aqua"}
    assert result[odd]["emoji"] == looks.FALLBACK_EMOJI and result[odd]["color"] in looks.FALLBACK_COLORS
    assert looks.transaction_look(result, "income", mine) == looks.INCOME
    assert looks.transaction_look(result, "transfer", None) == looks.TRANSFER
    assert looks.transaction_look(result, "regular", None) == looks.UNCATEGORIZED
    assert looks.transaction_look(result, "regular", mine) == result[mine]


def test_categories_get_colors_of_their_own(conn):
    mine = conn.execute("INSERT INTO categories (name, color) VALUES ('Lunch', 'orange')").lastrowid
    ids = [conn.execute("INSERT INTO categories (name) VALUES (?)", (name,)).lastrowid
           for name in ("Dinner", "Takeout", "Zzyzx", "Other", "Misc")]
    result = looks.category_looks(conn)
    assert result[mine]["color"] == "orange"  # the user's pick, which the others then go without
    colors = [result[cid]["color"] for cid in ids]
    assert len(set(colors[:3])) == 3 and "orange" not in colors[:3]
    assert colors[3:] == ["gray", "gray"]  # gray isn't meant to stand out, so it's shared
    assert result[ids[0]]["emoji"] == result[ids[1]]["emoji"] == "🍔"  # only the color changes


def test_categories_that_hold_transactions_choose_colors_before_their_groups(conn):
    group = conn.execute("INSERT INTO categories (name) VALUES ('Coffee & Tea')").lastrowid
    child = conn.execute("INSERT INTO categories (name, parent_id) VALUES ('Cafes', ?)", (group,)).lastrowid
    result = looks.category_looks(conn)
    assert result[child]["color"] == "brown" and result[group]["color"] != "brown"


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
