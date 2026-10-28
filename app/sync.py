"""Connecting banks through Plaid and pulling their transactions and balances into the ledger."""

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher

from . import links, recurring
from .categorize import APP_TRANSFERS, Categorizer, ensure_default_categories
from .config import Settings
from .db import connect
from .plaid_client import PlaidClient, PlaidError

log = logging.getLogger(__name__)

# One sync at a time, whether from the scheduler or a button.
SYNC_LOCK = threading.Lock()

# How Plaid transactions are merged with history an account already has (e.g. from Copilot):
# within OVERLAP_DAYS of either end of that history we look for the same transaction (same
# amount, dates up to MATCH_WINDOW_DAYS apart) and add it only if it isn't there; deeper inside
# the history we trust what's already there and skip Plaid's copy.
OVERLAP_DAYS = 7
MATCH_WINDOW_DAYS = 3

# Money moved between the user's own accounts through an app such as Zelle shows up as an app transfer on both
# sides. A Plaid row out of one account and one into another, for the same amount within this many days, are
# taken to be one such move (see pair_own_transfers).
PAIR_WINDOW_DAYS = 3

# Errors fixed by logging in to the bank again (Link update mode).
LOGIN_ERRORS = {"ITEM_LOGIN_REQUIRED", "ACCESS_NOT_GRANTED", "USER_SETUP_REQUIRED", "ITEM_LOCKED", "INVALID_CREDENTIALS"}
ACCOUNT_TYPES = {"depository", "credit", "loan", "investment"}


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    matched: int = 0  # Plaid transactions found in existing history and linked to it
    skipped: int = 0  # Plaid transactions already covered by existing history
    needs_match: bool = False  # the bank sent new accounts, and the user has to match them first

    def summary(self) -> str:
        if self.needs_match:
            return "new accounts need matching"
        return f"{self.added} new, {self.updated} updated, {self.removed} removed"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_utc(value: str | None) -> datetime | None:
    """Returns a stored timestamp as an aware UTC datetime, or None if it's empty."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def make_client(settings: Settings) -> PlaidClient | None:
    """Returns a Plaid client for the configured environment, or None when the keys are missing."""
    if not settings.plaid_configured:
        return None
    return PlaidClient(settings.plaid_client_id, settings.plaid_secret, settings.plaid_env)


def _account_fields(a: dict) -> dict:
    balances = a.get("balances") or {}
    return {
        "name": a.get("name") or a.get("official_name") or "Account",
        "type": a.get("type") if a.get("type") in ACCOUNT_TYPES else "other",
        "subtype": a.get("subtype"),
        "mask": a.get("mask"),
        "currency": balances.get("iso_currency_code") or balances.get("unofficial_currency_code") or "USD",
        "balance": balances.get("current"),
        "available": balances.get("available"),
        "credit_limit": balances.get("limit"),
        "updated": utc_now(),
    }


def _create_account(conn: sqlite3.Connection, a: dict, item_id: str, institution: str | None) -> int:
    return conn.execute(
        """INSERT INTO accounts (name, institution, type, subtype, mask, currency, balance, available,
                                 credit_limit, balance_updated_at, source, plaid_account_id, plaid_item_id)
           VALUES (:name, :institution, :type, :subtype, :mask, :currency, :balance, :available,
                   :credit_limit, :updated, 'plaid', :pid, :item)""",
        {**_account_fields(a), "institution": institution, "pid": a["account_id"], "item": item_id},
    ).lastrowid


def _item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()


def _account_keys(accounts: list[dict]) -> set[tuple[str, str | None]]:
    return {(a["mask"], a.get("subtype")) for a in accounts if a.get("mask")}


def find_duplicate(conn: sqlite3.Connection, env: str, institution: dict | None, accounts: list[dict] | None) -> sqlite3.Row | None:
    """Returns the existing connection that a new bank login from Link would duplicate, or None.

    That's a connection to the same bank in the same Plaid environment that shares an account with the new
    login (same last 4 digits and kind), or whose accounts can't be compared with it."""
    institution_id = (institution or {}).get("institution_id")
    if not institution_id:
        return None
    new = _account_keys(accounts or [])
    for item in conn.execute("SELECT * FROM plaid_items WHERE env = ? AND institution_id = ?", (env, institution_id)).fetchall():
        linked = [dict(r) for r in conn.execute("SELECT mask, subtype FROM accounts WHERE plaid_item_id = ?", (item["item_id"],))]
        known = _account_keys(linked + json.loads(item["pending_accounts"] or "[]"))
        if not new or not known or new & known:
            return item
    return None


