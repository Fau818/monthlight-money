"""Recurring charges: tracking them, estimating the next charge, and spotting new ones."""

import calendar
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import pairwise

FREQUENCIES = {"weekly": 7, "biweekly": 14, "monthly": 30.44, "quarterly": 91.31, "yearly": 365.25}
# Gaps between charges (in days) accepted as each frequency when spotting new recurring charges.
GAP_RANGES = {"weekly": (6, 8), "biweekly": (12, 16), "monthly": (26, 35), "quarterly": (84, 98), "yearly": (350, 380)}


def infer_frequency(dates: list[date]) -> str:
    """Returns the frequency closest to the typical gap between `dates` ('monthly' if there are fewer than two)."""
    days = sorted(set(dates))
    if len(days) < 2:
        return "monthly"
    gap = statistics.median((b - a).days for a, b in pairwise(days))
    return min(FREQUENCIES, key=lambda f: abs(FREQUENCIES[f] - gap))


def add_months(day: date, months: int) -> date:
    """Returns the same day `months` later, moved back to the month's last day when needed."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def next_date(last: date, frequency: str) -> date:
    """Returns when the charge after one on `last` is expected."""
    if frequency == "weekly":
        return last + timedelta(days=7)
    if frequency == "biweekly":
        return last + timedelta(days=14)
    return add_months(last, {"quarterly": 3, "yearly": 12}.get(frequency, 1))


def monthly_cost(amount: float | None, frequency: str) -> float:
    """Returns what a charge of `amount` at `frequency` costs per month on average, as a positive number."""
    return abs(amount or 0) * FREQUENCIES["monthly"] / FREQUENCIES.get(frequency, FREQUENCIES["monthly"])


def amount_close(expected: float | None, amount: float) -> bool:
    """Returns True if `amount` is within half to one and a half times `expected`, with the same sign."""
    if expected is None:
        return True
    if (expected < 0) != (amount < 0):
        return False
    return 0.5 * abs(expected) <= abs(amount) <= 1.5 * abs(expected)


class Matcher:
    """Finds the tracked recurring charge a transaction belongs to; loads the list once."""

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute("SELECT id, pattern, amount FROM recurring WHERE active = 1").fetchall()
        self.items = sorted(rows, key=lambda r: -len(r["pattern"]))

    def match(self, name: str | None, amount: float) -> int | None:
        """Returns the id of the recurring charge matching this transaction, or None."""
        text = (name or "").lower()
        for r in self.items:
            if r["pattern"] and r["pattern"] in text and amount_close(r["amount"], amount):
                return r["id"]
        return None


def track(conn: sqlite3.Connection, name: str, pattern: str, frequency: str, amount: float | None,
          category_id: int | None) -> tuple[int, int]:
    """Starts tracking a recurring charge and links past transactions that match it. Returns (id, transactions linked)."""
    pattern = pattern.strip().lower()
    rid = conn.execute(
        "INSERT INTO recurring (name, pattern, amount, frequency, category_id) VALUES (?, ?, ?, ?, ?)",
        (name, pattern, amount, frequency, category_id),
    ).lastrowid
    candidates = conn.execute(
        # The parts of a split charge go by the charge itself, which keeps its link to the recurring charge.
        "SELECT id, amount FROM transactions WHERE recurring_id IS NULL AND type != 'transfer' AND split_from_id IS NULL "
        "AND instr(lower(name), ?) > 0",
        (pattern,),
    ).fetchall()
    ids = [(rid, c["id"]) for c in candidates if amount_close(amount, c["amount"])]
    conn.executemany("UPDATE transactions SET recurring_id = ? WHERE id = ?", ids)
    return rid, len(ids)


def list_recurring(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Returns every tracked recurring charge with its last charge, next expected date and monthly cost, soonest first."""
    today = today or date.today()
    rows = conn.execute(
        """SELECT r.*, c.name AS category, max(t.date) AS last_date, count(t.id) AS count
           FROM recurring r
           LEFT JOIN transactions t ON t.recurring_id = r.id
           LEFT JOIN categories c ON c.id = r.category_id
           GROUP BY r.id"""
    ).fetchall()
    out = []
    for r in rows:
        last = date.fromisoformat(r["last_date"]) if r["last_date"] else None
        upcoming = next_date(last, r["frequency"]) if last else None
        out.append({
            **dict(r),
            "last_date": last,
            "next_date": upcoming,
            # A charge more than a week overdue may have been canceled.
            "late": bool(upcoming and upcoming < today - timedelta(days=7)),
            "monthly": monthly_cost(r["amount"], r["frequency"]),
        })
    out.sort(key=lambda r: (not r["active"], r["next_date"] or date.max))
    return out


