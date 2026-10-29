"""Numbers for the dashboard, budgets and accounts pages."""

import calendar
import json
import sqlite3
from datetime import date

from . import tags
from .db import get_meta, set_meta

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


def spending_by_category(conn: sqlite3.Connection, start: str, end: str) -> dict[tuple[str, int | None], float]:
    """Returns the spending from `start` to `end` (ISO days, the end left out) by month and category, as month_totals
    counts it: {(YYYY-MM, category id or None): amount}."""
    return {(r["month"], r["category_id"]): r["spent"] for r in conn.execute(
        f"""SELECT substr({DAY}, 1, 7) AS month, {CATEGORY} AS category_id, -sum(t.amount) AS spent {_SPENDING}
              AND t.type = 'regular' AND coalesce(c.excluded, 0) = 0
            GROUP BY 1, 2""",
        {"start": start, "end": end},
    )}


def effective_budgets(conn: sqlite3.Connection, month: str) -> dict[int, float | None]:
    """Returns each category's budget for `month`, by id: the one set from the latest month on or before it (the
    budgets table), else the category's own."""
    return {r["id"]: r["budget"] for r in conn.execute(
        """SELECT c.id, CASE WHEN b.month IS NULL THEN c.budget ELSE b.amount END AS budget FROM categories c
           LEFT JOIN budgets b ON b.category_id = c.id
             AND b.month = (SELECT max(x.month) FROM budgets x WHERE x.category_id = c.id AND x.month <= ?)""",
        (month,))}


def rollover_carry(conn: sqlite3.Connection, month: str) -> dict[int, float]:
    """Returns what carries into `month` for each category whose budget rolls over (categories.rollover_since, before
    `month`), by id: what was left of its budget in each month since, minus what went over it. A group counts its
    subcategories' spending too; a month without a budget carries nothing."""
    rolling = conn.execute("SELECT id, rollover_since FROM categories WHERE rollover_since IS NOT NULL AND rollover_since < ? AND excluded = 0",
                           (month,)).fetchall()
    if not rolling:
        return {}
    first = min(r["rollover_since"] for r in rolling)
    spent = spending_by_category(conn, month_range(first)[0], month_range(month)[0])
    children: dict[int, list[int]] = {}
    for r in conn.execute("SELECT id, parent_id FROM categories WHERE parent_id IS NOT NULL AND excluded = 0"):
        children.setdefault(r["parent_id"], []).append(r["id"])
    budgets_by_month: dict[str, dict] = {}
    carry = {}
    for r in rolling:
        total, m = 0.0, r["rollover_since"]
        while m < month:
            if m not in budgets_by_month:
                budgets_by_month[m] = effective_budgets(conn, m)
            budget = budgets_by_month[m].get(r["id"])
            if budget is not None:
                total += budget - sum(spent.get((m, cid), 0.0) for cid in [r["id"], *children.get(r["id"], [])])
            m = shift_month(m, 1)
        carry[r["id"]] = round(total, 2) + 0.0  # NOTE: + 0.0 turns -0.0 into 0.0
    return carry