def _exchange(client: PlaidClient, public_token: str) -> dict:
    # A public token works only once, so trying again helps only when the first request never reached Plaid.
    try:
        return client.exchange_public_token(public_token)
    except PlaidError as exc:
        if exc.code != "NETWORK_ERROR":
            raise
        return client.exchange_public_token(public_token)


def link_item(conn: sqlite3.Connection, client: PlaidClient, public_token: str, institution: dict | None) -> sqlite3.Row:
    """Saves a bank the user just connected in Link and returns its plaid_items row.

    The connection is saved as soon as Plaid hands over its access token, since each new one uses up a Trial
    plan slot. If Plaid can't list its accounts yet, it's left with an error and the next sync tries again. If
    there are accounts not yet connected to Plaid (such as ones imported from Copilot), it waits in 'needs_match'
    until the user says which of them its accounts are."""
    tokens = _exchange(client, public_token)
    institution = institution or {}
    with conn:
        conn.execute(
            "INSERT INTO plaid_items (item_id, access_token, env, institution_id, institution_name) VALUES (?, ?, ?, ?, ?)",
            (tokens["item_id"], tokens["access_token"], client.env, institution.get("institution_id"), institution.get("name")),
        )
        ensure_default_categories(conn)
    try:
        _load_accounts(conn, client, _item(conn, tokens["item_id"]))
    except PlaidError as exc:
        log.warning("Couldn't load the accounts of the new connection to %s: %s", institution.get("name") or "a bank", exc)
        status = "login_required" if exc.code in LOGIN_ERRORS else "error"
        with conn:
            conn.execute("UPDATE plaid_items SET status = ?, error = ? WHERE item_id = ?", (status, exc.friendly, tokens["item_id"]))
    return _item(conn, tokens["item_id"])


def suggest_matches(conn: sqlite3.Connection, item: sqlite3.Row) -> list[dict]:
    """Returns each Plaid account waiting for a match, paired with the existing account it most likely is (or None)."""
    pending = json.loads(item["pending_accounts"] or "[]")
    candidates = conn.execute("SELECT id, name, mask FROM accounts WHERE plaid_account_id IS NULL").fetchall()
    taken: set[int] = set()
    out = []
    for a in pending:
        free = [c for c in candidates if c["id"] not in taken]
        best = next((c["id"] for c in free if a.get("mask") and c["mask"] == a.get("mask")), None)
        if best is None and free:
            name = (a.get("name") or "").lower()
            score, cid = max((SequenceMatcher(None, name, c["name"].lower()).ratio(), c["id"]) for c in free)
            best = cid if score >= 0.6 else None
        if best is not None:
            taken.add(best)
        out.append({"plaid": a, "suggested": best})
    return out


