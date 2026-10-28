"""Splitting a transaction into parts, as in Copilot: one transaction becomes several, each with its own amount, category
and name, and each part can then be changed like any other transaction. The parts add up to the original, which stays as
the bank sent it, so syncs keep updating it, but counts only through its parts (reports.NOT_SPLIT)."""

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from . import links

_AMOUNT = re.compile(r"\d+(\.\d{0,2})?|\.\d{1,2}")


@dataclass
class Part:
    id: int | None  # an existing part, or None for a new one
    name: str
    cents: int  # how much, always above zero; the part has the sign of the transaction it's split from
    category_id: int | None


# Rows that can be combined into one (see combine): no bank record, not in a split, not linked to another row.
_COMBINABLE = """{t}.source IN ('copilot', 'manual') AND {t}.plaid_transaction_id IS NULL AND {t}.split_from_id IS NULL
                 AND {t}.transfer_id IS NULL AND {t}.reimburses_id IS NULL
                 AND NOT EXISTS (SELECT 1 FROM transactions s WHERE s.split_from_id = {t}.id) AND NOT """ + links.LINKED


def parts_of(conn: sqlite3.Connection, tid: int) -> list[sqlite3.Row]:
    """Returns the parts transaction `tid` is split into, in the order they were made."""
    return conn.execute("SELECT * FROM transactions WHERE split_from_id = ? ORDER BY id", (tid,)).fetchall()


def split_problem(conn: sqlite3.Connection, t) -> str | None:
    """Returns why transaction `t` can't be split, or None if it can."""
    if t["split_from_id"]:
        return "This is already part of a split. To change it, edit the split on the original."
    if t["type"] == "transfer" or t["transfer_id"]:
        return "A move between your accounts can't be split."
    if t["reimburses_id"]:
        return "Money that pays back a purchase can't be split. Unlink it from the purchase first."
    if conn.execute("SELECT 1 FROM transactions WHERE reimburses_id = ?", (t["id"],)).fetchone():
        return "Unlink this purchase's paybacks before splitting it. Afterwards you can link them to one of its parts."
    if links.is_linked(conn, t):
        return "It's linked with other transactions. Unlink it first."
    if abs(t["amount"]) < 0.005:
        return "A transaction of $0.00 can't be split."
    return None


def dollars(cents: int) -> str:
    """Returns an amount in cents as dollars, e.g. "$1,234.50"."""
    return f"${cents / 100:,.2f}"


def to_cents(text: str) -> int | None:
    """Returns the amount typed in `text`, in cents, ignoring "$" and ","; None when it's empty. Raises ValueError when
    it isn't an amount without a sign."""
    text = text.strip().replace("$", "").replace(",", "")
    if not text:
        return None
    if not _AMOUNT.fullmatch(text):
        raise ValueError(text)
    return int(Decimal(text) * 100)


def read_parts(ids: list[str], names: list[str], amounts: list[str], categories: list[str], total_cents: int,
               default_name: str) -> tuple[list[Part], str | None]:
    """Returns the parts typed into the split form and None, or [] and why they can't be saved. The first four
    arguments are the form's columns, one item per row. A row left without a name or an amount is skipped (emptying a
    part's row removes the part), a part without a name gets `default_name`, and one part without an amount gets
    what's left of `total_cents`."""
    if not len(ids) == len(names) == len(amounts) == len(categories):
        return [], "The split didn't come through completely. Try again."
    rows = []
    for pid, name, amount, category in zip(ids, names, amounts, categories):
        name = name.strip()
        if not name and not amount.strip():
            continue
        try:
            cents = to_cents(amount)
        except ValueError:
            return [], f"“{amount.strip()}” isn't an amount. Type it like 12.50, without a sign."
        if cents is not None and cents <= 0:
            return [], "Each part needs an amount above zero."
        rows.append(Part(int(pid) if pid.isdigit() else None, name or default_name, cents, int(category) if category.isdigit() else None))
    if len(rows) < 2:
        return [], "A split needs at least two parts."
    ids_given = [p.id for p in rows if p.id is not None]
    if len(ids_given) != len(set(ids_given)):
        return [], "The split didn't come through completely. Try again."
    blank = [p for p in rows if p.cents is None]
    typed = sum(p.cents for p in rows if p.cents is not None)
    if len(blank) > 1:
        return [], "Type the amounts of all the parts but one; that one gets what's left."
    if blank:
        if typed >= total_cents:
            return [], f"The other parts already add up to {dollars(typed)}, so nothing is left for the part without an amount."
        blank[0].cents = total_cents - typed
    elif typed != total_cents:
        return [], f"The parts add up to {dollars(typed)}, but the transaction is {dollars(total_cents)}."
    return rows, None