def category_spending(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Returns the month's spending as top-level categories, largest first. Each has its subcategories, a spending
    total that includes them, its budget for the month (effective_budgets), "carry" (what rolls over into the month,
    rollover_carry) and "rollover" (the month it rolls over since, if it does), and total_budget: what it has to
    spend, its budget and carry, else its subcategories' combined. Categories with a budget appear even with no
    spending."""
    start, end = month_range(month)
    own = {cid: amount for (_, cid), amount in spending_by_category(conn, start, end).items()}
    budgets, carry = effective_budgets(conn, month), rollover_carry(conn, month)
    cats = conn.execute("SELECT id, name, parent_id, rollover_since FROM categories WHERE excluded = 0 ORDER BY name").fetchall()
    nodes = {}
    for c in cats:
        budget = budgets.get(c["id"])
        nodes[c["id"]] = {"id": c["id"], "name": c["name"], "budget": budget, "spent": round(own.get(c["id"], 0), 2), "children": [],
                          "carry": carry.get(c["id"], 0.0) if budget is not None else 0.0,
                          "rollover": c["rollover_since"] if c["rollover_since"] and c["rollover_since"] <= month else None}
    roots = []
    for c in cats:
        parent = nodes.get(c["parent_id"])
        (parent["children"] if parent else roots).append(nodes[c["id"]])
    for node in nodes.values():
        node["total_budget"] = round(node["budget"] + node["carry"], 2) if node["budget"] is not None else None
    for node in roots:
        node["own_spent"] = node["spent"]
        node["spent"] = round(node["spent"] + sum(ch["spent"] for ch in node["children"]), 2)
        if node["budget"] is None:
            node["total_budget"] = round(sum(ch["total_budget"] or 0 for ch in node["children"]), 2) or None
        node["children"] = sorted((ch for ch in node["children"] if ch["spent"] or ch["budget"]), key=lambda n: -n["spent"])
    if own.get(None):
        spent = round(own[None], 2)
        roots.append({"id": None, "name": "Uncategorized", "budget": None, "total_budget": None, "carry": 0.0, "rollover": None,
                      "spent": spent, "own_spent": spent, "children": []})
    shown = [n for n in roots if n["spent"] or n["budget"] is not None or n["children"]]
    return sorted(shown, key=lambda n: -n["spent"])


def budget_total(nodes: list[dict]) -> float:
    """Returns the month's total budget across the top-level categories from category_spending."""
    return round(sum(n["total_budget"] or 0 for n in nodes), 2)


def monthly_series(conn: sqlite3.Connection, months: int, last_month: str, leave: dict | None = None) -> list[dict]:
    """Returns [{"month", "spent", "income"}] for the `months` months ending with `last_month`, oldest first, leaving
    out `leave` (leave_out_sql)."""
    first = shift_month(last_month, -(months - 1))
    start, end = month_range(first)[0], month_range(last_month)[1]
    clause, params = leave_out_sql(leave or {})
    rows = {
        r["month"]: r
        for r in conn.execute(
            f"""SELECT substr({DAY}, 1, 7) AS month,
                  coalesce(-sum(CASE WHEN t.type = 'regular' AND coalesce(c.excluded, 0) = 0 THEN t.amount END), 0) AS spent,
                  coalesce(sum(CASE WHEN t.type = 'income' THEN t.amount END), 0) AS income
                {_REPORTED} AND {clause}
                GROUP BY month""",
            {"start": start, "end": end, **params},
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


# A report (main.reports_page) counts as the dashboard does, and can leave things out for itself alone: accounts,
# categories (a group with its subcategories), tags, merchants, single transactions, and anything over an amount.
# Everything about a payback follows its purchase: its day, category, account, tags, merchant and size.
_REPORTED = f"""
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    LEFT JOIN transactions p ON p.id = t.reimburses_id
    LEFT JOIN transactions sp ON sp.id = t.split_from_id
    LEFT JOIN transactions psp ON psp.id = p.split_from_id
    LEFT JOIN categories c ON c.id = {CATEGORY}
    WHERE {DAY} >= :start AND {DAY} < :end AND t.excluded = 0 AND coalesce(p.excluded, 0) = 0 AND a.hidden = 0
      AND {NOT_SPLIT}
"""
# The merchant a transaction counts with: a part of a split goes by its split's name, and a payback by its purchase's.
MERCHANT = "CASE WHEN p.id IS NULL THEN coalesce(sp.name, t.name) ELSE coalesce(psp.name, p.name) END"
# What a report can count: which transactions, and what each adds (spending as a positive amount, a refund negative).
MEASURES = {"spending": ("t.type = 'regular' AND coalesce(c.excluded, 0) = 0", "-t.amount"), "income": ("t.type = 'income'", "t.amount")}
LEAVE_OUTS = ("account", "category", "tag", "merchant", "id")  # each a list; "over" is an amount
MAX_SERIES = 8  # a chart's series at most: past that, the smallest are folded into one, "other"


def no_leave_outs() -> dict:
    """Returns leave-outs (leave_out_sql) that leave nothing out."""
    return {**{name: [] for name in LEAVE_OUTS}, "over": None}


def merge_leave_outs(*leaves: dict) -> dict:
    """Returns leave-outs that leave out everything any of `leaves` does."""
    merged = no_leave_outs()
    for leave in leaves:
        for name in LEAVE_OUTS:
            merged[name] = list(dict.fromkeys(merged[name] + list(leave.get(name) or [])))
        if leave.get("over") is not None:
            merged["over"] = leave["over"] if merged["over"] is None else min(merged["over"], leave["over"])
    return merged


def leave_out_sql(leave: dict) -> tuple[str, dict]:
    """Returns the SQL condition on _REPORTED's rows that leaves out what `leave` names, and its named parameters:
    - "account", "tag" and "id": ids; leaving out a transaction leaves out its paybacks, and a split its parts
    - "category": ids, a group with its subcategories, and "none" for spending without a category
    - "merchant": names, whatever their case
    - "over": an amount, money in and out alike, past which anything is left out"""
    where, params = [], {}

    def listed(prefix: str, values: list, each: str = "{}") -> str:
        names = [f"{prefix}{i}" for i in range(len(values))]
        params.update(zip(names, values))
        return ", ".join(each.format(f":{name}") for name in names)

    if leave.get("account"):
        where.append(f"coalesce(p.account_id, t.account_id) NOT IN ({listed('la', leave['account'])})")
    ids = [int(c) for c in leave.get("category") or [] if str(c).isdigit()]
    if ids:
        either = listed("lc", ids)
        where.append(f"(t.type != 'regular' OR (coalesce(c.id, 0) NOT IN ({either}) AND coalesce(c.parent_id, 0) NOT IN ({either})))")
    if "none" in (leave.get("category") or []):
        where.append("(t.type != 'regular' OR c.id IS NOT NULL)")
    if leave.get("tag"):
        where.append(f"""NOT EXISTS (SELECT 1 FROM transaction_tags lt WHERE lt.transaction_id = coalesce(p.id, t.id)
                                    AND lt.tag_id IN ({listed('lg', leave['tag'])}))""")
    if leave.get("merchant"):
        where.append(f"lower({MERCHANT}) NOT IN ({listed('lm', leave['merchant'], 'lower({})')})")
    if leave.get("id"):
        either = listed("li", leave["id"])
        where.append(f"t.id NOT IN ({either}) AND coalesce(p.id, 0) NOT IN ({either}) AND coalesce(t.split_from_id, 0) NOT IN ({either})")
    if leave.get("over") is not None:
        params["lo"] = leave["over"] + 0.005
        where.append("abs(coalesce(p.amount, t.amount)) <= :lo")
    return " AND ".join(where) or "1 = 1", params


def counted(conn: sqlite3.Connection, start: str, end: str, measure: str = "spending", leave: dict | None = None) -> list[dict]:
    """Returns the transactions `measure` (MEASURES) counts from `start` to `end` (ISO days, the end left out), leaving
    out `leave` (leave_out_sql), in the order they count. Each has "day" (the day it counts on), "value" (what it adds),
    "category_id", "merchant", "account_id" (its purchase's, for a payback) and "tags" (its purchase's, for a payback)."""
    where, value = MEASURES[measure]
    clause, params = leave_out_sql(leave or {})
    rows = [dict(r) for r in conn.execute(
        f"""SELECT t.id, t.name, t.date, t.account_id AS own_account_id, t.reimburses_id, t.split_from_id, t.amount,
                   {DAY} AS day, {value} AS value, c.id AS category_id, {MERCHANT} AS merchant,
                   coalesce(p.account_id, t.account_id) AS account_id, coalesce(p.id, t.id) AS tag_source
            {_REPORTED} AND {where} AND {clause}
            ORDER BY day, t.id""",
        {"start": start, "end": end, **params},
    )]
    tagged = tags.tags_by_transaction(conn, list({r["tag_source"] for r in rows}))
    for r in rows:
        r["tags"] = tagged.get(r["tag_source"], [])
    return rows


def category_groups(conn: sqlite3.Connection) -> dict[int, int]:
    """Returns the category each category adds up in, by id, as category_spending adds them up: its group, unless that's
    excluded, else itself."""
    rows = conn.execute("SELECT id, parent_id, excluded FROM categories").fetchall()
    counted_ids = {r["id"] for r in rows if not r["excluded"]}
    return {r["id"]: r["parent_id"] if r["parent_id"] in counted_ids else r["id"] for r in rows}


def merchant_key(name: str | None) -> str:
    """Returns the key a merchant's transactions share, whatever the case and spacing of their names."""
    return " ".join((name or "").split()).lower()


def keys_of(row: dict, group: str, groups: dict[int, int]) -> list[str]:
    """Returns the keys of the series transaction `row` (from counted) adds to when grouped by `group` (one, or one for
    each of its tags): a category's group, a merchant (merchant_key), an account or a tag by id, or "none"."""
    if group == "category":
        return [str(groups.get(row["category_id"], row["category_id"]))] if row["category_id"] is not None else ["none"]
    if group == "merchant":
        return [merchant_key(row["merchant"])]
    if group == "account":
        return [str(row["account_id"])]
    return [str(tag) for tag in row["tags"]] or ["none"]


def column_of(day: str, by: str) -> str:
    """Returns the column a day (or a month, YYYY-MM) adds up in: its month, its quarter ("2026-Q1") or its year."""
    if by == "quarter":
        return f"{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}"
    return day[:4] if by == "year" else day[:7]


def column_range(column: str) -> tuple[str, str]:
    """Returns the first day of a column (column_of) and the first day after it, as ISO dates."""
    if "-Q" in column:
        first = f"{column[:4]}-{(int(column[-1]) - 1) * 3 + 1:02d}"
        return month_range(first)[0], month_range(shift_month(first, 2))[1]
    if len(column) == 4:
        return f"{column}-01-01", f"{int(column) + 1}-01-01"
    return month_range(column)


def grouped(rows: list[dict], columns: list[str], by: str, group: str, groups: dict[int, int]) -> dict:
    """Returns rows from counted added up by column and series: {"series": [{"key", "total", "count", "values"}], "total",
    "count", "folded"}. "values" holds each of `columns`' amount. Series come biggest first; past MAX_SERIES, the
    smallest are folded into one with the key "other", last, and "folded" lists their keys. A transaction with several
    tags adds to each of them, so the series can add up to more than "total"."""
    series: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        column = column_of(r["day"], by)
        total += r["value"]
        for key in keys_of(r, group, groups):
            s = series.setdefault(key, {"key": key, "total": 0.0, "count": 0, "values": dict.fromkeys(columns, 0.0)})
            s["total"] += r["value"]
            s["count"] += 1
            if column in s["values"]:
                s["values"][column] += r["value"]
    ordered = sorted(series.values(), key=lambda s: (-s["total"], s["key"]))
    folded = []
    if len(ordered) > MAX_SERIES:
        kept, rest = ordered[:MAX_SERIES - 1], ordered[MAX_SERIES - 1:]
        other = {"key": "other", "total": sum(s["total"] for s in rest), "count": sum(s["count"] for s in rest),
                 "values": {c: sum(s["values"][c] for s in rest) for c in columns}}
        ordered, folded = kept + [other], [s["key"] for s in rest]
    for s in ordered:
        s["total"] = round(s["total"], 2)
        s["values"] = {c: round(v, 2) for c, v in s["values"].items()}
    return {"series": ordered, "total": round(total, 2), "count": len(rows), "folded": folded}


def totals(conn: sqlite3.Connection, start: str, end: str, leave: dict | None = None) -> dict:
    """Returns {"spent", "income", "net", "rate"} from `start` to `end` (ISO days, the end left out), leaving out
    `leave` (leave_out_sql), counted as month_totals counts them; "rate" is the share of income kept, None without income."""
    clause, params = leave_out_sql(leave or {})
    row = conn.execute(
        f"""SELECT
              coalesce(-sum(CASE WHEN {MEASURES['spending'][0]} THEN t.amount END), 0) AS spent,
              coalesce(sum(CASE WHEN {MEASURES['income'][0]} THEN t.amount END), 0) AS income
            {_REPORTED} AND {clause}""",
        {"start": start, "end": end, **params},
    ).fetchone()
    spent, income = round(row["spent"], 2), round(row["income"], 2)
    net = round(income - spent, 2)
    return {"spent": spent, "income": income, "net": net, "rate": round(net / income * 100, 1) if income > 0 else None}


def biggest(conn: sqlite3.Connection, start: str, end: str, measure: str = "spending", leave: dict | None = None,
            limit: int = 10) -> list[dict]:
    """Returns the biggest transactions `measure` counts from `start` to `end`, leaving out `leave`, biggest first:
    purchases (a split by its parts) or money in, never a payback, which counts with its purchase. Each purchase has
    "paid_back", what its paybacks came to, if any."""
    where, value = MEASURES[measure]
    clause, params = leave_out_sql(leave or {})
    return [dict(r) for r in conn.execute(
        f"""SELECT t.id, t.name, t.date, t.amount, t.account_id, c.id AS category_id, {MERCHANT} AS merchant,
                   (SELECT sum(b.amount) FROM transactions b WHERE b.reimburses_id = t.id AND b.excluded = 0) AS paid_back
            {_REPORTED} AND {where} AND {clause} AND t.reimburses_id IS NULL AND {value} > 0
            ORDER BY {value} DESC, t.date DESC, t.id LIMIT :limit""",
        {"start": start, "end": end, "limit": limit, **params},
    )]


def everyday(conn: sqlite3.Connection) -> dict:
    """Returns what the user set aside from everyday spending (main.everyday_page), as leave-outs (leave_out_sql)."""
    try:
        saved = json.loads(get_meta(conn, "everyday") or "{}")
    except ValueError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    over = saved.get("over")
    return merge_leave_outs({name: [v for v in saved.get(name) or [] if isinstance(v, (int, str))] for name in LEAVE_OUTS}
                            | {"over": float(over) if isinstance(over, (int, float)) and over > 0 else None})


def set_everyday(conn: sqlite3.Connection, leave: dict) -> None:
    """Stores `leave` as what's set aside from everyday spending."""
    kept = {name: leave[name] for name in LEAVE_OUTS if leave.get(name)}
    if leave.get("over") is not None:
        kept["over"] = leave["over"]
    set_meta(conn, "everyday", json.dumps(kept))


def set_budget(conn: sqlite3.Connection, category_id: int, month: str, amount: float | None, only: bool = False) -> bool:
    """Sets a category's budget from `month` on, or for that month alone (`only`), when the month after keeps the budget
    it had. Returns whether anything changed."""
    later = conn.execute("SELECT 1 FROM budgets WHERE category_id = ? AND month > ? LIMIT 1", (category_id, month)).fetchone()
    if effective_budgets(conn, month).get(category_id) == amount and (only or not later):
        return False
    if only:
        after = shift_month(month, 1)
        if not conn.execute("SELECT 1 FROM budgets WHERE category_id = ? AND month = ?", (category_id, after)).fetchone():
            conn.execute("INSERT INTO budgets (category_id, month, amount) VALUES (?, ?, ?)",
                         (category_id, after, effective_budgets(conn, after).get(category_id)))
    else:
        conn.execute("DELETE FROM budgets WHERE category_id = ? AND month > ?", (category_id, month))
    conn.execute("INSERT INTO budgets (category_id, month, amount) VALUES (?, ?, ?) "
                 "ON CONFLICT (category_id, month) DO UPDATE SET amount = excluded.amount", (category_id, month, amount))
    return True


def account_history(conn: sqlite3.Connection, account: dict) -> list[tuple[str, float]]:
    """Returns (date, balance) for each day an account's balance is known, shown the way banks show it (what's owed on a
    card or loan positive): the balances recorded, or for an account kept by hand (net_worth's "kept_by_hand"), what
    its transactions had left on it by each day they changed it."""
    if not account.get("kept_by_hand"):
        return [(r["date"], r["balance"]) for r in conn.execute(
            "SELECT date, balance FROM balance_history WHERE account_id = ? ORDER BY date", (account["id"],))]
    sign = -1 if account["type"] in LIABILITY_TYPES else 1
    out, held = [], 0.0
    for r in conn.execute(f"""SELECT t.date, sum(t.amount) AS amount FROM transactions t
                              WHERE t.account_id = ? AND t.excluded = 0 AND {NOT_SPLIT} GROUP BY t.date ORDER BY t.date""",
                          (account["id"],)):
        held += r["amount"]
        out.append((r["date"], round(sign * held, 2) + 0.0))
    return out