def apply_matches(conn: sqlite3.Connection, item_id: str, choices: dict[str, int | None]) -> None:
    """Connects each waiting Plaid account to the chosen existing account, or to a new account when the
    choice is None, and marks the Item ready to sync. Raises ValueError if an account is chosen twice."""
    chosen = [c for c in choices.values() if c is not None]
    if len(chosen) != len(set(chosen)):
        raise ValueError("Each existing account can be matched to only one bank account.")
    item = conn.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()
    with conn:
        for a in json.loads(item["pending_accounts"] or "[]"):
            target = choices.get(a["account_id"])
            if target is None:
                _create_account(conn, a, item_id, item["institution_name"])
                continue
            if not conn.execute("SELECT 1 FROM accounts WHERE id = ? AND plaid_account_id IS NULL", (target,)).fetchone():
                raise ValueError("That account is already connected to a bank.")
            span = conn.execute("SELECT min(date), max(date) FROM transactions WHERE account_id = ?", (target,)).fetchone()
            conn.execute(
                """UPDATE accounts SET institution = coalesce(institution, :institution), type = :type,
                          subtype = :subtype, mask = coalesce(:mask, mask), currency = :currency,
                          balance = :balance, available = :available, credit_limit = :credit_limit,
                          balance_updated_at = :updated, plaid_account_id = :pid, plaid_item_id = :item,
                          history_start = :start, history_cutoff = :cutoff
                   WHERE id = :id""",
                {**_account_fields(a), "institution": item["institution_name"], "pid": a["account_id"],
                 "item": item_id, "start": span[0], "cutoff": span[1], "id": target},
            )
        conn.execute("UPDATE plaid_items SET status = 'ok', pending_accounts = NULL WHERE item_id = ?", (item_id,))


def _fetch_all_pages(client: PlaidClient, access_token: str, cursor: str | None) -> dict:
    updates: dict = {"added": [], "modified": [], "removed": []}
    while True:
        page = client.transactions_sync(access_token, cursor)
        for key in ("added", "modified", "removed"):
            updates[key].extend(page.get(key, []))
        cursor = page["next_cursor"]
        if not page.get("has_more"):
            return {**updates, "next_cursor": cursor, "status": page.get("transactions_update_status")}


def fetch_updates(client: PlaidClient, access_token: str, cursor: str | None) -> dict:
    """Returns every transaction change after `cursor`: {"added", "modified", "removed", "next_cursor", "status"}."""
    attempts = 0
    while True:
        try:
            return _fetch_all_pages(client, access_token, cursor)
        except PlaidError as exc:
            # Plaid requires restarting from the first cursor when data changes mid-pagination.
            attempts += 1
            if exc.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempts >= 3:
                raise


def purchase_time(value: str | None) -> datetime | None:
    """Returns the bank's purchase time `value` in the app's time zone (the TZ setting); None when it's missing,
    unreadable or a bank's default midnight, which Plaid's docs warn about."""
    if not value or "T00:00:00" in value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _txn_date(t: dict) -> date:
    """Returns the day transaction `t` was made:
    - with a purchase time from the bank: that time's day in the app's time zone
    - else with a purchase date: that date, as Plaid suggests for display
    - else: its date, the posting day or the day a pending one was made"""
    made_at = purchase_time(t.get("authorized_datetime"))  # NOTE: some banks' purchase date is the UTC day
    return made_at.date() if made_at else date.fromisoformat(t.get("authorized_date") or t["date"])


def _display_name(t: dict) -> str:
    return t.get("merchant_name") or t.get("name") or "Unknown"


def _plaid_category(t: dict) -> str | None:
    return (t.get("personal_finance_category") or {}).get("detailed")


def _plaid_confidence(t: dict) -> str | None:
    return (t.get("personal_finance_category") or {}).get("confidence_level")


def _details(t: dict) -> str | None:
    """Returns what else Plaid says about transaction `t` that its page shows, as JSON: the days it was made and posted
    (with the time, when the bank gives one), the bank's and the merchant's names, the website, how it was paid, the
    bank's own type for it, where, who was on the other side, the payment processor, and how sure Plaid is of its
    category. Parts Plaid leaves empty are left out; None when nothing's left."""
    location = t.get("location") or {}
    region = " ".join(x for x in (location.get("region"), location.get("postal_code")) if x)
    details = {
        "posted": t.get("date"), "made": t.get("authorized_date"), "time": t.get("authorized_datetime") or t.get("datetime"),
        "bank_name": t.get("name"), "merchant": t.get("merchant_name"), "website": t.get("website"),
        "channel": t.get("payment_channel"), "code": t.get("transaction_code"), "store": location.get("store_number"),
        "where": ", ".join(x for x in (location.get("address"), location.get("city"), region) if x),
        "counterparties": [{"name": c["name"], "type": c.get("type")} for c in t.get("counterparties") or [] if c.get("name")],
        "processor": (t.get("payment_meta") or {}).get("payment_processor"), "confidence": _plaid_confidence(t),
    }
    details = {k: v for k, v in details.items() if v}
    return json.dumps(details, sort_keys=True) if details else None