def upcoming(conn: sqlite3.Connection, days: int = 30, today: date | None = None) -> list[dict]:
    """Returns active recurring charges due within `days` days or up to a week overdue, soonest first."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    return [r for r in list_recurring(conn, today)
            if r["active"] and r["next_date"] and not r["late"] and r["next_date"] <= horizon]


def month_summary(conn: sqlite3.Connection, items: list[dict], today: date | None = None) -> dict:
    """Returns this month's tracked charges from list_recurring's `items`: "paid", what the charges dated this month
    came to; "due", what's still expected before the month ends (as positive amounts); and "due_items", those
    charges soonest first. A charge a few days overdue is still due; one more than a week overdue isn't (see
    list_recurring)."""
    today = today or date.today()
    start = today.replace(day=1)
    end = add_months(start, 1)
    paid = conn.execute(
        "SELECT coalesce(-sum(amount), 0) FROM transactions WHERE recurring_id IS NOT NULL AND amount < 0 AND date >= ? AND date < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    due_items = [r for r in items if r["active"] and not r["late"] and r["next_date"] and r["next_date"] < end
                 and (r["amount"] or 0) < 0]
    return {"paid": round(paid, 2), "due": round(sum(-r["amount"] for r in due_items), 2), "due_items": due_items}


def find_candidates(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Returns charges that repeat on a regular schedule but aren't tracked yet, most frequent first."""
    today = today or date.today()
    since = (today - timedelta(days=400)).isoformat()
    tracked = [r["pattern"] for r in conn.execute("SELECT pattern FROM recurring")]
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for t in conn.execute(
        """SELECT lower(trim(name)) AS key, name, date, amount, category_id FROM transactions
           WHERE recurring_id IS NULL AND type = 'regular' AND amount < 0 AND date >= ? AND split_from_id IS NULL
           ORDER BY date""",
        (since,),
    ):
        groups[t["key"]].append(t)

    candidates = []
    for key, txns in groups.items():
        if len(txns) < 3 or any(p in key for p in tracked):
            continue
        days = sorted({date.fromisoformat(t["date"]) for t in txns})
        if len(days) < 3:
            continue
        frequency = infer_frequency(days)
        low, high = GAP_RANGES[frequency]
        gaps = [(b - a).days for a, b in pairwise(days)]
        if sum(low <= g <= high for g in gaps) / len(gaps) < 0.75:
            continue
        amounts = [abs(t["amount"]) for t in txns]
        typical = statistics.median(amounts[-3:])
        if sum(abs(a - typical) <= 0.25 * typical for a in amounts) / len(amounts) < 0.75:
            continue
        if (today - days[-1]).days > FREQUENCIES[frequency] * 1.5 + 5:
            continue  # stopped a while ago
        category = Counter(t["category_id"] for t in txns).most_common(1)[0][0]
        candidates.append({
            "name": txns[-1]["name"], "pattern": key, "frequency": frequency,
            "amount": -round(typical, 2), "count": len(txns), "last_date": days[-1],
            "category_id": category,
        })
    candidates.sort(key=lambda c: -c["count"])
    return candidates