def save(conn: sqlite3.Connection, t, parts: list[Part]) -> int:
    """Makes `parts` the parts of transaction `t`, and marks `t` as reviewed and edited by hand (so syncs keep its name
    and type). Existing parts are updated and new ones made, with `t`'s date, type and tags. Only the parts are excluded
    from then on, each on its own: `t`'s exclusion passes to them. A part left out is taken out of the split (see
    release). Returns how many parts left out became transactions of their own again; raises
    ValueError, saying why, if a part belongs to another transaction or deleting one would lose a link."""
    existing = {r["id"]: r for r in parts_of(conn, t["id"])}
    kept = {p.id for p in parts if p.id is not None}
    if not kept <= existing.keys():
        raise ValueError("One of those parts belongs to another transaction. Reload the page and try again.")
    left_out = [row for pid, row in existing.items() if pid not in kept]
    _check_links(conn, left_out)
    # Once split, the parts carry the exclusion: a new part is excluded if the transaction was when split, or if every
    # part is now.
    excluded = int(bool(t["excluded"] or (existing and all(r["excluded"] for r in existing.values()))))
    for row in left_out:
        release(conn, row)
    sign = -1 if t["amount"] < 0 else 1
    regular = t["type"] == "regular"
    tag_ids = [r[0] for r in conn.execute("SELECT tag_id FROM transaction_tags WHERE transaction_id = ?", (t["id"],))]
    for i, p in enumerate(parts):
        if p.id is not None:
            row = existing[p.id]
            category = p.category_id if row["type"] == "regular" else None
            if (row["name"], round(row["amount"] * 100), row["category_id"]) != (p.name, sign * p.cents, category):
                conn.execute("UPDATE transactions SET name = ?, amount = ?, category_id = ?, user_edited = 1 WHERE id = ?",
                             (p.name, sign * p.cents / 100, category, p.id))
            continue
        # The first part of a new split carries the note; every part gets the tags.
        part_id = conn.execute(
            """INSERT INTO transactions (account_id, date, amount, name, original_name, category_id, type, excluded, notes,
                                         reviewed, user_edited, source, split_from_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 'split', ?)""",
            (t["account_id"], t["date"], sign * p.cents / 100, p.name, t["original_name"], p.category_id if regular else None,
             t["type"], excluded if regular else 0, t["notes"] if not existing and i == 0 else None, t["id"]),
        ).lastrowid
        conn.executemany("INSERT INTO transaction_tags (transaction_id, tag_id) VALUES (?, ?)", [(part_id, g) for g in tag_ids])
    conn.execute("UPDATE transactions SET user_edited = 1, reviewed = 1, excluded = 0 WHERE id = ?", (t["id"],))
    return sum(row["source"] != "split" for row in left_out)


