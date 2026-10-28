"""Numbers for the dashboard, budgets and accounts pages."""

import calendar
import sqlite3
from datetime import date

LIABILITY_TYPES = {"credit", "loan"}
ACCOUNT_GROUPS = [
    ("depository", "Cash"), ("credit", "Credit cards"), ("investment", "Investments"),
    ("loan", "Loans"), ("other", "Other"),
]

# Money that pays back a purchase (a friend's share, linked through reimburses_id) counts with that purchase: on its
# date and in its category, so the purchase costs only the user's share in its own month.
DAY = "coalesce(p.date, t.date)"
CATEGORY = "CASE WHEN p.id IS NULL THEN t.category_id ELSE p.category_id END"
# A transaction split into parts (splits.py) counts only through its parts, which point to it with split_from_id.
NOT_SPLIT = "NOT EXISTS (SELECT 1 FROM transactions s WHERE s.split_from_id = t.id)"
# A split transaction whose parts don't add up to it (the bank changed its amount, say): it waits for review until they do.
SPLIT_OFF = "abs(coalesce((SELECT sum(s.amount) FROM transactions s WHERE s.split_from_id = t.id), t.amount) - t.amount) >= 0.005"

# Spending: regular transactions that aren't excluded or split, in visible accounts and counted categories.
_SPENDING = f"""
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN transactions p ON p.id = t.reimburses_id
    LEFT JOIN categories c ON c.id = {CATEGORY}
    WHERE {DAY} >= :start AND {DAY} < :end AND t.excluded = 0 AND coalesce(p.excluded, 0) = 0 AND a.hidden = 0
      AND {NOT_SPLIT}
"""


def current_month(today: date | None = None) -> str:
    """Returns this month as YYYY-MM."""
    return (today or date.today()).strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    """Returns the YYYY-MM month `delta` months after `month`."""
    year, mon = map(int, month.split("-"))
    index = year * 12 + mon - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(month: str) -> tuple[str, str]:
    """Returns the first day of `month` (YYYY-MM) and the first day of the next month, as ISO dates."""
    return f"{month}-01", f"{shift_month(month, 1)}-01"


def valid_month(month: str | None) -> str | None:
    """Returns `month` if it's a well-formed YYYY-MM, otherwise None."""
    if not month or len(month) != 7 or month[4] != "-":
        return None
    try:
        year, mon = int(month[:4]), int(month[5:])
    except ValueError:
        return None
    return month if 1900 < year < 3000 and 1 <= mon <= 12 else None


def month_totals(conn: sqlite3.Connection, month: str) -> dict:
    """Returns {"spent", "income", "net"} for the month. Refunds reduce spending; transfers don't count."""
    start, end = month_range(month)
    row = conn.execute(
        f"""SELECT
              coalesce(-sum(CASE WHEN t.type = 'regular' AND coalesce(c.excluded, 0) = 0 THEN t.amount END), 0) AS spent,
              coalesce(sum(CASE WHEN t.type = 'income' THEN t.amount END), 0) AS income
            {_SPENDING}""",
        {"start": start, "end": end},
    ).fetchone()
    spent, income = round(row["spent"], 2), round(row["income"], 2)
    return {"spent": spent, "income": income, "net": round(income - spent, 2)}


def daily_spending(conn: sqlite3.Connection, month: str) -> list[float]:
    """Returns the month's spending on each of its days (index 0 is the 1st), counted as in month_totals."""
    start, end = month_range(month)
    year, mon = map(int, month.split("-"))
    days = [0.0] * calendar.monthrange(year, mon)[1]
    for r in conn.execute(
        f"""SELECT {DAY} AS day, -sum(t.amount) AS spent {_SPENDING}
              AND t.type = 'regular' AND coalesce(c.excluded, 0) = 0
            GROUP BY day""",
        {"start": start, "end": end},
    ):
        days[int(r["day"][8:10]) - 1] = r["spent"]
    return days


def running_total(values: list[float]) -> list[float]:
    """Returns the running sum of `values`, rounded to cents."""
    out, total = [], 0.0
    for v in values:
        total += v
        out.append(round(total, 2))
    return out