def _overlap_action(day: date, start: str | None, cutoff: str | None) -> str:
    """Returns 'insert', 'match' or 'skip' for a Plaid transaction on `day`, given the account's earlier history."""
    if not start or not cutoff:
        return "insert"
    first, last = date.fromisoformat(start), date.fromisoformat(cutoff)
    window, overlap = timedelta(days=MATCH_WINDOW_DAYS), timedelta(days=OVERLAP_DAYS)
    if day < first - window or day > last + window:
        return "insert"
    if day <= first + overlap or day >= last - overlap:
        return "match"
    return "skip"


def _link_to_history(conn: sqlite3.Connection, account_id: int, t: dict, day: date) -> bool:
    amount = -float(t["amount"])
    window = timedelta(days=MATCH_WINDOW_DAYS)
    # A part of a split isn't a bank transaction on its own: the original it was split from is.
    row = conn.execute(
        """SELECT id FROM transactions
           WHERE account_id = ? AND plaid_transaction_id IS NULL AND split_from_id IS NULL AND abs(amount - ?) < 0.005
             AND date BETWEEN ? AND ?
           ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1""",
        (account_id, amount, (day - window).isoformat(), (day + window).isoformat(), day.isoformat()),
    ).fetchone()
    if not row:
        return False
    conn.execute(
        """UPDATE transactions SET plaid_transaction_id = ?, plaid_category = ?, logo_url = ?, plaid_details = ?,
                  original_name = coalesce(original_name, ?)
           WHERE id = ?""",
        (t["transaction_id"], _plaid_category(t), t.get("logo_url"), _details(t), t.get("original_description") or t.get("name"), row["id"]),
    )
    return True


def _update_existing(conn: sqlite3.Connection, t: dict, lookup_id: str, categorizer: Categorizer) -> bool:
    """Updates the row Plaid knows as `lookup_id` from transaction `t`, the same transaction again or its pending
    version posting; returns False if there's no such row."""
    row = conn.execute(
        """SELECT t.id, t.user_edited, t.source, t.amount, a.type AS account_type,
                  EXISTS (SELECT 1 FROM transactions s WHERE s.split_from_id = t.id) AS split
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.plaid_transaction_id = ?""",
        (lookup_id,),
    ).fetchone()
    if not row:
        return False
    fields = {
        "id": row["id"], "pid": t["transaction_id"], "amount": -float(t["amount"]),
        "date": _txn_date(t).isoformat(), "pending": int(bool(t.get("pending"))),
        "original": t.get("original_description") or t.get("name"), "pfc": _plaid_category(t),
        "logo": t.get("logo_url"), "name": _display_name(t), "details": _details(t),
    }
    # A date the user set by hand stays, even when the bank moves it (a pending charge can post on another day).
    # Otherwise a sync never moves the date later: it's the day the money was spent, so a charge keeps its pending day
    # when it posts and through every later update or re-sync. An earlier day still wins: some banks date a transfer's
    # posting a day before it showed as pending.
    if row["source"] == "copilot":
        # Imported history keeps its own date, amount and name; only Plaid's details are added.
        conn.execute("UPDATE transactions SET plaid_transaction_id = :pid, plaid_category = :pfc, logo_url = :logo, plaid_details = :details "
                     "WHERE id = :id", fields)
    elif row["user_edited"]:
        conn.execute(
            """UPDATE transactions SET plaid_transaction_id = :pid, amount = :amount, pending = :pending,
                      date = CASE WHEN date_edited THEN date ELSE min(date, :date) END,
                      original_name = :original, plaid_category = :pfc, logo_url = :logo, plaid_details = :details
               WHERE id = :id""",
            fields,
        )
    else:
        fields["category"], fields["type"] = categorizer.categorize(
            fields["name"], fields["original"], fields["pfc"], _plaid_confidence(t), row["account_type"])
        conn.execute(
            """UPDATE transactions SET plaid_transaction_id = :pid, amount = :amount, pending = :pending,
                      date = CASE WHEN date_edited THEN date ELSE min(date, :date) END,
                      name = :name, original_name = :original, plaid_category = :pfc, logo_url = :logo, plaid_details = :details,
                      category_id = :category, type = :type
               WHERE id = :id""",
            fields,
        )
    if row["split"] and row["source"] != "copilot" and abs(row["amount"] - fields["amount"]) >= 0.005:
        # Its parts no longer add up to it (a tip added when it posted, say): back to the review list to fix them.
        conn.execute("UPDATE transactions SET reviewed = 0 WHERE id = ?", (row["id"],))
    return True