def combine_candidates(conn: sqlite3.Connection, t) -> list[sqlite3.Row] | None:
    """Returns the transactions `t` could be combined with (see combine), nearest first, or None when `t` can't be
    combined: rows of its account with no bank record (from the Copilot history, or added by hand) that aren't in a
    split or linked to anything, of its type and on its side (money in or out), up to 3 days apart."""
    if not conn.execute(f"SELECT 1 FROM transactions t WHERE t.id = ? AND {_COMBINABLE.format(t='t')}", (t["id"],)).fetchone():
        return None
    return conn.execute(
        f"""SELECT c.*, a.name AS account_name, a.mask AS account_mask FROM transactions c JOIN accounts a ON a.id = c.account_id
            WHERE c.account_id = :account AND c.id != :id AND {_COMBINABLE.format(t='c')} AND c.type = :type
              AND (c.amount < 0) = (:amount < 0) AND abs(julianday(c.date) - julianday(:date)) <= 3
            ORDER BY abs(julianday(c.date) - julianday(:date)), c.id LIMIT 30""",
        {"account": t["account_id"], "id": t["id"], "type": t["type"], "amount": t["amount"], "date": t["date"]},
    ).fetchall()


def combine(conn: sqlite3.Connection, t, others: list, name: str) -> int:
    """Combines `t` and `others` (from combine_candidates) into one transaction named `name`, on `t`'s date, of their
    total, which shows as one line like a split, with them as its parts; returns its id. It's how a purchase split in
    Copilot, with no bank transaction to rebuild it from, comes back together. No total changes: it counts only
    through its parts."""
    rows = [t, *others]
    combined = conn.execute(
        """INSERT INTO transactions (account_id, date, amount, name, type, reviewed, user_edited, source)
           VALUES (?, ?, ?, ?, ?, 1, 1, 'group')""",
        (t["account_id"], t["date"], round(sum(r["amount"] for r in rows), 2), name, t["type"]),
    ).lastrowid
    conn.executemany("UPDATE transactions SET split_from_id = ? WHERE id = ?", [(combined, r["id"]) for r in rows])
    return combined


def unsplit(conn: sqlite3.Connection, t) -> int:
    """Removes the parts of transaction `t`, so it counts whole again (unless every part was excluded, and so it is),
    and returns how many of them were kept as
    transactions of their own (parts that existed before the split, such as rows from the Copilot history). When any
    were kept, they and `t` go back on the review list, since they may now count twice. A transaction made by combining
    others (see combine) goes away instead, and they're as they were before. Raises ValueError, saying why, if deleting
    a part would lose a link."""
    parts = parts_of(conn, t["id"])
    _check_links(conn, parts)
    if t["source"] == "group" and t["plaid_transaction_id"] is None:
        links.unlink(conn, t)  # it goes, so it leaves what it's linked with
        conn.execute("UPDATE transactions SET split_from_id = NULL WHERE split_from_id = ?", (t["id"],))
        conn.execute("DELETE FROM transactions WHERE id = ?", (t["id"],))
        return 0
    for row in parts:
        release(conn, row)
    # Whole again, it's excluded only if every part was.
    conn.execute("UPDATE transactions SET excluded = ? WHERE id = ?",
                 (int(t["type"] == "regular" and bool(parts) and all(row["excluded"] for row in parts)), t["id"]))
    kept = sum(row["source"] != "split" for row in parts)
    if kept:
        conn.execute("UPDATE transactions SET reviewed = 0 WHERE id = ?", (t["id"],))
    return kept


def release(conn: sqlite3.Connection, part) -> None:
    """Takes a part out of its split: deletes it if splitting made it, else makes it a transaction of its own again,
    back on the review list, as it may now count twice."""
    if part["source"] == "split":
        conn.execute("DELETE FROM transactions WHERE id = ?", (part["id"],))
    else:
        conn.execute("UPDATE transactions SET split_from_id = NULL, reviewed = 0 WHERE id = ?", (part["id"],))


def _check_links(conn: sqlite3.Connection, parts) -> None:
    # Deleting a part made by splitting would silently drop a payback or a transfer link, and move money between months.
    for part in parts:
        if part["source"] != "split":
            continue
        if part["reimburses_id"] or conn.execute("SELECT 1 FROM transactions WHERE reimburses_id = ?", (part["id"],)).fetchone():
            raise ValueError(f"The part “{part['name']}” is linked to a payback. Unlink that first, on the part's page.")
        if part["transfer_id"]:
            raise ValueError(f"The part “{part['name']}” is one side of a move between your accounts. Unlink that first, on the part's page.")
