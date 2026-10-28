"""Importing a Copilot Money transactions export (CSV)."""

import csv
import hashlib
import io
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from . import recurring
from .categorize import add_rule, auto_map_plaid_categories, get_or_create_category
from .db import set_meta

# Header names we accept for each field, after lowercasing and turning "_" and "-" into spaces.
COLUMNS = {
    "date": ("date", "transaction date", "posted date"),
    "name": ("name", "merchant", "description", "payee"),
    "amount": ("amount",),
    "status": ("status", "pending"),
    "category": ("category",),
    "parent_category": ("parent category", "category group", "parent"),
    "excluded": ("excluded", "exclude"),
    "type": ("type", "transaction type"),
    "account": ("account", "account name"),
    "mask": ("account mask", "mask", "account number"),
    "note": ("note", "notes"),
    "recurring": ("recurring", "recurring name"),
}
REQUIRED = ("date", "name", "amount")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")
TRUE_WORDS = {"true", "yes", "y", "1", "x"}


class CopilotImportError(ValueError):
    """The file can't be read as a Copilot export; the message says why."""


@dataclass
class Row:
    date: date
    name: str
    amount: float  # as written in the file
    pending: bool
    category: str
    parent_category: str
    excluded: bool
    type: str  # regular | income | transfer
    account: str
    mask: str
    note: str
    recurring: str


@dataclass
class Parsed:
    rows: list[Row]
    sign: int  # file amount * sign = our amount (money in positive)
    columns: dict[str, str]  # field -> header as written in the file
    unreadable: int  # lines skipped because the date or amount couldn't be read


@dataclass
class ImportResult:
    added: int = 0
    duplicates: int = 0
    pending_skipped: int = 0
    accounts_created: int = 0
    categories_created: int = 0
    rules_created: int = 0
    recurring_created: int = 0
    accounts: list[str] = field(default_factory=list)


def _normalize_header(header: str) -> str:
    return re.sub(r"[\s_\-]+", " ", header.strip().lower())


def parse_date(text: str) -> date:
    """Returns the date in `text` (ISO, or US month/day/year); raises ValueError if it isn't one."""
    text = text.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text[:10])
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {text!r}")


def parse_amount(text: str) -> float:
    """Returns the number in `text`, allowing $, commas, a Unicode minus and (parentheses) for negatives."""
    cleaned = text.strip().replace("$", "").replace(",", "").replace("−", "-").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    value = float(cleaned.strip("()"))
    return -value if negative else value


def _parse_type(text: str) -> str:
    text = text.lower()
    if "income" in text:
        return "income"
    if "transfer" in text:
        return "transfer"
    return "regular"


def _mask(text: str) -> str:
    return re.sub(r"\D", "", text)[-4:]


def detect_sign(rows: list[Row]) -> int:
    """Returns -1 if the file writes spending as positive numbers, otherwise 1."""
    income = [r.amount for r in rows if r.type == "income" and r.amount]
    if income:
        return -1 if sum(a < 0 for a in income) > len(income) / 2 else 1
    regular = [r.amount for r in rows if r.type == "regular" and r.amount]
    return -1 if sum(a > 0 for a in regular) > len(regular) / 2 else 1