def _insert(conn: sqlite3.Connection, account: sqlite3.Row, t: dict, day: date,
            categorizer: Categorizer, matcher: recurring.Matcher, reviewed: bool) -> None:
    name, original, pfc = _display_name(t), t.get("original_description") or t.get("name"), _plaid_category(t)
    amount = -float(t["amount"])  # Plaid counts money leaving the account as positive
    category, type_ = categorizer.categorize(name, original, pfc, _plaid_confidence(t), account["type"])
    conn.execute(
        """INSERT INTO transactions (account_id, date, amount, name, original_name, category_id, type, pending,
                                     reviewed, source, plaid_transaction_id, plaid_category, logo_url, plaid_details, recurring_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'plaid', ?, ?, ?, ?, ?)""",
        (account["id"], day.isoformat(), amount, name, original, category, type_, int(bool(t.get("pending"))),
         int(reviewed), t["transaction_id"], pfc, t.get("logo_url"), _details(t), matcher.match(name, amount) if type_ != "transfer" else None),
    )


def _apply_transactions(conn: sqlite3.Connection, item_id: str, updates: dict, first_load: bool) -> SyncResult:
    result = SyncResult()
    # During the first load, only recent transactions go to the review list, except on accounts with imported
    # history: anything added there wasn't in the import (say, a transfer deleted in Copilot), so it gets a look.
    review_after = date.today() - timedelta(days=7) if first_load else date.min
    accounts = {
        r["plaid_account_id"]: r
        for r in conn.execute("SELECT id, type, plaid_account_id, history_start, history_cutoff FROM accounts WHERE plaid_item_id = ?", (item_id,))
    }
    categorizer, matcher = Categorizer(conn), recurring.Matcher(conn)
    # Added before removed: a posted transaction arrives together with the removal of its pending
    # version, and carrying the pending row forward keeps the user's edits and notes.
    for t in updates["added"]:
        account = accounts.get(t["account_id"])
        if account is None:
            continue
        if _update_existing(conn, t, t["transaction_id"], categorizer) or (
            t.get("pending_transaction_id") and _update_existing(conn, t, t["pending_transaction_id"], categorizer)
        ):
            result.updated += 1
            continue
        day = _txn_date(t)
        action = _overlap_action(day, account["history_start"], account["history_cutoff"])
        if action == "skip":
            result.skipped += 1
        elif action == "match" and _link_to_history(conn, account["id"], t, day):
            result.matched += 1
        else:
            _insert(conn, account, t, day, categorizer, matcher, reviewed=day < review_after and account["history_start"] is None)
            result.added += 1
    for t in updates["modified"]:
        if _update_existing(conn, t, t["transaction_id"], categorizer):
            result.updated += 1
    for r in updates["removed"]:
        row = conn.execute("SELECT id, source FROM transactions WHERE plaid_transaction_id = ?", (r["transaction_id"],)).fetchone()
        if not row:
            continue
        if row["source"] == "copilot":
            conn.execute("UPDATE transactions SET plaid_transaction_id = NULL WHERE id = ?", (row["id"],))
        else:
            # Parts made by splitting it go with it; parts that were transactions before (from the Copilot history) stay.
            conn.execute("DELETE FROM transactions WHERE split_from_id = ? AND source = 'split'", (row["id"],))
            conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
            result.removed += 1
    return result


