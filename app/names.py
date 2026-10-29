"""Cleaner names for transactions: the codes and account numbers banks add, taken off the names they send, and the
user's own names for them, kept for the ones that come later."""

import json
import re
import sqlite3
from collections import Counter, defaultdict

from .categorize import Categorizer, add_rename

# A code that identifies one payment, after a word saying what it is: "Conf# 1A2B3C4D", "Confirmation# XYZ12345",
# "Ref #IB0ABC12", "Trace no. 12345", "Transaction ID: 98765", "PPD ID: 1234567890".
_CODE = re.compile(r"\b(?:conf(?:irmation)?|ref(?:erence)?|trace|trans(?:action)?|auth(?:orization)?|seq(?:uence)?)\s*"
                   r"(?:#|:|no\b\.?|num(?:ber)?\b|id\b|code\b)\s*[:#]?\s*[a-z0-9][\w-]*"
                   r"|\b(?:ppd|web|ccd|tel|arc)\s+id\s*:?\s*\w+|\bid\s*:\s*\w*\d\w*", re.IGNORECASE)
# An account's last digits: masked ("XXXXXX1234", "****1234", "...1234", "x1234", "*1234"), or after a word for the
# account ("SAV 1234", "CHK 1234", "CRD 1234", "ending in 1234"), which stays.
_MASK = re.compile(r"(?<![\w*.])(?:[x*•]{2,}|\.{2,}|x)\d{2,6}\b|(?<![\w*])\*\d{4}\b", re.IGNORECASE)
_ACCOUNT = re.compile(r"\b(sav(?:ings)?|chk|checking|crd|card|acct|account|dda|ending(?:\s+in)?)\s*(?:#|no\.?)?\s*\d{4}\b", re.IGNORECASE)
# A reference number on its own, and a code at the very end that mixes letters and digits ("JPM99ABC123").
_NUMBER = re.compile(r"(?<![\w-])\d{8,}(?![\w-])")
_LAST_CODE = re.compile(r"\s(?=\S*\d\S*\d)(?=\S*[a-z]\S*[a-z])[a-z0-9]{8,}$", re.IGNORECASE)
# What the sender of a Zelle payment wrote with it: "Zelle payment to Jamie Doe for pizza night".
_ZELLE_NOTE = re.compile(r"^(zelle\b.*?\s(?:to|from)\s+\S.*?)\s+for\s+.+$", re.IGNORECASE)
# Words and marks left dangling at the end once what followed them is gone.
_LOOSE_END = re.compile(r"(?:[\s\-–—:;,#/*|.]+|\s+(?:to|from|for|on|at|in|ending))+$", re.IGNORECASE)


def clean(name: str | None) -> str:
    """Returns the bank's name for a transaction without the codes, account numbers and Zelle notes in it:
    - nothing to take off: the name as it is
    - nothing but numbers left: the name as it is
    - else: what's left, its loose ends tidied, cleaned once more"""
    text = " ".join((name or "").split())
    cleaned = _ACCOUNT.sub(r"\1", _MASK.sub(" ", _CODE.sub(" ", text)))
    cleaned = _LAST_CODE.sub("", " ".join(_NUMBER.sub(" ", cleaned).split()))
    cleaned = _ZELLE_NOTE.sub(r"\1", cleaned)
    if cleaned == text:
        return text
    # NOTE: only once something came off, so a name that ends in "to" or "on" by itself keeps it
    cleaned = _LOOSE_END.sub("", cleaned).lstrip(" -–—:;,#/*|.")
    if not re.search(r"[^\W\d_]", cleaned):
        return text
    return clean(cleaned)  # NOTE: some codes only end the name once what followed them is gone ("SAV 1234 5678")


def key(name: str | None) -> str:
    """Returns `name` the way rules compare names: without the spaces around it, in lowercase."""
    return (name or "").strip().lower()


