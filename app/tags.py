"""Tags: labels the user puts on transactions, any number each, to find them again later (such as what's left to check)."""

import re
import sqlite3

from .looks import COLORS, FALLBACK_COLORS

MAX_NAME = 40  # characters


def clean_name(name: str) -> str:
    """Returns a tag name with its spaces tidied and its length capped."""
    return re.sub(r"\s+", " ", name).strip()[:MAX_NAME].strip()


def parse_names(text: str) -> list[str]:
    """Returns the tag names in comma-separated `text`, tidied, each once (ignoring letter case)."""
    names: dict[str, str] = {}
    for part in text.split(","):
        name = clean_name(part)
        if name:
            names.setdefault(name.lower(), name)
    return list(names.values())


def tag_looks(conn: sqlite3.Connection) -> dict[int, dict]:
    """Returns {tag id: {"id", "name", "color"}} for every tag, ordered by name. The color is the user's pick, else one
    that depends on the tag's id."""
    return {
        r["id"]: {"id": r["id"], "name": r["name"],
                  "color": r["color"] if r["color"] in COLORS else FALLBACK_COLORS[(r["id"] - 1) % len(FALLBACK_COLORS)]}
        for r in conn.execute("SELECT id, name, color FROM tags ORDER BY name COLLATE NOCASE, id")
    }


def get_or_create(conn: sqlite3.Connection, name: str, color: str | None = None) -> int:
    """Returns the id of the tag with this name (any letter case), creating it when needed, in `color` if that's one of
    looks.COLORS."""
    name = clean_name(name)
    row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO tags (name, color) VALUES (?, ?)", (name, color if color in COLORS else None)).lastrowid


def tags_by_transaction(conn: sqlite3.Connection, transaction_ids: list[int]) -> dict[int, list[int]]:
    """Returns {transaction id: [its tag ids]} for the given transactions; ones without tags are left out."""
    out: dict[int, list[int]] = {}
    for start in range(0, len(transaction_ids), 500):
        chunk = transaction_ids[start:start + 500]
        for r in conn.execute(
            f"SELECT transaction_id, tag_id FROM transaction_tags WHERE transaction_id IN ({','.join('?' * len(chunk))})", chunk
        ):
            out.setdefault(r["transaction_id"], []).append(r["tag_id"])
    return out


def set_tags(conn: sqlite3.Connection, transaction_id: int, tag_ids: list[int]) -> None:
    """Gives the transaction exactly these tags, leaving out ids that aren't tags."""
    known = {r[0] for r in conn.execute("SELECT id FROM tags")}
    conn.execute("DELETE FROM transaction_tags WHERE transaction_id = ?", (transaction_id,))
    conn.executemany("INSERT OR IGNORE INTO transaction_tags (transaction_id, tag_id) VALUES (?, ?)",
                     [(transaction_id, g) for g in tag_ids if g in known])


def add(conn: sqlite3.Connection, tag_id: int, transaction_ids: list[int]) -> None:
    """Puts the tag on each of the transactions that doesn't have it yet."""
    conn.executemany("INSERT OR IGNORE INTO transaction_tags (transaction_id, tag_id) VALUES (?, ?)",
                     [(t, tag_id) for t in transaction_ids])


def remove(conn: sqlite3.Connection, tag_id: int, transaction_ids: list[int]) -> int:
    """Takes the tag off the transactions and returns how many had it."""
    return sum(conn.execute("DELETE FROM transaction_tags WHERE tag_id = ? AND transaction_id = ?", (tag_id, t)).rowcount
               for t in transaction_ids)