def pair_own_transfers(conn: sqlite3.Connection) -> int:
    """Marks money moved between the user's own accounts as transfers, and returns how many rows changed.

    A pair is two Plaid rows on different accounts, one out and one in, for the same amount within
    PAIR_WINDOW_DAYS, both filed by Plaid as app transfers or left uncategorized (OTHER), at least one of them an
    app transfer. A side already marked as a transfer (by a rule or by hand) counts, so marking one side makes
    the other follow. Rows edited by hand are never changed, and a hand-edited row that isn't a transfer doesn't
    pair at all. Rows already linked to their other side are left out, and so are transactions split into parts (their
    parts can pair) and rows the user linked with others (links.py). Every pair found is linked (link_transfers)."""
    apps = ", ".join(f"'{code}'" for code in sorted(APP_TRANSFERS))
    eligible = lambda t: (f"{t}.source = 'plaid' AND ({t}.plaid_category IN ({apps}) OR {t}.plaid_category LIKE 'OTHER%') "
                          f"AND ({t}.type = 'transfer' OR ({t}.type = 'regular' AND {t}.user_edited = 0)) "
                          f"AND NOT EXISTS (SELECT 1 FROM transactions s WHERE s.split_from_id = {t}.id) AND NOT {links.LINKED.format(t=t)}")
    pairs = conn.execute(
        f"""SELECT o.id AS out_id, i.id AS in_id,
                   o.type = 'regular' AS out_changes, i.type = 'regular' AS in_changes
            FROM transactions o JOIN transactions i
              ON i.account_id != o.account_id AND abs(i.amount + o.amount) < 0.005
             AND abs(julianday(i.date) - julianday(o.date)) <= ?
            WHERE o.amount < 0 AND {eligible('o')} AND {eligible('i')} AND o.transfer_id IS NULL AND i.transfer_id IS NULL
              AND (o.plaid_category IN ({apps}) OR i.plaid_category IN ({apps}))
            ORDER BY abs(julianday(i.date) - julianday(o.date)), o.date, o.id, i.id""",
        (PAIR_WINDOW_DAYS,),
    ).fetchall()
    used: set[int] = set()
    changed: list[int] = []
    # Closest dates first, and each row joins one pair at most, so a pair found earlier keeps its rows.
    for p in pairs:
        if p["out_id"] in used or p["in_id"] in used:
            continue
        used.update((p["out_id"], p["in_id"]))
        changed += [row for row, changes in ((p["out_id"], p["out_changes"]), (p["in_id"], p["in_changes"])) if changes]
    conn.executemany("UPDATE transactions SET type = 'transfer', category_id = NULL, excluded = 0, recurring_id = NULL WHERE id = ?",
                     [(i,) for i in changed])
    link_transfers(conn)
    return len(changed)


def link_transfers(conn: sqlite3.Connection) -> int:
    """Links the two sides of moves between the user's accounts, and returns how many pairs it linked.

    A pair is two transactions marked as transfers but not linked yet, on different accounts, one out and one in,
    for the same amount within PAIR_WINDOW_DAYS; the closest dates pair first, and each row pairs once. A transaction
    split into parts never pairs (its parts can), nor one the user linked with others (links.py). Both sides are then
    marked reviewed: with the other side found, the money stayed with the user."""
    pairs = conn.execute(
        f"""SELECT o.id AS out_id, i.id AS in_id
           FROM transactions o JOIN transactions i
             ON i.account_id != o.account_id AND abs(i.amount + o.amount) < 0.005
            AND abs(julianday(i.date) - julianday(o.date)) <= ?
           WHERE o.amount < 0 AND o.type = 'transfer' AND i.type = 'transfer' AND o.transfer_id IS NULL AND i.transfer_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM transactions s WHERE s.split_from_id IN (o.id, i.id))
             AND NOT {links.LINKED.format(t='o')} AND NOT {links.LINKED.format(t='i')}
           ORDER BY abs(julianday(i.date) - julianday(o.date)), o.date, o.id, i.id""",
        (PAIR_WINDOW_DAYS,),
    ).fetchall()
    used: set[int] = set()
    linked = 0
    for p in pairs:
        if p["out_id"] in used or p["in_id"] in used:
            continue
        used.update((p["out_id"], p["in_id"]))
        conn.execute("UPDATE transactions SET transfer_id = ?, reviewed = 1 WHERE id = ?", (p["in_id"], p["out_id"]))
        conn.execute("UPDATE transactions SET transfer_id = ?, reviewed = 1 WHERE id = ?", (p["out_id"], p["in_id"]))
        linked += 1
    return linked


