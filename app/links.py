"""Linking transactions that belong together, like rent paid in two payments, or an order and the protection plan
charged for one of its items. Unlike a split or a combined line (splits.py), there's no transaction above them: each
stays a transaction of its own, counting on its own date and in its own category, and the list shows them together, as
equals, on the biggest one's day (main.SHOWN_DAY, main.linked_together, main.chained). A split is linked as a whole,
through the transaction its parts are split from. Linked transactions share a number in transactions.link_id."""

import sqlite3

# Transaction {t} is linked with at least one other. One left on its own (the bank removed the others) isn't.
LINKED = "EXISTS (SELECT 1 FROM transactions l WHERE l.link_id = {t}.link_id AND l.id != {t}.id)"


def is_linked(conn: sqlite3.Connection, t) -> bool:
    """Returns whether transaction `t` is linked with any other."""
    return t["link_id"] is not None and conn.execute(
        "SELECT 1 FROM transactions WHERE link_id = ? AND id != ?", (t["link_id"], t["id"])).fetchone() is not None


def wholes(conn: sqlite3.Connection, rows) -> list[sqlite3.Row]:
    """Returns transactions `rows` with each part of a split replaced by the transaction it's split from, each once."""
    out, seen = [], set()
    for r in rows:
        if r["split_from_id"]:
            r = conn.execute("SELECT * FROM transactions WHERE id = ?", (r["split_from_id"],)).fetchone()
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def link_problem(conn: sqlite3.Connection, rows) -> str | None:
    """Returns why transactions `rows` can't be linked together, or None if they can. A part of a split can't, as its
    split links as a whole (see wholes), and neither can a transfer paired with its other side. One without it can,
    like money moved through a friend in several payments."""
    if len(rows) < 2:
        return "Pick at least two transactions to link."
    for r in rows:
        if r["split_from_id"]:
            return f"“{r['name']}” is part of a split. Link the split instead."
        if r["type"] == "transfer" and r["transfer_id"]:
            return f"“{r['name']}” is a move between your accounts, linked with its other side already."
        if r["reimburses_id"]:
            return f"“{r['name']}” pays back a purchase, so it's listed with that purchase already."
    return None


def link(conn: sqlite3.Connection, rows) -> int:
    """Links transactions `rows` together, with whatever each of them was linked with already, and returns the number
    they now share."""
    existing = sorted({r["link_id"] for r in rows if r["link_id"] is not None})
    link_id = existing[0] if existing else conn.execute("SELECT coalesce(max(link_id), 0) + 1 FROM transactions").fetchone()[0]
    conn.executemany("UPDATE transactions SET link_id = ? WHERE link_id = ?", [(link_id, other) for other in existing[1:]])
    conn.executemany("UPDATE transactions SET link_id = ? WHERE id = ?", [(link_id, r["id"]) for r in rows])
    return link_id


def unlink(conn: sqlite3.Connection, t) -> None:
    """Takes transaction `t` out of the transactions it's linked with. When only one is left, it isn't linked anymore."""
    if t["link_id"] is None:
        return
    conn.execute("UPDATE transactions SET link_id = NULL WHERE id = ?", (t["id"],))
    left = conn.execute("SELECT id FROM transactions WHERE link_id = ?", (t["link_id"],)).fetchall()
    if len(left) < 2:
        conn.execute("UPDATE transactions SET link_id = NULL WHERE link_id = ?", (t["link_id"],))