def parse(text: str) -> Parsed:
    """Reads a Copilot CSV export. Returns its rows and sign convention; raises CopilotImportError if required columns are missing."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, None)
    if not header:
        raise CopilotImportError("The file is empty.")
    normalized = [_normalize_header(h) for h in header]
    index: dict[str, int] = {}
    for name, aliases in COLUMNS.items():
        found = next((normalized.index(a) for a in aliases if a in normalized), None)
        if found is not None:
            index[name] = found
    missing = [c for c in REQUIRED if c not in index]
    if missing:
        raise CopilotImportError(f"Missing column(s): {', '.join(missing)}. The file's columns are: {', '.join(header)}")

    rows, unreadable = [], 0
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        cell = {name: (record[i].strip() if i < len(record) else "") for name, i in index.items()}
        try:
            day, amount = parse_date(cell["date"]), parse_amount(cell["amount"])
        except ValueError:
            unreadable += 1
            continue
        rows.append(Row(
            date=day,
            name=cell["name"] or "Unknown",
            amount=amount,
            pending=cell.get("status", "").lower() in TRUE_WORDS | {"pending"},
            category=cell.get("category", ""),
            parent_category=cell.get("parent_category", ""),
            excluded=cell.get("excluded", "").lower() in TRUE_WORDS,
            type=_parse_type(cell.get("type", "")),
            account=cell.get("account", "") or "Copilot account",
            mask=_mask(cell.get("mask", "")),
            note=cell.get("note", ""),
            recurring=cell.get("recurring", ""),
        ))
    if not rows:
        raise CopilotImportError("No transactions could be read from the file.")
    return Parsed(rows, detect_sign(rows), {name: header[i] for name, i in index.items()}, unreadable)


def summarize(parsed: Parsed) -> dict:
    """Returns what an import would bring in (counts, date range, accounts), for review before importing."""
    rows = parsed.rows
    posted = [r for r in rows if not r.pending]
    accounts = Counter((r.account, r.mask) for r in posted)
    return {
        "rows": len(rows),
        "posted": len(posted),
        "pending": len(rows) - len(posted),
        "unreadable": parsed.unreadable,
        "start": min(r.date for r in rows),
        "end": max(r.date for r in rows),
        "accounts": [{"name": n, "mask": m, "count": c} for (n, m), c in accounts.most_common()],
        "categories": len({r.category for r in rows if r.category}),
        "recurring": len({r.recurring for r in rows if r.recurring}),
        "spending_positive": parsed.sign == -1,
        "columns": parsed.columns,
        "missing_columns": [c for c in COLUMNS if c not in parsed.columns],
    }


def _guess_account_type(name: str) -> str:
    text = name.lower()
    if any(w in text for w in ("credit", "card", "visa", "mastercard", "amex", "sapphire", "freedom")):
        return "credit"
    if any(w in text for w in ("loan", "mortgage")):
        return "loan"
    if any(w in text for w in ("brokerage", "401k", "401(k)", "ira", "invest")):
        return "investment"
    return "depository"


def _rules_from_history(conn) -> int:
    """Creates an exact-name rule for each merchant the export consistently categorized one way; returns how many."""
    groups: dict[str, list[tuple]] = defaultdict(list)
    for r in conn.execute("SELECT lower(trim(name)) AS key, category_id, type FROM transactions WHERE source = 'copilot'"):
        groups[r["key"]].append((r["category_id"], r["type"]))
    existing = {r["pattern"] for r in conn.execute("SELECT pattern FROM rules WHERE match = 'equals'")}
    created = 0
    for key, values in groups.items():
        if not key or key in existing or len(values) < 2:
            continue
        (category, type_), count = Counter(values).most_common(1)[0]
        if count / len(values) < 0.8 or (category is None and type_ == "regular"):
            continue
        add_rule(conn, key, "equals", category, type_)
        created += 1
    return created


def _recurring_from_history(conn, linked: dict[str, list[int]]) -> int:
    """Tracks each recurring charge named in the export and links its transactions; returns how many were new."""
    created = 0
    for name, ids in linked.items():
        existing = conn.execute("SELECT id FROM recurring WHERE name = ?", (name,)).fetchone()
        if existing:
            rid = existing["id"]
        else:
            txns = conn.execute(
                f"SELECT date, amount, name, category_id FROM transactions WHERE id IN ({','.join('?' * len(ids))}) ORDER BY date",
                ids,
            ).fetchall()
            recent = [t["amount"] for t in txns[-3:]]
            sign = -1 if sum(a < 0 for a in recent) >= len(recent) / 2 else 1
            rid = conn.execute(
                "INSERT INTO recurring (name, pattern, amount, frequency, category_id) VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    Counter(t["name"].strip().lower() for t in txns).most_common(1)[0][0],
                    sign * round(statistics.median(abs(a) for a in recent), 2),
                    recurring.infer_frequency([date.fromisoformat(t["date"]) for t in txns]),
                    Counter(t["category_id"] for t in txns).most_common(1)[0][0],
                ),
            ).lastrowid
            created += 1
        conn.executemany("UPDATE transactions SET recurring_id = ? WHERE id = ?", [(rid, i) for i in ids])
    return created


def import_parsed(conn, parsed: Parsed) -> ImportResult:
    """Adds the export's posted transactions with their accounts, categories, rules and recurring charges.
    Rows imported before are skipped, so importing the same file twice is safe, even after renaming accounts or
    categories. Returns what was added."""
    result = ImportResult()
    accounts: dict[tuple[str, str], int] = {}
    categories: dict[tuple[str, str], int | None] = {}
    seen: Counter = Counter()
    recurring_ids: dict[str, list[int]] = defaultdict(list)
    with conn:
        categories_before = conn.execute("SELECT count(*) FROM categories").fetchone()[0]
        for r in parsed.rows:
            if r.pending:
                result.pending_skipped += 1  # Plaid will bring these in once they post
                continue
            amount = round(r.amount * parsed.sign, 2)
            identity = (r.account, r.mask, r.date.isoformat(), f"{amount:.2f}", r.name)
            seen[identity] += 1  # identical rows in one file are separate transactions
            import_key = hashlib.sha1("|".join(identity + (str(seen[identity]),)).encode()).hexdigest()
            # NOTE: checked before looking up its account and category, which would bring back any renamed since
            if conn.execute("SELECT 1 FROM transactions WHERE import_key = ?", (import_key,)).fetchone():
                result.duplicates += 1
                continue
            key = (r.account, r.mask)
            if key not in accounts:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE name = ? AND coalesce(mask, '') = ?", key
                ).fetchone()
                if row:
                    accounts[key] = row["id"]
                else:
                    accounts[key] = conn.execute(
                        "INSERT INTO accounts (name, type, mask, source) VALUES (?, ?, ?, 'copilot')",
                        (r.account, _guess_account_type(r.account), r.mask or None),
                    ).lastrowid
                    result.accounts_created += 1
                    result.accounts.append(r.account + (f" ••{r.mask}" if r.mask else ""))
            cat_key = (r.parent_category, r.category)
            if cat_key not in categories:
                parent = get_or_create_category(conn, r.parent_category) if r.parent_category else None
                categories[cat_key] = get_or_create_category(conn, r.category, parent) if r.category else None
            cur = conn.execute(
                """INSERT OR IGNORE INTO transactions (account_id, date, amount, name, category_id, type, excluded,
                                                      notes, reviewed, source, import_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'copilot', ?)""",
                (accounts[key], r.date.isoformat(), amount, r.name, categories[cat_key], r.type,
                 int(r.excluded and r.type == "regular"), r.note or None, import_key),
            )
            if cur.rowcount:
                result.added += 1
                if r.recurring:
                    recurring_ids[r.recurring].append(cur.lastrowid)
            else:
                result.duplicates += 1
        result.categories_created = conn.execute("SELECT count(*) FROM categories").fetchone()[0] - categories_before
        result.rules_created = _rules_from_history(conn)
        result.recurring_created = _recurring_from_history(conn, recurring_ids)
        auto_map_plaid_categories(conn)
        set_meta(conn, "copilot_imported_at", datetime.now(UTC).isoformat(timespec="seconds"))
    return result