def _update_accounts(conn: sqlite3.Connection, item: sqlite3.Row, data: dict) -> bool:
    """Saves the balances of the Item's accounts and adds accounts it didn't have before.

    Returns False, adding nothing, when there are new accounts while other accounts here aren't connected to a
    bank (one of those may be the same account, e.g. from the Copilot import). The Item then waits in
    'needs_match', and once the user has matched them, its transactions are fetched again from the start."""
    accounts = data.get("accounts", [])
    known = {
        r["plaid_account_id"]: r["id"]
        for r in conn.execute("SELECT id, plaid_account_id FROM accounts WHERE plaid_item_id = ?", (item["item_id"],))
    }
    new = [a for a in accounts if a["account_id"] not in known]
    if new and conn.execute("SELECT 1 FROM accounts WHERE plaid_account_id IS NULL LIMIT 1").fetchone():
        conn.execute(
            "UPDATE plaid_items SET status = 'needs_match', pending_accounts = ?, cursor = NULL, update_status = NULL WHERE item_id = ?",
            (json.dumps(new), item["item_id"]),
        )
        return False
    today = date.today().isoformat()
    for a in accounts:
        f = _account_fields(a)
        account_id = known.get(a["account_id"])
        if account_id is not None:
            conn.execute(
                """UPDATE accounts SET type = :type, subtype = :subtype, mask = coalesce(:mask, mask), currency = :currency,
                          balance = :balance, available = :available, credit_limit = :credit_limit, balance_updated_at = :updated
                   WHERE id = :id""",
                {**f, "id": account_id},
            )
        else:
            account_id = _create_account(conn, a, item["item_id"], item["institution_name"])
        if f["balance"] is not None:
            conn.execute(
                "INSERT INTO balance_history (account_id, date, balance) VALUES (?, ?, ?) "
                "ON CONFLICT (account_id, date) DO UPDATE SET balance = excluded.balance",
                (account_id, today, f["balance"]),
            )
    return True


def _load_accounts(conn: sqlite3.Connection, client: PlaidClient, item: sqlite3.Row) -> bool:
    """Fetches the Item's accounts from Plaid and saves them (see _update_accounts). Returns False when the user
    has to match new accounts first; raises PlaidError on failure."""
    data = client.get_accounts(item["access_token"])
    details = data.get("item") or {}
    with conn:
        conn.execute(
            "UPDATE plaid_items SET institution_id = coalesce(institution_id, ?), consent_expires_at = coalesce(?, consent_expires_at) WHERE item_id = ?",
            (details.get("institution_id"), details.get("consent_expiration_time"), item["item_id"]),
        )
        return _update_accounts(conn, item, data)


def sync_item(conn: sqlite3.Connection, client: PlaidClient, item_id: str) -> SyncResult:
    """Brings one connected bank's balances and transactions up to date. Returns the changes; raises PlaidError on failure."""
    item = _item(conn, item_id)
    if not _load_accounts(conn, client, item):
        return SyncResult(needs_match=True)
    updates = fetch_updates(client, item["access_token"], item["cursor"])
    with conn:
        result = _apply_transactions(conn, item_id, updates, first_load=item["update_status"] != "HISTORICAL_UPDATE_COMPLETE")
        pair_own_transfers(conn)
        conn.execute(
            "UPDATE plaid_items SET cursor = ?, update_status = ?, last_synced_at = ?, status = 'ok', error = NULL WHERE item_id = ?",
            (updates["next_cursor"], updates["status"], utc_now(), item_id),
        )
    return result