def _named(conn: sqlite3.Connection, name: str, but: int | None = None) -> list[sqlite3.Row]:
    """Returns the transactions (all but `but`) named `name`, any letter case, once both names lose their codes: a
    name from before the cleanup and the one a new transaction gets count as the same. Each has its id and name."""
    wanted = key(clean(name))
    return [r for r in conn.execute("SELECT id, name FROM transactions WHERE id IS NOT ?", (but,)) if key(clean(r["name"])) == wanted]


def rename_offer(conn: sqlite3.Connection, t: sqlite3.Row, old: str) -> dict | None:
    """Returns the offer, after transaction `t` was renamed from `old`, to rename the others with that name and the
    ones still to come: its id, the old name without its codes, the new name, and how many others have the old one;
    None when nothing would change."""
    if t["name"] in (old, clean(old)):
        return None
    old = clean(old)
    others = len(_named(conn, old, but=t["id"]))
    if not others and Categorizer(conn).rename(old) == t["name"]:
        return None
    return {"id": t["id"], "old": old, "new": t["name"], "others": others}


def rename_all(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Renames every transaction named `old` (as `_named` compares names) to `new`, and makes new ones come named so
    too (categorize.add_rename). Returns how many it renamed."""
    add_rename(conn, clean(old), new)
    ids = [r["id"] for r in _named(conn, old) if r["name"] != new]
    conn.executemany("UPDATE transactions SET name = ? WHERE id = ?", [(new, i) for i in ids])
    return len(ids)


def _shown(details: str | None) -> tuple[str, dict]:
    """Returns the name a sync gives a transaction with these details from the bank (before any rule renames it), and
    the details; the name is empty when the bank gave none."""
    bank = json.loads(details or "{}")
    return bank.get("merchant") or (clean(bank.get("bank_name")) if bank.get("bank_name") else ""), bank


def learned(conn: sqlite3.Connection) -> list[dict]:
    """Returns rules that would give new transactions the names their earlier ones with the same text from the bank
    have (in the Copilot history, or given by hand), most used first: {"pattern", "shown", "name", "count"} for each,
    `shown` being the name they'd get now. Only a name most of them share, which no rule gives yet, is offered."""
    categorizer = Categorizer(conn)
    given, shown = defaultdict(Counter), defaultdict(Counter)
    for r in conn.execute("SELECT name, plaid_details FROM transactions "
                          "WHERE plaid_details IS NOT NULL AND (source = 'copilot' OR name_edited = 1)"):
        now = _shown(r["plaid_details"])[0]
        if now:
            given[key(now)][r["name"].strip()] += 1
            shown[key(now)][now] += 1
    offers = []
    for pattern, counts in given.items():
        (name, count), now = counts.most_common(1)[0], shown[pattern].most_common(1)[0][0]
        if count >= 2 and count >= 0.8 * counts.total() and name != now and not categorizer.rename(now):
            offers.append({"pattern": pattern, "shown": now, "name": name, "count": count})
    return sorted(offers, key=lambda o: (-o["count"], o["pattern"]))


def cleanup(conn: sqlite3.Connection) -> list[dict]:
    """Returns the transactions from the bank whose names would come out differently now, their codes and account
    numbers taken off and the rules' names given: {"id", "date", "name", "new"} for each, the newest first. Names
    changed by hand are kept, so they aren't among them."""
    categorizer, changes = Categorizer(conn), []
    for r in conn.execute("""SELECT id, date, name, original_name, plaid_details FROM transactions
                             WHERE source = 'plaid' AND name_edited = 0 AND plaid_details IS NOT NULL ORDER BY date DESC, id DESC"""):
        now, bank = _shown(r["plaid_details"])
        new = (categorizer.rename(now, bank.get("bank_name"), r["original_name"]) or now) if now else ""
        if new and new != r["name"]:
            changes.append({"id": r["id"], "date": r["date"], "name": r["name"], "new": new})
    return changes


def clean_up(conn: sqlite3.Connection) -> int:
    """Gives the transactions `cleanup` lists their new names, and returns how many it renamed."""
    changes = cleanup(conn)
    conn.executemany("UPDATE transactions SET name = ? WHERE id = ?", [(c["new"], c["id"]) for c in changes])
    return len(changes)