def spending_pace(conn: sqlite3.Connection, month: str, today: date | None = None) -> dict:
    """Returns the month's spending added up day by day beside the month before's, for the dashboard chart.

    "this" stops at today in the current month (or at the last day with spending, if that's later) and is empty
    for future months; "last" covers the whole month before. "last_same_day" is what the month before had spent
    by the day "this" reaches, for comparing at the same point."""
    today = today or date.today()
    this = running_total(daily_spending(conn, month))
    last = running_total(daily_spending(conn, shift_month(month, -1)))
    days_in_month = len(this)
    current = current_month(today)
    if month > current:
        this = []
    elif month == current:
        # Spending dated later this month (a hand-entered bill, say) still shows up to its day.
        later = [i + 1 for i in range(today.day, days_in_month) if this[i] != this[i - 1]]
        this = this[:max([today.day] + later)]
    same_day = last[min(len(this), len(last)) - 1] if this and last else 0.0
    return {"this": this, "last": last, "days": max(days_in_month, len(last)), "last_same_day": same_day}


def category_spending(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Returns the month's spending as top-level categories, largest first. Each has its subcategories, a
    spending total that includes them, and total_budget (its own budget, else its subcategories' combined).
    Categories with a budget appear even with no spending."""
    start, end = month_range(month)
    own = {
        r["category_id"]: r["spent"]
        for r in conn.execute(
            f"""SELECT {CATEGORY} AS category_id, -sum(t.amount) AS spent {_SPENDING}
                  AND t.type = 'regular' AND coalesce(c.excluded, 0) = 0
                GROUP BY 1""",
            {"start": start, "end": end},
        )
    }
    cats = conn.execute("SELECT id, name, parent_id, budget FROM categories WHERE excluded = 0 ORDER BY name").fetchall()
    nodes = {c["id"]: {"id": c["id"], "name": c["name"], "budget": c["budget"], "spent": round(own.get(c["id"], 0), 2), "children": []}
             for c in cats}
    roots = []
    for c in cats:
        parent = nodes.get(c["parent_id"])
        (parent["children"] if parent else roots).append(nodes[c["id"]])
    for node in nodes.values():
        node["total_budget"] = node["budget"]
    for node in roots:
        node["own_spent"] = node["spent"]
        node["spent"] = round(node["spent"] + sum(ch["spent"] for ch in node["children"]), 2)
        node["total_budget"] = node["budget"] or round(sum(ch["budget"] or 0 for ch in node["children"]), 2) or None
        node["children"] = sorted((ch for ch in node["children"] if ch["spent"] or ch["budget"]), key=lambda n: -n["spent"])
    if own.get(None):
        spent = round(own[None], 2)
        roots.append({"id": None, "name": "Uncategorized", "budget": None, "total_budget": None,
                      "spent": spent, "own_spent": spent, "children": []})
    shown = [n for n in roots if n["spent"] or n["budget"] or n["children"]]
    return sorted(shown, key=lambda n: -n["spent"])


def budget_total(nodes: list[dict]) -> float:
    """Returns the month's total budget across the top-level categories from category_spending."""
    return round(sum(n["total_budget"] or 0 for n in nodes), 2)


def monthly_series(conn: sqlite3.Connection, months: int, last_month: str) -> list[dict]:
    """Returns [{"month", "spent", "income"}] for the `months` months ending with `last_month`, oldest first."""
    first = shift_month(last_month, -(months - 1))
    start, end = month_range(first)[0], month_range(last_month)[1]
    rows = {
        r["month"]: r
        for r in conn.execute(
            f"""SELECT substr({DAY}, 1, 7) AS month,
                  coalesce(-sum(CASE WHEN t.type = 'regular' AND coalesce(c.excluded, 0) = 0 THEN t.amount END), 0) AS spent,
                  coalesce(sum(CASE WHEN t.type = 'income' THEN t.amount END), 0) AS income
                {_SPENDING}
                GROUP BY month""",
            {"start": start, "end": end},
        )
    }
    out = []
    for i in range(months):
        month = shift_month(first, i)
        r = rows.get(month)
        out.append({"month": month, "spent": round(r["spent"], 2) if r else 0.0, "income": round(r["income"], 2) if r else 0.0})
    return out


def account_value(account_type: str, balance: float | None) -> float | None:
    """Returns the account's contribution to net worth: what it holds, or minus what's owed."""
    if balance is None:
        return None
    return -balance if account_type in LIABILITY_TYPES else balance


# An account kept by hand (a store's credit, say) has no bank to report its balance, and all of its money went through
# the app from zero, so what's left on it is what its transactions add up to, a split counted through its parts.
# Excluded ones are left out: excluded counts nowhere, so excluding a wrong row fixes the balance too.
_KEPT_BY_HAND = "a.source = 'manual' AND a.balance IS NULL"
_HELD = f"(SELECT sum(t.amount) FROM transactions t WHERE t.account_id = a.id AND t.excluded = 0 AND {NOT_SPLIT})"


def _left_on(a: sqlite3.Row) -> float:
    """Returns what an account kept by hand has left, shown as a bank would: what's owed is positive on a card or loan."""
    held = round(a["held"] or 0.0, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    return -held if a["type"] in LIABILITY_TYPES else held


def net_worth(conn: sqlite3.Connection) -> dict:
    """Returns accounts grouped by kind, plus overall assets, liabilities and net worth. Each group has "total", what
    its visible accounts add to net worth (negative for what's owed), and "balance", what they show the way banks show
    it: what's owed on cards and loans as a positive amount, and a negative one when the bank owes the user. An account
    kept by hand ("kept_by_hand") has the balance its transactions leave on it."""
    rows = conn.execute(
        f"""SELECT a.*, i.status AS connection_status, ({_KEPT_BY_HAND}) AS kept_by_hand, {_HELD} AS held FROM accounts a
           LEFT JOIN plaid_items i ON i.item_id = a.plaid_item_id
           ORDER BY a.hidden, a.name"""
    ).fetchall()
    accounts = [dict(a, balance=_left_on(a) if a["kept_by_hand"] else a["balance"]) for a in rows]
    groups = []
    assets = liabilities = 0.0
    for kind, label in ACCOUNT_GROUPS:
        members = [dict(a, value=account_value(a["type"], a["balance"])) for a in accounts if a["type"] == kind]
        if not members:
            continue
        total = sum(a["value"] for a in members if a["value"] is not None and not a["hidden"])
        balance = sum(a["balance"] for a in members if a["balance"] is not None and not a["hidden"])
        groups.append({"kind": kind, "label": label, "accounts": members, "total": round(total, 2), "balance": round(balance, 2)})
        if kind in LIABILITY_TYPES:
            liabilities -= total
        else:
            assets += total
    return {"groups": groups, "assets": round(assets, 2), "liabilities": round(liabilities, 2), "net": round(assets - liabilities, 2)}


def net_worth_history(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """Returns (date, net worth) for each day a balance was recorded, carrying each account's last balance forward.
    Accounts kept by hand count with what their transactions had left on them by that day."""
    rows = conn.execute(
        """SELECT h.date, h.account_id, h.balance, a.type FROM balance_history h
           JOIN accounts a ON a.id = h.account_id WHERE a.hidden = 0 ORDER BY h.date"""
    ).fetchall()
    # Accounts kept by hand have no recorded balances; what they add to net worth is their transactions so far.
    kept = conn.execute(
        f"""SELECT t.date, sum(t.amount) AS amount FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {_KEPT_BY_HAND} AND a.hidden = 0 AND t.excluded = 0 AND {NOT_SPLIT} GROUP BY t.date ORDER BY t.date"""
    ).fetchall()
    latest: dict[int, float] = {}
    out: list[tuple[str, float]] = []
    held, k = 0.0, 0
    for r in rows:
        while k < len(kept) and kept[k]["date"] <= r["date"]:
            held += kept[k]["amount"]
            k += 1
        latest[r["account_id"]] = account_value(r["type"], r["balance"])
        total = round(sum(latest.values()) + held, 2)
        if out and out[-1][0] == r["date"]:
            out[-1] = (r["date"], total)
        else:
            out.append((r["date"], total))
    return out