def run_sync(settings: Settings, item_ids: list[str] | None = None) -> list[tuple[str, str]]:
    """Syncs the given connected banks (default: every one that's ready) and records any errors on them.
    Returns (bank name, short result message) for each bank synced."""
    client = make_client(settings)
    if client is None:
        return []
    with SYNC_LOCK:
        conn = connect(settings.db_path)
        try:
            items = conn.execute(
                "SELECT item_id, institution_name FROM plaid_items WHERE env = ? AND status != 'needs_match'",
                (settings.plaid_env,),
            ).fetchall()
            results = []
            for item in items:
                if item_ids is not None and item["item_id"] not in item_ids:
                    continue
                label = item["institution_name"] or "Bank"
                try:
                    results.append((label, sync_item(conn, client, item["item_id"]).summary()))
                except PlaidError as exc:
                    status = "login_required" if exc.code in LOGIN_ERRORS else "error"
                    with conn:
                        conn.execute(
                            "UPDATE plaid_items SET status = ?, error = ?, last_synced_at = ? WHERE item_id = ?",
                            (status, exc.friendly, utc_now(), item["item_id"]),
                        )
                    log.warning("Sync failed for %s: %s", label, exc)
                    results.append((label, exc.friendly))
            return results
        finally:
            conn.close()


def due_items(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> list[str]:
    """Returns the ids of connected banks the background scheduler should sync now."""
    now = now or datetime.now(UTC)
    interval = timedelta(hours=settings.sync_interval_hours)
    due = []
    for r in conn.execute(
        "SELECT item_id, status, update_status, last_synced_at, created_at FROM plaid_items WHERE env = ? AND status IN ('ok', 'error')",
        (settings.plaid_env,),
    ):
        last, created = parse_utc(r["last_synced_at"]), parse_utc(r["created_at"])
        # Right after a bank is connected, Plaid keeps gathering history for a while; check back often,
        # though less often while the connection is failing.
        gathering = r["update_status"] != "HISTORICAL_UPDATE_COMPLETE" and created and now - created < timedelta(hours=3)
        retry = timedelta(minutes=2 if r["status"] == "ok" else 15)
        if last is None or now - last >= interval or (gathering and now - last >= retry):
            due.append(r["item_id"])
    return due


def disconnect_item(conn: sqlite3.Connection, client: PlaidClient | None, item_id: str, delete_data: bool) -> None:
    """Disconnects a bank at Plaid and here. Its history stays unless `delete_data`, which deletes what came
    from this connection; history imported from Copilot is always kept."""
    item = conn.execute("SELECT * FROM plaid_items WHERE item_id = ?", (item_id,)).fetchone()
    if item is None:
        return
    if client is not None and client.env == item["env"]:
        try:
            client.remove_item(item["access_token"])
        except PlaidError as exc:
            log.warning("Plaid couldn't remove item %s: %s", item_id, exc)
    with conn:
        accounts = "SELECT id FROM accounts WHERE plaid_item_id = :item"
        if delete_data:
            bank_rows = f"SELECT id FROM transactions WHERE source = 'plaid' AND account_id IN ({accounts})"
            conn.execute(f"DELETE FROM transactions WHERE source = 'split' AND split_from_id IN ({bank_rows})", {"item": item_id})
            conn.execute(f"DELETE FROM transactions WHERE source = 'plaid' AND account_id IN ({accounts})", {"item": item_id})
        conn.execute(f"UPDATE transactions SET plaid_transaction_id = NULL WHERE account_id IN ({accounts})", {"item": item_id})
        if delete_data:
            conn.execute(
                "DELETE FROM accounts WHERE plaid_item_id = :item AND id NOT IN (SELECT DISTINCT account_id FROM transactions)",
                {"item": item_id},
            )
        conn.execute("UPDATE accounts SET plaid_account_id = NULL, plaid_item_id = NULL WHERE plaid_item_id = ?", (item_id,))
        conn.execute("DELETE FROM plaid_items WHERE item_id = ?", (item_id,))
