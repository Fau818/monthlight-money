"""The web app: pages, form handlers, and the background sync thread."""

import calendar
import csv
import hashlib
import io
import json
import logging
import math
import os
import sqlite3
import threading
import zlib
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import backup, categorize, charts, copilot_import, links, looks, names, recurring, reports, splits, sync, tags
from .config import Settings, load_settings
from .db import connect, get_meta, set_meta
from .plaid_client import PlaidError

log = logging.getLogger("monthlight")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
# (path, label, icon). The main pages are also the tab bar on a phone; the others sit under More there, Reports first.
NAV = [
    ("/", "Dashboard", "home"), ("/transactions", "Transactions", "list"), ("/categories", "Categories", "pie"),
    ("/recurring", "Recurring", "repeat"), ("/accounts", "Accounts", "bank"),
]
REPORTS_NAV = ("/reports", "Reports", "chart")
SIDE_NAV = NAV[:2] + [REPORTS_NAV] + NAV[2:]
MORE_NAV = [("/rules", "Rules & tags", "tag"), ("/import", "Import", "download"), ("/settings", "Settings", "sliders")]
FOLDED_COOKIE = "sidebar_folded"  # the account groups folded in the sidebar, by kind: "credit,loan" (app.js sets it)
THEME_COOKIE = "theme"  # "light" or "dark" when picked (POST /theme, or app.js); else the system's
PAGE_SIZE = 200
# How long before a bank's access through Plaid expires to start asking for a fresh login. Many US banks ask
# for one every 12 months.
CONSENT_WARNING = timedelta(days=14)


# ---- template helpers -------------------------------------------------------

def money(value: float | None) -> str:
    """Returns a balance-style amount, e.g. "$1,234.56" or "-$12.00"; "—" when unknown."""
    if value is None:
        return "—"
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def amount(value: float) -> str:
    """Returns a transaction amount the way Copilot shows it: spending plain, money in with a "+"."""
    return f"{'+' if value > 0 else ''}${abs(value):,.2f}"


def amount_html(value: float) -> Markup:
    """Returns a transaction amount as `amount` writes it, with a small "$", as Copilot lists them."""
    return Markup(f'{"+" if value > 0 else ""}<span class="cur">$</span>{abs(value):,.2f}')


def ago(value: str | None) -> str:
    """Returns how long ago a stored UTC timestamp was, e.g. "5 min ago"; "never" when empty."""
    when = sync.parse_utc(value)
    if when is None:
        return "never"
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def money_parts(value: float | None) -> tuple[str, str]:
    """Returns a large amount in two parts for display, dollars and cents: ("$1,234", ".56"), or ("-$12", ".00")."""
    text = money(value)
    return (text, "") if value is None else (text[:-3], text[-3:])


def month_label(month: str) -> str:
    """Returns "October 2026" for "2026-10"."""
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def month_name(month: str) -> str:
    """Returns "October" for "2026-10"."""
    return datetime.strptime(month, "%Y-%m").strftime("%B")


def short_date(value: date | str | None) -> str:
    """Returns a date like "Oct 18"; "—" when there's none."""
    if not value:
        return "—"
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day:%b} {day.day}"


def day_label(iso: str, today: date | None = None) -> str:
    """Returns "Today", "Yesterday", or the date like "Mon, Oct 12" (with the year when it isn't this year)."""
    day, today = date.fromisoformat(iso), today or date.today()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    text = f"{day:%a}, {day:%b} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def days_until(value: date, today: date | None = None) -> str:
    """Returns when `value` is from today, e.g. "today", "tomorrow", "in 5 days", "3 days ago"."""
    days = (value - (today or date.today())).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


def percent(part: float | None, whole: float | None) -> int:
    """Returns part/whole as a whole percentage, capped at 100; 0 when there's no whole."""
    if not whole or not part or whole <= 0:
        return 0
    return max(0, min(100, round(part / whole * 100)))


def plaid_label(code: str) -> str:
    """Returns a readable name for a Plaid category code, e.g. "Food and drink › Coffee"."""
    primary = categorize.plaid_primary(code) or code
    words = lambda text: text.replace("_", " ").capitalize()
    detail = code[len(primary) + 1:]
    return f"{words(primary)} › {words(detail)}" if detail else words(primary)


def plural(count: int, word: str, many: str | None = None) -> str:
    """Returns e.g. "1 transaction" or "3 transactions"."""
    return f"{count:,} {word if count == 1 else (many or word + 's')}"


templates.env.filters.update(
    money=money, amount=amount, amount_html=amount_html, ago=ago, month_label=month_label, month_name=month_name,
    plaid_label=plaid_label, money_parts=money_parts, short_date=short_date, day_label=day_label, days_until=days_until,
    compact_money=charts.compact_money,
)
# Part of the script and stylesheet URLs, so browsers fetch new versions instead of using a cached old one.
STATIC_VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((HERE / "static").iterdir()) if p.is_file())).hexdigest()[:12]
templates.env.globals.update(
    percent=percent, plural=plural, nav=NAV, side_nav=SIDE_NAV, more_nav=MORE_NAV, phone_more=[REPORTS_NAV] + MORE_NAV,
    static_version=STATIC_VERSION,
    transaction_look=looks.transaction_look, colors=looks.COLORS,
)


# ---- request helpers --------------------------------------------------------

def get_db(request: Request):
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def safe_back(url: str | None, default: str = "/") -> str:
    """Returns `url` if it's a path on this site, otherwise `default` (so redirects can't leave the app)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


def redirect(url: str, message: str | None = None, **extra) -> RedirectResponse:
    """Returns a redirect to `url` that shows `message` on the next page."""
    response = RedirectResponse(url, status_code=303)
    if message:
        set_flash(response, message, **extra)
    return response


def set_flash(response: Response, message: str, **extra) -> None:
    response.set_cookie("flash", quote(json.dumps({"message": message, **extra})), max_age=120, httponly=True, samesite="strict")


def render(request: Request, conn, template: str, **context) -> Response:
    """Returns the page `template` filled with `context` plus what every page needs."""
    raw = request.cookies.get("flash")
    flash = None
    if raw:
        try:
            flash = json.loads(unquote(raw))
        except ValueError:
            pass
    settings: Settings = request.app.state.settings
    # Plaid's consent_expiration_time is ISO 8601 in UTC, so comparing the text compares the times.
    consent_cutoff = (datetime.now(UTC) + CONSENT_WARNING).isoformat(timespec="seconds")
    problems = conn.execute(
        """SELECT item_id, institution_name, status, error, env, consent_expires_at FROM plaid_items
           WHERE status != 'ok' OR env != ? OR consent_expires_at < ?""",
        (settings.plaid_env, consent_cutoff),
    ).fetchall()
    response = templates.TemplateResponse(request, template, {
        "flash": flash, "settings": settings, "problems": problems, "consent_cutoff": consent_cutoff,
        "here": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        "looks": looks.category_looks(conn), "tag_looks": tags.tag_looks(conn),
        "review_count": conn.execute(f"SELECT count(*) FROM transactions t WHERE t.reviewed = 0 OR {reports.SPLIT_OFF}").fetchone()[0],
        "last_synced": conn.execute("SELECT max(last_synced_at) FROM plaid_items").fetchone()[0],
        "side_groups": sidebar_groups(conn, set((request.cookies.get(FOLDED_COOKIE) or "").split(","))),
        "theme": request.cookies.get(THEME_COOKIE) if request.cookies.get(THEME_COOKIE) in ("light", "dark") else "",
        "account_labels": account_labels(conn),
        **context,
    })
    if raw:
        response.delete_cookie("flash")
    return response


def category_options(conn) -> list[dict]:
    """Returns every category as {"id", "label", "emoji"} for dropdowns, subcategories labeled "Group › Name"."""
    rows = conn.execute("SELECT c.id, c.name, p.name AS parent FROM categories c LEFT JOIN categories p ON p.id = c.parent_id").fetchall()
    category_looks = looks.category_looks(conn)
    options = [{"id": r["id"], "label": f"{r['parent']} › {r['name']}" if r["parent"] else r["name"],
                "emoji": category_looks[r["id"]]["emoji"]} for r in rows]
    return sorted(options, key=lambda o: o["label"].lower())


def optional_int(value: str | None) -> int | None:
    """Returns the id typed in `value`; None when it's empty, "none", or anything else that isn't digits."""
    text = "" if value is None else str(value).strip()
    return int(text) if text.isdigit() else None


def optional_money(value: str | None) -> float | None:
    """Returns the amount typed in `value`, ignoring "$" and ","; None when it's empty. Raises ValueError when it isn't
    a finite number."""
    value = (value or "").strip().replace("$", "").replace(",", "")
    if not value:
        return None
    number = float(value)
    if not math.isfinite(number):  # NOTE: float() takes "inf" and "nan", which would break every total they're in
        raise ValueError(value)
    return round(number, 2)


def amount_range(text: str) -> tuple[float | None, float | None] | None:
    """Returns the (lowest, highest) amount typed into the amount filter, either one None when it's open-ended, or None
    when it can't be read. It takes "12.50" (that amount), "10-20" (a range), ">100" or "<50"; signs, "$" and ","
    don't matter, since the filter matches money in and out alike."""
    text = text.strip().replace("$", "").replace(",", "").replace(" ", "").replace("–", "-").replace("—", "-").replace("..", "-")
    try:
        if text[:1] in (">", "≥"):
            low, high = abs(float(text.lstrip(">=≥"))), None
        elif text[:1] in ("<", "≤"):
            low, high = None, abs(float(text.lstrip("<=≤")))
        elif "-" in text.lstrip("+-"):
            first, second = text.lstrip("+-").split("-", 1)
            low, high = sorted((abs(float(first)), abs(float(second))))
        else:
            low = high = abs(float(text))
    except (ValueError, IndexError):
        return None
    if any(v is not None and not math.isfinite(v) for v in (low, high)):
        return None
    return low, high


# The transaction list's filters, in the order its address holds them (list_filters); those in MANY can hold several.
LIST_FILTERS = ("q", "month", "period", "from", "to", "account", "category", "type", "tag", "amount", "review", "transfers", "sort")
MANY = ("account", "category", "type", "tag")
# The filter menu's spans of time, each of whole months counted from today (period_range); "custom" takes from/to.
PERIODS = {"this-month": "This month", "last-month": "Last month", "last-3-months": "Last 3 months",
           "last-12-months": "Last 12 months", "this-year": "This year", "last-year": "Last year"}
AMOUNT_CHOICES = [("", "Any amount"), ("eq", "Equals"), ("between", "Between"), ("over", "More than"), ("under", "Less than")]


def iso_day(text: str | None) -> str:
    """Returns `text` as a day like 2026-10-14, or "" when it isn't one."""
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return ""


def period_range(period: str, today: date) -> tuple[str, str]:
    """Returns the first day of `period` (one of PERIODS) and the day after its last, as ISO dates."""
    month = reports.current_month(today)
    if period.endswith("-year"):
        year = today.year - (period == "last-year")
        return f"{year}-01-01", f"{year + 1}-01-01"
    first, last = {"this-month": (0, 0), "last-month": (-1, -1), "last-3-months": (-2, 0), "last-12-months": (-11, 0)}[period]
    return reports.month_range(reports.shift_month(month, first))[0], reports.month_range(reports.shift_month(month, last))[1]


def amount_text(choice: str, first: str, second: str = "") -> str:
    """Returns what the amount filter holds for the filter menu's `choice` (see AMOUNT_CHOICES) and amounts: "12.50",
    "10-20", ">100" or "<50"; "" without a choice or an amount."""
    first, second = first.strip(), second.strip()
    if choice == "between" and first and second:
        return f"{first}-{second}"
    if not first or choice not in ("eq", "between", "over", "under"):
        return ""
    return {"over": ">", "under": "<"}.get(choice, "") + first


def amount_choice(text: str) -> tuple[str, str, str]:
    """Returns the filter menu's choice and its one or two amounts (amount_text, the other way) for what the amount
    filter holds; an amount it can't read stays as it was typed, as "Equals"."""
    found = amount_range(text) if text else None
    if not found:
        return ("eq", text, "") if text else ("", "", "")
    show = lambda v: f"{v:.2f}".removesuffix(".00")
    low, high = found
    if low == high:
        return "eq", show(low), ""
    if low is None or high is None:
        return ("under", show(high), "") if low is None else ("over", show(low), "")
    return "between", show(low), show(high)


def list_filters(query: list[tuple[str, str]]) -> dict:
    """Returns the transaction list's filters from its address's `query` (name, value pairs), each checked: for each of
    LIST_FILTERS, a list (MANY) or text, empty when it isn't set or can't be read. The filter menu's amount fields
    (amount_op and two amounts) make the amount, and its period can hold a month (YYYY-MM)."""
    last = lambda name: next((v.strip() for k, v in reversed(query) if k == name and v.strip()), "")
    every = lambda name, ok: list(dict.fromkeys(v.strip() for k, v in query if k == name and ok(v.strip())))
    period = last("period")
    month = reports.valid_month(period) or reports.valid_month(last("month")) or ""
    period = period if period in PERIODS or period == "custom" else ""
    start, end = iso_day(last("from")), iso_day(last("to"))
    if any(k == "amount_op" for k, _ in query):
        amount = amount_text(last("amount_op"), last("amount_min"), last("amount_max"))
    else:
        amount = last("amount")
    return {
        "q": last("q"), "month": "" if period else month, "period": period,
        "from": start if period == "custom" else "", "to": end if period == "custom" else "",
        "account": every("account", str.isdigit),
        "category": every("category", lambda v: v.isdigit() or v in ("none", "excluded")),
        "type": every("type", lambda v: v in categorize.TYPES), "tag": every("tag", str.isdigit), "amount": amount,
        "review": last("review") if last("review") in ("1", "done") else "", "transfers": "1" if last("transfers") == "1" else "",
        "sort": "amount" if last("sort") == "amount" else "",
    }


def filter_pairs(filters: dict) -> list[tuple[str, str]]:
    """Returns the filters that are set as the (name, value) pairs of the list's address, in LIST_FILTERS order."""
    return [(k, v) for k in LIST_FILTERS for v in (filters[k] if k in MANY else [filters[k]]) if v]


def list_criteria(filters: dict, today: date) -> dict:
    """Returns transaction_filter's criteria for the list's `filters` (list_filters)."""
    if filters["month"]:
        dates = reports.month_range(filters["month"])
    elif filters["period"] == "custom":
        # NOTE: picked, its days aren't typed yet: nothing to leave out until they are
        after = (date.fromisoformat(filters["to"]) + timedelta(days=1)).isoformat() if filters["to"] else "9999-12-31"
        dates = (filters["from"] or "0001-01-01", after) if filters["from"] or filters["to"] else None
    else:
        dates = period_range(filters["period"], today) if filters["period"] else None
    return {"dates": dates, "accounts": [int(a) for a in filters["account"]], "categories": filters["category"],
            "types": filters["type"], "tags": [int(g) for g in filters["tag"]], "q": filters["q"] or None,
            "amount": amount_range(filters["amount"]) if filters["amount"] else None, "review": filters["review"]}


def filter_chips(conn, filters: dict, categories: list[dict], link) -> list[dict]:
    """Returns the filters that are set as chips: {"label", "remove"} for each, where `remove` is the list's address
    without it (`link`, the list's address with some filters changed)."""
    accounts, tag_names = account_labels(conn), {str(g["id"]): g["name"] for g in tags.tag_looks(conn).values()}
    names = {"none": "Uncategorized", "excluded": "Excluded", **{str(c["id"]): c["label"].split(" › ")[-1] for c in categories}}
    chips = []
    if filters["q"]:
        chips.append({"label": f"“{filters['q']}”", "remove": link(q="")})
    if filters["month"]:
        chips.append({"label": month_label(filters["month"]), "remove": link(month="")})
    elif filters["period"] == "custom" and (filters["from"] or filters["to"]):
        start, end = (short_date(d) if d else "" for d in (filters["from"], filters["to"]))
        chips.append({"label": f"{start} – {end}" if start and end else f"From {start}" if start else f"Until {end}",
                      "remove": link(period="", **{"from": "", "to": ""})})
    elif filters["period"] in PERIODS:
        chips.append({"label": PERIODS[filters["period"]], "remove": link(period="")})
    for key, label in (("account", lambda v: accounts.get(int(v), "An account")), ("category", lambda v: names.get(v, "A category")),
                       ("tag", lambda v: tag_names.get(v, "A tag")), ("type", str.capitalize)):
        if filters[key]:
            chips.append({"label": ", ".join(label(v) for v in filters[key]), "remove": link(**{key: []})})
    if filters["amount"]:
        choice, first, second = amount_choice(filters["amount"])
        amounts = {"eq": f"${first}", "between": f"${first} – ${second}", "over": f"Over ${first}", "under": f"Under ${first}"}
        label = amounts[choice] if amount_range(filters["amount"]) else f"“{filters['amount']}”"  # NOTE: unreadable, it leaves nothing out
        chips.append({"label": label, "remove": link(amount="")})
    if filters["review"] == "done":
        chips.append({"label": "Reviewed", "remove": link(review="")})
    if filters["transfers"]:
        chips.append({"label": "With moves between accounts", "remove": link(transfers="")})
    return chips


# A report's filters, in the order its address holds them (report_filters); those in REPORT_MANY can hold several. Each
# "not_…" leaves something out of the report alone (reports.leave_out_sql), and "everyday" what isn't everyday spending.
REPORT_FILTERS = ("period", "month", "from", "to", "by", "show", "group", "not_category", "not_account", "not_tag",
                  "not_merchant", "not_id", "over", "everyday", "saved")
REPORT_MANY = ("not_category", "not_account", "not_tag", "not_merchant", "not_id")
REPORT_DEFAULTS = {"period": "last-12-months", "by": "month", "show": "spending", "group": "category"}
REPORT_BY = {"month": "By month", "quarter": "By quarter", "year": "By year"}
REPORT_SHOWS = {"spending": "Spending", "income": "Income"}
REPORT_GROUPS = {"category": "By category", "merchant": "By merchant", "account": "By account", "tag": "By tag"}
# The hues a report gives merchants and accounts, which have no color of their own: the data-viz palette's, in its order,
# but red, kept for "over budget". "Other" is gray.
SERIES_HUES = ("blue", "orange", "aqua", "yellow", "magenta", "green", "violet")


def report_filters(query: list[tuple[str, str]]) -> dict:
    """Returns a report's filters from its address's `query` (name, value pairs), each checked, with REPORT_DEFAULTS for
    what isn't set: a period (PERIODS, "custom" with from and to, or "" for one month), how to add it up ("by"), what to
    show and group by, the leave-outs (lists, and "over", an amount), "everyday" and "saved" (a saved report's id)."""
    last = lambda name: next((v.strip() for k, v in reversed(query) if k == name and v.strip()), "")
    every = lambda name, ok: list(dict.fromkeys(v.strip() for k, v in query if k == name and ok(v.strip())))
    period = last("period")
    month = reports.valid_month(period) or reports.valid_month(last("month")) or ""
    period = "" if month else period if period in PERIODS or period == "custom" else REPORT_DEFAULTS["period"]
    try:
        over = optional_money(last("over"))
    except ValueError:
        over = None
    pick = lambda name, choices: last(name) if last(name) in choices else REPORT_DEFAULTS[name]
    return {
        "period": period, "month": month,
        "from": iso_day(last("from")) if period == "custom" else "", "to": iso_day(last("to")) if period == "custom" else "",
        "by": pick("by", REPORT_BY), "show": pick("show", REPORT_SHOWS), "group": pick("group", REPORT_GROUPS),
        "not_category": every("not_category", lambda v: v.isdigit() or v == "none"), "not_account": every("not_account", str.isdigit),
        "not_tag": every("not_tag", str.isdigit), "not_merchant": every("not_merchant", bool), "not_id": every("not_id", str.isdigit),
        "over": f"{over:.2f}".removesuffix(".00") if over and over > 0 else "",
        "everyday": "1" if last("everyday") == "1" else "", "saved": last("saved") if last("saved").isdigit() else "",
    }


def report_pairs(filters: dict, saved: bool = True) -> list[tuple[str, str]]:
    """Returns a report's filters that differ from REPORT_DEFAULTS as the (name, value) pairs of its address, in
    REPORT_FILTERS order; without `saved`, leaving out which saved report it is."""
    return [(k, v) for k in REPORT_FILTERS if saved or k != "saved"
            for v in (filters[k] if k in REPORT_MANY else [filters[k]]) if v and v != REPORT_DEFAULTS.get(k)]


def report_leave_outs(conn, filters: dict) -> dict:
    """Returns what a report leaves out (reports.leave_out_sql), from its `filters`: its own leave-outs, and with
    "everyday", what's set aside from everyday spending too."""
    own = {"category": filters["not_category"], "account": [int(a) for a in filters["not_account"]],
           "tag": [int(g) for g in filters["not_tag"]], "merchant": filters["not_merchant"], "id": [int(i) for i in filters["not_id"]],
           "over": float(filters["over"]) if filters["over"] else None}
    return reports.merge_leave_outs(own, reports.everyday(conn) if filters["everyday"] else {})


def month_list(first: str, last: str) -> list[str]:
    """Returns the months from `first` to `last` (YYYY-MM), both included."""
    months = [first]
    while months[-1] < last:
        months.append(reports.shift_month(months[-1], 1))
    return months


def span_dates(start: str, end: str) -> str:
    """Returns the days from `start` to the day before `end` in words: "Nov 2025 – Oct 2026" for whole months, "October
    2026" for one, else like "Mar 15 – Jun 14, 2026"."""
    last = date.fromisoformat(end) - timedelta(days=1)
    first = date.fromisoformat(start)
    if start.endswith("-01") and end.endswith("-01"):
        if start[:7] == last.isoformat()[:7]:
            return month_label(start[:7])
        return f"{first:%b %Y} – {last:%b %Y}"
    if first.year == last.year:
        return f"{short_date(first)} – {short_date(last)}, {last.year}"
    return f"{short_date(first)}, {first.year} – {short_date(last)}, {last.year}"


def report_span(conn, filters: dict, today: date) -> dict:
    """Returns the days a report covers: {"start", "end" (the day after its last), "months" (each it touches, oldest
    first), "whole" (whether it's whole months), "label" (like "Last 12 months"), "dates" (span_dates)}. The year so far
    ends with this month, whole, as the dashboard counts it; days of its own without a start start with the history."""
    if filters["month"]:
        start, end = reports.month_range(filters["month"])
        label = month_label(filters["month"])
    elif filters["period"] == "custom":
        first = conn.execute("SELECT min(date) FROM transactions").fetchone()[0] or today.isoformat()
        days = sorted([filters["from"] or first, filters["to"] or today.isoformat()])
        start, end, label = days[0], (date.fromisoformat(days[1]) + timedelta(days=1)).isoformat(), "Dates of your own"
    else:
        start, end = period_range(filters["period"], today)
        if filters["period"] == "this-year":
            end = reports.month_range(reports.current_month(today))[1]
        label = PERIODS[filters["period"]]
    last = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
    return {"start": start, "end": end, "months": month_list(start[:7], last[:7]), "label": label,
            "whole": start.endswith("-01") and end.endswith("-01"), "dates": span_dates(start, end)}


def shift_days(day: str, months: int = 0, days: int = 0) -> str:
    """Returns the ISO day `months` months and `days` days after `day` (the month's last day when it's shorter)."""
    moved = recurring.add_months(date.fromisoformat(day), months) if months else date.fromisoformat(day)
    return (moved + timedelta(days=days)).isoformat()


def earlier_spans(span: dict, filters: dict) -> list[dict]:
    """Returns the spans a report compares itself with, as {"start", "end", "label"}: the one just before it, as long,
    and the same days a year earlier; only the latter for the year so far, and only one when they're the same."""
    count = len(span["months"])
    if span["whole"]:
        before = {"start": shift_days(span["start"], -count), "end": span["start"],
                  "label": "the month before" if count == 1 else f"the {count} months before"}
    else:
        length = (date.fromisoformat(span["end"]) - date.fromisoformat(span["start"])).days
        before = {"start": shift_days(span["start"], days=-length), "end": span["start"], "label": f"the {length} days before"}
    year = {"start": shift_days(span["start"], -12), "end": shift_days(span["end"], -12), "label": "a year earlier"}
    if span["whole"] and count == 1:
        year["label"] = month_label(year["start"][:7])
    if filters["period"] == "this-year":
        return [{**year, "label": f"the same months of {year['start'][:4]}"}]
    return [before] if (before["start"], before["end"]) == (year["start"], year["end"]) else [before, year]


def change(now: float, then: float) -> dict | None:
    """Returns how `now` compares with `then`: {"diff", "percent" (None when `then` was 0)}; None when both are 0."""
    if not now and not then:
        return None
    diff = round(now - then, 2)
    return {"diff": diff, "percent": round(diff / then * 100) if then > 0 else None}


def report_chips(conn, filters: dict, link) -> list[dict]:
    """Returns what a report leaves out as chips: {"label", "remove"} for each, where `remove` is the report's address
    without it (`link`, its address with some filters changed)."""
    names = {"none": "Uncategorized", **{str(r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM categories")}}
    accounts, tag_names = account_labels(conn), {str(g["id"]): g["name"] for g in tags.tag_looks(conn).values()}
    picked = {str(r["id"]): f"{r['name']}, {short_date(r['date'])}" for r in conn.execute(
        f"SELECT id, name, date FROM transactions WHERE id IN ({','.join('?' * len(filters['not_id']))})", filters["not_id"])}
    chips = []
    for key, label in (("not_category", lambda v: names.get(v, "a category")), ("not_account", lambda v: accounts.get(int(v), "an account")),
                       ("not_tag", lambda v: f"tagged {tag_names.get(v, 'with a tag')}"), ("not_merchant", str),
                       ("not_id", lambda v: picked.get(v, "a transaction"))):
        for value in filters[key]:
            chips.append({"label": f"Not {label(value)}", "remove": link(**{key: [v for v in filters[key] if v != value]})})
    if filters["over"]:
        chips.append({"label": f"Nothing over ${filters['over']}", "remove": link(over="")})
    if filters["everyday"]:
        chips.append({"label": "Everyday spending only", "remove": link(everyday="")})
    return chips


def filter_choices(conn, counted: bool = False) -> dict:
    """Returns what the filter menus offer to pick, each as (value, label, color): "accounts" (only those that count,
    when `counted`), "categories" (with their emoji, "Group › Name" for a subcategory) and "tags"."""
    labels = account_labels(conn)
    rows = conn.execute(f"SELECT id FROM accounts {'WHERE hidden = 0' if counted else ''} ORDER BY hidden, archived, name COLLATE NOCASE")
    return {"accounts": [(str(r["id"]), labels[r["id"]], None) for r in rows],
            "categories": [(str(o["id"]), f"{o['emoji']} {o['label']}", None) for o in category_options(conn)],
            "tags": [(str(g["id"]), g["name"], g["color"]) for g in tags.tag_looks(conn).values()]}


# A category's or a tag's own color as a chart draws it: one of the data-viz palette's (charts.HUES), else the nearest.
CHART_HUE = {"lime": "green", "teal": "aqua", "sky": "blue", "purple": "violet", "brown": "orange"}


def chart_colors(wanted: list[str | None], other: bool) -> list[str]:
    """Returns a color of charts.HUES for each of a chart's series, in order, from the color each `wanted` (None for
    none of its own), each color once:
    - a series' own color, when it's one of the palette's and free
    - else the nearest of them (CHART_HUE), when that's free
    - else the next free one; gray is taken when the chart has "Other" (`other`), and red never given"""
    taken = {"gray"} if other else set()
    out: list[str | None] = [None] * len(wanted)
    for i, color in enumerate(wanted):  # a series' own color first, when it's one of the palette's
        if color in charts.HUES["light"] and color not in taken:
            out[i] = color
            taken.add(color)
    for i, color in enumerate(wanted):  # then the nearest of them, else the next free one
        if out[i] is None:
            near = CHART_HUE.get(color or "")
            out[i] = near if near and near not in taken else next((c for c in SERIES_HUES if c not in taken), "gray")
            taken.add(out[i])
    return out


def csv_text(value: str | None) -> str:
    """Returns `value` for a cell of the CSV export, "" for none. Text that a spreadsheet would run as a formula
    (it starts with =, +, -, @, a tab or a return) gets a ' in front, so it shows as text."""
    value = value or ""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value

def is_remote(request: Request, settings: Settings) -> bool:
    """Returns whether the request came from another device, through one of settings.remote_hosts."""
    host = (request.url.hostname or "").lower()
    return any(host == p or (p.startswith("*.") and host.endswith(p[1:])) for p in settings.remote_hosts)


def sync_in_background(settings: Settings, item_ids: list[str] | None = None) -> None:
    threading.Thread(target=sync.run_sync, args=(settings, item_ids), daemon=True).start()


# ---- app --------------------------------------------------------------------

def background_work(settings: Settings) -> None:
    """Does one round of the background work:
    syncs the banks that are due, unless automatic syncing is off;
    then saves today's backup, if there isn't one yet."""
    if settings.sync_interval_hours > 0:
        try:
            conn = connect(settings.db_path)
            try:
                due = sync.due_items(conn, settings)
            finally:
                conn.close()
            if due:
                sync.run_sync(settings, due)
        except Exception:
            log.exception("Background sync failed")
    try:
        made = backup.make_daily_backup(settings.db_path, settings.backup_dir, date.today())
        if made:
            log.info("Saved backup %s", made.name)
    except Exception:
        log.exception("Daily backup failed")


def _scheduler(settings: Settings, stop: threading.Event) -> None:
    """Runs the background work once a minute, until `stop` is set."""
    while not stop.wait(60):
        background_work(settings)


def create_app(settings: Settings) -> FastAPI:
    """Returns the web app configured with `settings`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            connect(settings.db_path).close()  # create the database before the first request
        except sqlite3.OperationalError:
            # NOTE: in Docker the app runs as user ID 1000, which can't write to a data folder another user owns
            log.error("Can't open the database at %s: make sure user ID %s can write to that folder.", settings.db_path, os.getuid())
            raise
        stop = threading.Event()
        threading.Thread(target=_scheduler, args=(settings, stop), daemon=True).start()
        yield
        stop.set()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    # Only answer to localhost names and those in REMOTE_HOSTS (your other devices', through a private network), so
    # another website can't reach this app through DNS tricks.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts + settings.remote_hosts)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # Browsers label cross-site form posts and fetches with an Origin header; refuse those.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Blocked a request from another website.", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def no_framing(request: Request, call_next):
        # Other websites can't show these pages in a frame, where a hidden click could change something.
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    register_routes(app)
    return app


def register_routes(app: FastAPI) -> None:
    settings: Settings = app.state.settings

    # ---- the app's icon, where browsers and phones look for it ----

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(HERE / "static" / "favicon.ico")

    @app.get("/apple-touch-icon.png")
    def home_screen_icon():
        return FileResponse(HERE / "static" / "apple-touch-icon.png")

    # ---- dashboard ----

    @app.get("/")
    def dashboard(request: Request, month: str | None = None, conn=Depends(get_db)):
        today = date.today()
        month = reports.valid_month(month) or reports.current_month(today)
        nodes = reports.category_spending(conn, month)
        budget = reports.budget_total(nodes)
        pace = reports.spending_pace(conn, month, today)
        review, to_review = find_transactions(conn, review=True, limit=5)
        history = reports.net_worth_history(conn)
        aside = reports.everyday(conn)
        has_everyday = any(aside[k] for k in reports.LEAVE_OUTS) or aside["over"] is not None
        everyday_shown = has_everyday and get_meta(conn, "everyday_shown") == "1"
        return render(
            request, conn, "dashboard.html",
            month=month, prev_month=reports.shift_month(month, -1), next_month=reports.shift_month(month, 1),
            this_month=reports.current_month(today), totals=reports.month_totals(conn, month),
            nodes=nodes, budget=budget, pace=pace, pace_chart=pace_chart(pace, month, budget),
            months=months_chart(reports.monthly_series(conn, 12, month), month,
                                reports.monthly_series(conn, 12, month, aside) if everyday_shown else None),
            everyday_shown=everyday_shown, has_everyday=has_everyday, pinned=pinned_reports(conn, today), today=today,
            review_offer=reports.shift_month(month, -1) if month == reports.current_month(today) and today.day <= 7 else None,
            worth=reports.net_worth(conn), worth_chart=worth_chart(history[-90:]),
            upcoming=recurring.upcoming(conn, 30, today)[:5], review=review, to_review=to_review,
            categories=category_options(conn),
            has_data=conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None,
        )

    # ---- a month in review ----

    @app.get("/review")
    def latest_review():
        """The review of the month before this one."""
        return RedirectResponse(f"/review/{reports.shift_month(reports.current_month(date.today()), -1)}", status_code=303)

    @app.get("/review/{month}")
    def month_review(request: Request, month: str, conn=Depends(get_db)):
        """A month in review (review_view): how it went, what changed from the month before, and how net worth moved."""
        if reports.valid_month(month) is None:
            raise HTTPException(404, "There's no such month.")
        return render(request, conn, "review.html", **review_view(conn, month, date.today()))

    # ---- transactions ----

    @app.get("/transactions")
    def transactions(request: Request, conn=Depends(get_db)):
        """The list, with the filters, the sort and the page's place in the list read from the address (list_filters).
        An address that holds them otherwise (as the filter menu sends them, say) leads to the one that holds them so."""
        query = request.query_params
        filters = list_filters(query.multi_items())
        place = {k: v for k in ("oldest", "upto") if (v := query.get(k, "").strip())}
        pairs = filter_pairs(filters) + list(place.items())
        if sorted(query.multi_items()) != sorted(pairs):
            return RedirectResponse("/transactions" + (f"?{urlencode(pairs)}" if pairs else ""), status_code=303)
        today = date.today()
        criteria = list_criteria(filters, today)
        # Moves between the user's own accounts, with both sides found and linked, are hidden unless asked for. A
        # transfer without its other side shows, since it may be money that went to someone else. The review list
        # shows every new one once; looking for an amount or for transfers shows them all, as the other side may be
        # what's looked for.
        hide = not filters["transfers"] and not filters["type"] and filters["review"] != "1" and not criteria["amount"]
        by_amount = filters["sort"] == "amount"
        months_listed = listed_months(conn, **criteria, hide_transfers=hide)
        total = sum(n for _, n in months_listed)
        older = []
        if by_amount:
            # Biggest first, in one list: PAGE_SIZE more each time the list nears its end (app.js), up to `upto`.
            shown = int(place["upto"]) if place.get("upto", "").isdigit() else PAGE_SIZE
            rows, _ = find_transactions(conn, **criteria, hide_transfers=hide, by_amount=True, limit=max(shown, PAGE_SIZE))
            more = "/transactions?" + urlencode(filter_pairs(filters) + [("upto", len(rows) + PAGE_SIZE)]) if total > len(rows) else ""
            months = []
        else:
            # A few months at a time, newest first, through `oldest` once more were loaded (app.js does that as it
            # scrolls near the end): whole months, as many as it takes to list PAGE_SIZE transactions or more.
            shown = month_chunk(months_listed, months_listed[0][0], reports.valid_month(place.get("oldest"))) if months_listed else []
            rest = [m for m, _ in months_listed if shown and m < shown[-1]]
            older = month_chunk(months_listed, rest[0]) if rest else []
            between = (reports.month_range(shown[-1])[0], reports.month_range(shown[0])[1]) if shown else None
            rows, _ = find_transactions(conn, **criteria, between=between, hide_transfers=hide, limit=-1)
            more = older and "/transactions?" + urlencode(filter_pairs(filters) + [("oldest", older[-1])]) + f"#m{older[0]}"
            # Each month starts with its totals, as on the dashboard, when the list shows all of it.
            whole = not any(filters[k] for k in LIST_FILTERS if k not in ("month", "period", "transfers")) and (
                criteria["dates"] is None or all(day.endswith("-01") for day in criteria["dates"]))
            budget = reports.budget_total(reports.category_spending(conn, reports.current_month(today))) if whole else 0
            months = [dict(m, totals=reports.month_totals(conn, m["month"]) if whole else None, budget=budget,
                           current=m["month"] == reports.current_month(today)) for m in by_month(group_by_day(rows, today))]
        hidden = find_transactions(conn, **criteria, linked_transfers=True, limit=0)[1] if hide else 0
        categories = category_options(conn)

        def link(**changes) -> str:
            """Returns the list's address with these filters changed ("" or [] for none)."""
            pairs = filter_pairs({**filters, **changes})
            return "/transactions" + (f"?{urlencode(pairs)}" if pairs else "")

        chips = filter_chips(conn, filters, categories, link)
        return render(
            request, conn, "transactions.html",
            months=months, rows=rows, by_amount=by_amount, total=total, filters=filters, filter_pairs=filter_pairs(filters),
            chips=chips, menu_count=len(chips) - bool(filters["q"]) + (filters["review"] == "1"),  # the menu's filters in use
            clear_link=link(**{k: [] if k in MANY else "" for k in LIST_FILTERS if k != "sort"}),
            more=more, older_month=older and older[0], totals=listed_totals(conn, **criteria, hide_transfers=hide),
            amount_unreadable=bool(filters["amount"]) and criteria["amount"] is None, amount_choice=amount_choice(filters["amount"]),
            periods=PERIODS, amount_choices=AMOUNT_CHOICES, categories=categories, hidden_transfers=hidden,
            show_transfers_link=link(transfers="1"), hide_transfers_link=link(transfers=""),
            review_link=link(review="1"), all_link=link(review=""),
            choices=filter_choices(conn),
            month_options=[r[0] for r in conn.execute("SELECT DISTINCT substr(date, 1, 7) FROM transactions ORDER BY 1 DESC")],
        )

    # Registered before /transactions/{tid}, which would otherwise take "new" for a transaction id.
    def new_transaction_form(request: Request, conn, values: dict, error: str | None = None, status: int = 200) -> Response:
        # Cash first, then accounts no bank sends transactions for: a hand-made copy of a bank's transaction counts twice.
        # Hidden and archived accounts aren't offered.
        accounts = conn.execute(
            "SELECT id, name, mask, plaid_account_id IS NOT NULL AS connected FROM accounts "
            "WHERE NOT (source = 'manual' AND name = 'Cash') AND hidden = 0 AND archived = 0 ORDER BY connected, name COLLATE NOCASE"
        ).fetchall()
        response = render(request, conn, "transaction_new.html", values=values, error=error, accounts=accounts,
                          categories=category_options(conn), has_cash=cash_account(conn) is not None)
        response.status_code = status
        return response

    @app.get("/transactions/new")
    def new_transaction(request: Request, back: str = "/transactions", conn=Depends(get_db)):
        # The account picked last time (or, before any, the one of the last transaction added by hand), while it's still
        # offered; else Cash.
        last = get_meta(conn, "last_manual_account")
        if last is None:
            row = conn.execute("SELECT account_id FROM transactions WHERE source = 'manual' AND type != 'transfer' ORDER BY id DESC LIMIT 1").fetchone()
            last = "cash" if row is None or row[0] == cash_account(conn) else str(row[0])
        offered = last.isdigit() and conn.execute("SELECT 1 FROM accounts WHERE id = ? AND hidden = 0 AND archived = 0", (int(last),)).fetchone()
        values = {"date": date.today().isoformat(), "direction": "out", "account": last if offered else "cash", "type": "regular",
                  "back": safe_back(back, "/transactions")}
        return new_transaction_form(request, conn, values)

    @app.post("/transactions/new")
    def add_transaction(request: Request, day: str = Form("", alias="date"), name: str = Form(""), amount: str = Form(""),
                        direction: str = Form("out"), account: str = Form("cash"), type: str = Form("regular"),
                        category_id: str = Form(""), notes: str = Form(""), back: str = Form("/transactions"),
                        conn=Depends(get_db)):
        values = {"date": day, "name": name, "amount": amount, "direction": direction, "account": account, "type": type,
                  "category_id": optional_int(category_id), "notes": notes, "back": safe_back(back, "/transactions")}
        try:
            when = date.fromisoformat(day)
            value = optional_money(amount)
        except ValueError:
            return new_transaction_form(request, conn, values, "Check the date and the amount, for example 12.50.", 400)
        if not name.strip() or not value or value <= 0:
            return new_transaction_form(request, conn, values, "Give the transaction a name and an amount above zero.", 400)
        type_ = type if type in categorize.TYPES else "regular"
        with conn:
            if account == "cash":
                account_id = cash_account(conn) or conn.execute(
                    "INSERT INTO accounts (name, type, source) VALUES ('Cash', 'other', 'manual')").lastrowid
            else:
                row = conn.execute("SELECT id FROM accounts WHERE id = ?", (optional_int(account),)).fetchone()
                if row is None:
                    return new_transaction_form(request, conn, values, "Pick an account.", 400)
                account_id = row["id"]
            signed = value if direction == "in" else -value
            # Marked as edited by hand, so rules and syncs leave it as entered.
            conn.execute(
                """INSERT INTO transactions (account_id, date, amount, name, category_id, type, notes, reviewed, user_edited,
                                             source, recurring_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'manual', ?)""",
                (account_id, when.isoformat(), signed, name.strip(), optional_int(category_id) if type_ == "regular" else None,
                 type_, notes.strip() or None, recurring.Matcher(conn).match(name, signed) if type_ != "transfer" else None),
            )
            set_meta(conn, "last_manual_account", "cash" if account == "cash" else str(account_id))  # the next one starts there
        return redirect(f"/transactions?{urlencode({'month': when.strftime('%Y-%m')})}", f"Added “{name.strip()}”.")

    @app.post("/transactions/{tid}/delete")
    def delete_transaction(tid: int, back: str = Form("/transactions"), conn=Depends(get_db)):
        t = get_transaction(conn, tid)
        if t["split_from_id"]:
            return redirect(safe_back(back, "/transactions"),
                            "To remove a part, open the transaction it's split from and empty the part's name and amount.")
        if t["source"] == "group":
            return redirect(safe_back(back, "/transactions"), "To undo combining, open it and press Ungroup.")
        if t["source"] != "manual":
            # A bank's transaction would come back the next time its history is fetched again.
            return redirect(safe_back(back, "/transactions"),
                            "Only transactions you added yourself can be deleted. For a bank's, change its type to Transfer, or exclude it.")
        try:
            with conn:
                splits.unsplit(conn, t)
                links.unlink(conn, t)
                conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
        except ValueError as exc:
            return redirect(safe_back(back, "/transactions"), str(exc))
        return redirect(safe_back(back, "/transactions"), f"Deleted “{t['name']}”.")

    @app.post("/transactions/{tid}/category")
    def set_category(tid: int, category_id: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        t = get_transaction(conn, tid)
        if category_id == "excluded":  # a choice beside the categories, as in Copilot (db.py clears the category)
            with conn:
                conn.execute("UPDATE transactions SET excluded = 1, user_edited = 1, reviewed = 1 WHERE id = ?", (tid,))
            return redirect(safe_back(back, "/transactions"), f"“{t['name']}” is excluded from spending.")
        new_category = optional_int(category_id)
        with conn:
            conn.execute("UPDATE transactions SET category_id = ?, excluded = 0, user_edited = 1, reviewed = 1 WHERE id = ?", (new_category, tid))
            learned = learn_plaid_mapping(conn, t["plaid_category"], new_category)
        name = category_name(conn, new_category)
        message = f"“{t['name']}” is now {name or 'uncategorized'}." + (" Similar new transactions will be too." if learned else "")
        return redirect(safe_back(back, "/transactions"), message, rule_offer=rule_offer(conn, t, new_category))

    @app.post("/transactions/review")
    def mark_reviewed(ids: list[int] = Form([]), back: str = Form("/transactions"), conn=Depends(get_db)):
        with conn:
            conn.executemany("UPDATE transactions SET reviewed = 1 WHERE id = ?", [(i,) for i in ids])
        return redirect(safe_back(back, "/transactions"), f"Marked {plural(len(ids), 'transaction')} as reviewed.")

    @app.get("/transactions/selected")
    def selected_transactions(request: Request, ids: list[int] = Query([]), back: str = "/transactions", conn=Depends(get_db)):
        """The transactions ticked in the list (`ids`), to change together: in the pane beside the list (app.js), or on
        a page of their own."""
        rows = conn.execute(f"""SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id = t.account_id
                                WHERE t.id IN ({','.join('?' * len(ids))}) ORDER BY t.date DESC, t.id DESC""", ids).fetchall() if ids else []
        if not rows:
            return redirect(safe_back(back, "/transactions"), "Tick the transactions to change together.")
        regular = [r for r in rows if r["type"] == "regular"]
        shared = {r["category_id"] for r in regular} | ({"excluded"} if any(r["excluded"] for r in regular) else set())
        tagged = tags.tags_by_transaction(conn, [r["id"] for r in rows])
        here = request.url.path + "?" + request.url.query
        return render(request, conn, "transactions_selected.html", rows=rows, back=safe_back(back, "/transactions"), here=here,
                      own_names=dict(conn.execute("SELECT id, name FROM categories").fetchall()),
                      regular=regular, category=next(iter(shared)) if len(shared) == 1 else "mixed", categories=category_options(conn),
                      tag_counts=Counter(g for r in rows for g in tagged.get(r["id"], [])),
                      money_out=round(-sum(r["amount"] for r in rows if r["amount"] < 0), 2),
                      money_in=round(sum(r["amount"] for r in rows if r["amount"] > 0), 2))

    @app.post("/transactions/bulk")
    def bulk_edit(ids: list[int] = Form([]), action: str = Form(""), tag_id: str = Form(""), new_tag: str = Form(""),
                  category_id: str = Form(""), name: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Does `action` to the transactions selected in the list (`ids`): tag (tag_id, or a new tag named new_tag) or
        untag (tag_id), category (category_id, where "excluded" excludes them), review or unreview, exclude, include,
        link or unlink (links.py), or combine (into one named `name`, see splits.combine)."""
        back = safe_back(back, "/transactions")
        rows = conn.execute(f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(ids))}) ORDER BY date, id", ids).fetchall() if ids else []
        if not rows:
            return redirect(back, "Select some transactions first.")
        count = lambda n, what="transaction": plural(n, what)
        # Category and exclusion apply to regular transactions only; income and transfers take neither.
        regular = [r for r in rows if r["type"] == "regular"]
        skipped = f" {count(len(rows) - len(regular))} left as they were: income and transfers take neither." if len(regular) < len(rows) else ""
        with conn:
            if action in ("tag", "untag"):
                typed = tags.parse_names(new_tag) if action == "tag" and not tag_id.isdigit() else []
                tag_id = str(tags.get_or_create(conn, typed[0])) if typed else tag_id
                tag = conn.execute("SELECT id, name FROM tags WHERE id = ?", (optional_int(tag_id),)).fetchone() if tag_id.isdigit() else None
                if tag is None:
                    return redirect(back, "Pick a tag first.")
                if action == "tag":
                    tags.add(conn, tag["id"], [r["id"] for r in rows])
                    return redirect(back, f"Tagged {count(len(rows))} “{tag['name']}”.")
                return redirect(back, f"Took “{tag['name']}” off {count(tags.remove(conn, tag['id'], [r['id'] for r in rows]))}.")
            if action == "category" and category_id == "excluded":
                action = "exclude"  # NOTE: a choice beside the categories, as in the list's menus
            if action == "category":
                new_category = optional_int(category_id)  # "none" means uncategorized
                if not category_id or (new_category is not None and category_name(conn, new_category) is None):
                    return redirect(back, "Pick a category first.")
                conn.executemany("UPDATE transactions SET category_id = ?, excluded = 0, user_edited = 1, reviewed = 1 WHERE id = ?",
                                 [(new_category, r["id"]) for r in regular])
                return redirect(back, f"{count(len(regular))} now in {category_name(conn, new_category) or 'Uncategorized'}.{skipped}")
            if action in ("review", "unreview"):
                conn.executemany("UPDATE transactions SET reviewed = ? WHERE id = ?", [(int(action == "review"), r["id"]) for r in rows])
                return redirect(back, f"Marked {count(len(rows))} as {'reviewed' if action == 'review' else 'not reviewed yet'}.")
            if action in ("exclude", "include"):
                conn.executemany("UPDATE transactions SET excluded = ?, user_edited = 1 WHERE id = ?",
                                 [(int(action == "exclude"), r["id"]) for r in regular])
                done = "excluded from spending" if action == "exclude" else "counted in spending again"
                return redirect(back, f"{count(len(regular))} {done}.{skipped}")
            if action == "link":
                chosen = links.wholes(conn, rows)
                problem = links.link_problem(conn, chosen)
                if problem:
                    return redirect(back, problem)
                links.link(conn, chosen)
                return redirect(back, f"Linked {count(len(chosen))}. Each still counts on its own date, in its own category.")
            if action == "unlink":
                linked = [r for r in links.wholes(conn, rows) if links.is_linked(conn, r)]
                for r in linked:
                    links.unlink(conn, r)
                return redirect(back, f"Unlinked {count(len(linked))}.")
            if action == "combine":
                first, others = rows[0], rows[1:]
                allowed = {c["id"] for c in splits.combine_candidates(conn, first) or []}
                if not others or any(r["id"] not in allowed for r in others):
                    return redirect(back, "Only transactions from one account, without a record from your bank, up to 3 days apart, "
                                          "can be combined, and none of them already in a split or linked to another.")
                name = name.strip() or first["name"].split(":")[0].strip() or first["name"]
                splits.combine(conn, first, others, name)
                return redirect(back, f"Combined {count(len(rows))} into “{name}”. Each still counts in its own category.")
        return redirect(back, "Pick something to do with them.")

    @app.get("/transactions/{tid}")
    def edit_transaction(request: Request, tid: int, back: str = "/transactions", conn=Depends(get_db)):
        t = conn.execute(
            "SELECT t.*, a.name AS account_name, a.mask AS account_mask FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.id = ?",
            (tid,),
        ).fetchone()
        if t is None:
            raise HTTPException(404)
        parts = conn.execute(f"{_LINK_ROWS} WHERE t.split_from_id = ? ORDER BY t.id", (tid,)).fetchall()
        split_parent = siblings = None
        if t["split_from_id"]:
            split_parent = conn.execute(f"{_LINK_ROWS} WHERE t.id = ?", (t["split_from_id"],)).fetchone()
            siblings = conn.execute(f"{_LINK_ROWS} WHERE t.split_from_id = ? AND t.id != ? ORDER BY t.id",
                                    (t["split_from_id"], tid)).fetchall()
        linked_rows = conn.execute(f"{_LINK_ROWS} WHERE t.link_id = ? AND t.id != ? ORDER BY abs(t.amount) DESC, t.date, t.id",
                                   (t["link_id"], tid)).fetchall() if t["link_id"] is not None else []
        # A transaction split into parts counts only through them, so it links to nothing itself, and is no candidate.
        # One linked with others (links.py) can't pay back a purchase or be one side of a move.
        # The candidates come in two groups: those that fit, and those that pay a purchase back in full with some over
        # (a friend rounding up), which go last, in "oversized".
        purchase = paybacks = candidates = oversized = None
        if t["reimburses_id"]:
            purchase = conn.execute(f"{_LINK_ROWS} WHERE t.id = ?", (t["reimburses_id"],)).fetchone()
        elif t["amount"] > 0 and t["type"] != "transfer" and not parts and not linked_rows:
            # Purchases this money could pay back, from up to 60 days before, nearest first: as big as it or bigger, then smaller.
            purchases = lambda size, limit: conn.execute(
                f"""{_LINK_ROWS} WHERE t.amount < 0 AND t.type = 'regular' AND t.reimburses_id IS NULL AND {reports.NOT_SPLIT}
                      AND {size} AND t.date BETWEEN date(?, '-60 days') AND date(?, '+3 days')
                    ORDER BY abs(julianday(t.date) - julianday(?)), t.id DESC LIMIT {limit}""",
                (t["amount"], t["date"], t["date"], t["date"]),
            ).fetchall()
            candidates, oversized = purchases("-t.amount >= ? - 0.005", 40), purchases("-t.amount < ? - 0.005", 20)
        elif t["amount"] < 0 and t["type"] == "regular" and not parts:
            paybacks = conn.execute(f"{_LINK_ROWS} WHERE t.reimburses_id = ? ORDER BY t.date, t.id", (tid,)).fetchall()
            # Money in that could pay this back, from a few days before to 60 days after, not linked yet: up to its
            # amount, then more.
            money_in = lambda size, limit: conn.execute(
                f"""{_LINK_ROWS} WHERE t.amount > 0 AND t.type != 'transfer' AND t.reimburses_id IS NULL AND t.id != ?
                      AND {size} AND t.date BETWEEN date(?, '-3 days') AND date(?, '+60 days')
                      AND NOT EXISTS (SELECT 1 FROM transactions b WHERE b.reimburses_id = t.id) AND {reports.NOT_SPLIT}
                      AND NOT {links.LINKED.format(t='t')}
                    ORDER BY abs(julianday(t.date) - julianday(?)), t.id LIMIT {limit}""",
                (tid, -t["amount"], t["date"], t["date"], t["date"]),
            ).fetchall()
            candidates, oversized = money_in("t.amount <= ? + 0.005", 40), money_in("t.amount > ? + 0.005", 20)
        other = other_candidates = None
        if t["transfer_id"]:
            other = conn.execute(f"{_LINK_ROWS} WHERE t.id = ?", (t["transfer_id"],)).fetchone()
        elif not parts and not t["split_from_id"] and not t["reimburses_id"] and not paybacks and t["type"] != "income" and not linked_rows:
            # The opposite amount on another account within 10 days, not part of another link, nearest first.
            other_candidates = conn.execute(
                f"""{_LINK_ROWS} WHERE t.account_id != ? AND abs(t.amount + ?) < 0.005 AND t.type != 'income'
                      AND t.transfer_id IS NULL AND t.reimburses_id IS NULL AND {reports.NOT_SPLIT}
                      AND NOT EXISTS (SELECT 1 FROM transactions b WHERE b.reimburses_id = t.id) AND NOT {links.LINKED.format(t='t')}
                      AND t.date BETWEEN date(?, '-10 days') AND date(?, '+10 days')
                    ORDER BY abs(julianday(t.date) - julianday(?)), t.id LIMIT 20""",
                (t["account_id"], t["amount"], t["date"], t["date"], t["date"]),
            ).fetchall()
        categories = category_options(conn)
        return render(
            request, conn, "transaction_edit.html", t=t, back=safe_back(back, "/transactions"),
            categories=categories, category_names={c["id"]: c["label"] for c in categories},
            purchase=purchase, paybacks=paybacks, candidates=candidates, oversized=oversized,
            other=other, other_candidates=other_candidates, parts=parts, split_parent=split_parent, siblings=siblings, linked_rows=linked_rows,
            split_problem=None if parts else splits.split_problem(conn, t),
            # Copilot names the parts of an order like "Store: Coffee mug"; the combined line gets the shop's name.
            combine=splits.combine_candidates(conn, t), combine_name=t["name"].split(":")[0].strip() or t["name"],
            tag_ids={r[0] for r in conn.execute("SELECT tag_id FROM transaction_tags WHERE transaction_id = ?", (tid,))},
            bank=bank_details(t),
            recurring_list=conn.execute("SELECT id, name FROM recurring ORDER BY name").fetchall(),
            own_names=dict(conn.execute("SELECT id, name FROM categories").fetchall()),
            set_aside=tid in reports.everyday(conn)["id"],
        )

    @app.post("/transactions/{tid}/split")
    def save_split(tid: int, part_id: list[str] = Form([]), part_name: list[str] = Form([]), part_amount: list[str] = Form([]),
                   part_category: list[str] = Form([]), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Splits transaction `tid` into the parts typed on its page, or changes the parts it's split into."""
        t = get_transaction(conn, tid)
        page = safe_back(back, f"/transactions/{tid}")
        had_parts = bool(splits.parts_of(conn, tid))
        problem = None if had_parts else splits.split_problem(conn, t)
        parts = []
        if not problem:
            parts, problem = splits.read_parts(part_id, part_name, part_amount, part_category, round(abs(t["amount"]) * 100), t["name"])
        if problem:
            return redirect(page, problem)
        try:
            with conn:
                kept = splits.save(conn, t, parts)
        except ValueError as exc:
            return redirect(page, str(exc))
        if kept:
            return redirect(page, f"Split saved. {plural(kept, 'part')} from your history {'is' if kept == 1 else 'are'} on "
                                  f"{'its' if kept == 1 else 'their'} own again, on the review list: check nothing counts twice.")
        if had_parts:
            return redirect(page, "Split saved.")
        return redirect(page, f"Split “{t['name']}” into {len(parts)} parts. Each counts on its own, in its own category.")

    @app.post("/transactions/{tid}/combine")
    def combine_transactions(tid: int, ids: list[int] = Form([]), name: str = Form(""), back: str = Form("/transactions"),
                             conn=Depends(get_db)):
        """Combines transaction `tid` and the chosen ones into one, which shows as a single line (splits.combine)."""
        t = get_transaction(conn, tid)
        back = safe_back(back, "/transactions")
        allowed = {c["id"]: c for c in splits.combine_candidates(conn, t) or []}
        chosen = [allowed[i] for i in dict.fromkeys(ids) if i in allowed]
        if not chosen or len(chosen) != len(set(ids)):
            return redirect(f"/transactions/{tid}?{urlencode({'back': back})}", "Pick at least one more transaction from the list to combine it with.")
        with conn:
            combined = splits.combine(conn, t, chosen, name.strip() or t["name"])
        return redirect(f"/transactions/{combined}?{urlencode({'back': back})}",
                        f"Combined {len(chosen) + 1} transactions into one line. Each still counts in its own category.")

    @app.post("/transactions/{tid}/unsplit")
    def unsplit_transaction(tid: int, back: str = Form("/transactions"), conn=Depends(get_db)):
        """Takes the parts out of transaction `tid`, so it counts whole again (or, for a combined one, separates them)."""
        t = get_transaction(conn, tid)
        combined = t["source"] == "group" and t["plaid_transaction_id"] is None
        try:
            with conn:
                kept = splits.unsplit(conn, t)
        except ValueError as exc:
            return redirect(safe_back(back, f"/transactions/{tid}"), str(exc))
        if combined:
            return redirect(safe_back(back, "/transactions"), "Ungrouped: they show as separate transactions again.")
        message = f"“{t['name']}” counts as one transaction again."
        if kept:
            message += (f" {plural(kept, 'part')} from your history {'was' if kept == 1 else 'were'} there before the split and "
                        f"{'stays' if kept == 1 else 'stay'} on {'its' if kept == 1 else 'their'} own, so check nothing counts twice.")
        return redirect(safe_back(back, f"/transactions/{tid}"), message)

    @app.post("/transactions/{tid}/pays-back")
    def link_payback(tid: int, purchase_id: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Links money in (`tid`) to the purchase it pays back, or unlinks it when purchase_id is empty."""
        payback, purchase = get_transaction(conn, tid), None
        if optional_int(purchase_id) is not None:
            purchase = get_transaction(conn, optional_int(purchase_id))
        problem = payback_problem(conn, payback, purchase)
        if problem:
            return redirect(safe_back(back, "/transactions"), problem)
        with conn:
            if purchase is None:
                conn.execute("UPDATE transactions SET reimburses_id = NULL WHERE id = ?", (tid,))
            else:
                # Hand-edited, so rules, syncs and own-transfer pairing leave it as a payback.
                conn.execute("UPDATE transactions SET reimburses_id = ?, type = 'regular', reviewed = 1, user_edited = 1 WHERE id = ?",
                             (purchase["id"], tid))
        if purchase is None:
            return redirect(safe_back(back, "/transactions"), f"“{payback['name']}” no longer pays back a purchase.")
        category = category_name(conn, purchase["category_id"]) or "uncategorized"
        return redirect(safe_back(back, "/transactions"),
                        f"“{payback['name']}” pays back “{purchase['name']}”: it counts in {category} on {short_date(purchase['date'])}.")

    @app.post("/transactions/{tid}/other-side")
    def link_other_side(tid: int, other_id: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Links `tid` with the other side of a move between the user's accounts, or unlinks it when other_id is empty."""
        t = get_transaction(conn, tid)
        if optional_int(other_id) is None:
            with conn:
                conn.execute("UPDATE transactions SET transfer_id = NULL WHERE id = ? OR transfer_id = ?", (tid, tid))
            return redirect(safe_back(back, "/transactions"),
                            "Unlinked. Both are still transfers; change the type of one that isn't.")
        other = get_transaction(conn, optional_int(other_id))
        problem = transfer_problem(conn, t, other)
        if problem:
            return redirect(safe_back(back, "/transactions"), problem)
        with conn:
            # Whatever either side was linked to before lets go of it.
            conn.execute("UPDATE transactions SET transfer_id = NULL WHERE transfer_id IN (?, ?)", (t["id"], other["id"]))
            for row, partner in ((t, other), (other, t)):
                conn.execute(
                    """UPDATE transactions SET transfer_id = ?, type = 'transfer', category_id = NULL, excluded = 0,
                              recurring_id = NULL, reimburses_id = NULL, reviewed = 1, user_edited = 1 WHERE id = ?""",
                    (partner["id"], row["id"]),
                )
        return redirect(safe_back(back, "/transactions"),
                        f"Linked “{t['name']}” and “{other['name']}” as one move between your accounts. Neither counts as spending.")

    @app.post("/transactions/{tid}/whole")
    def save_whole(tid: int, name: str = Form(""), day: str = Form("", alias="date"), back: str = Form("/transactions"),
                   quiet: str = Form(""), conn=Depends(get_db)):
        """Renames transaction `tid`, split into parts (or combined from them), or changes its date, which its parts
        take too (db.py). Each part keeps its own name. `quiet` (saved as it changed, app.js) leaves out the message."""
        t = get_transaction(conn, tid)
        page = safe_back(back, f"/transactions/{tid}")
        if t["split_from_id"]:
            return redirect(page, "A part is dated like the transaction it's split from. Change the date there.")
        try:
            when = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            return redirect(page, "Check the date, for example 2026-10-14.")
        name = name.strip() or t["name"]
        renamed = int(name != t["name"])
        with conn:
            # Set by hand, so the bank's later updates keep them (sync._update_existing), and so do renaming rules.
            conn.execute("""UPDATE transactions SET name = ?, date = ?, user_edited = max(user_edited, ?), name_edited = max(name_edited, ?),
                                   date_edited = max(date_edited, ?) WHERE id = ?""",
                         (name, when, renamed, renamed, int(when != t["date"]), tid))
        offer = names.rename_offer(conn, get_transaction(conn, tid), t["name"]) if renamed else None
        if quiet:
            return redirect(page, f"Renamed it “{name}”." if offer else None, rename_offer=offer)
        return redirect(page, f"Saved. It's dated {short_date(when)}, and so are its parts." if when != t["date"] else "Saved.",
                        rename_offer=offer)

    @app.post("/transactions/{tid}/unlink")
    def unlink_transaction(tid: int, back: str = Form("/transactions"), conn=Depends(get_db)):
        """Takes transaction `tid` out of the transactions it's linked with (links.py)."""
        t = get_transaction(conn, tid)
        with conn:
            links.unlink(conn, t)
        return redirect(safe_back(back, "/transactions"), f"“{t['name']}” is no longer linked with the others.")

    @app.post("/transactions/{tid}/paybacks")
    def add_payback(tid: int, payback_id: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Links the money in chosen on purchase `tid`'s page to it."""
        if optional_int(payback_id) is None:
            return redirect(safe_back(back, "/transactions"), "Pick the money that paid it back.")
        return link_payback(optional_int(payback_id), str(tid), back, conn)

    @app.post("/transactions/{tid}")
    def save_transaction(tid: int, name: str = Form(...), category_id: str = Form(""), type: str = Form("regular"),
                         excluded: str = Form(""), notes: str = Form(""), reviewed: str = Form(""),
                         recurring_id: str = Form(""), make_rule: str = Form(""), back: str = Form("/transactions"),
                         day: str = Form("", alias="date"), tags_shown: str = Form(""), tag_ids: list[int] = Form([]),
                         new_tags: str = Form(""), quiet: str = Form(""), conn=Depends(get_db)):
        """Saves the transaction's form. `quiet` (a field saved as it changed, app.js) leaves out the word that it's
        saved, which the page already shows, but still offers a rule when the category changed."""
        t = get_transaction(conn, tid)
        if splits.parts_of(conn, tid):
            return redirect(f"/transactions/{tid}?{urlencode({'back': safe_back(back, '/transactions')})}",
                            "It's split into parts, which count instead of it: change those.")
        try:
            when = date.fromisoformat(day.strip()).isoformat() if day.strip() else t["date"]
        except ValueError:
            return redirect(f"/transactions/{tid}?{urlencode({'back': safe_back(back, '/transactions')})}",
                            "Check the date, for example 2026-10-14.")
        if t["split_from_id"]:
            when = t["date"]  # a part is dated like the transaction it's split from (db.py), changed there
        # A date set by hand is kept when the bank's later updates move it (sync._update_existing).
        moved = when != t["date"]
        type_ = type if type in categorize.TYPES else t["type"]
        # Excluded is a choice in the category menu, so a transaction is in a category or excluded, not both.
        exclude = type_ == "regular" and (category_id == "excluded" or bool(excluded))
        if exclude:
            category_id = ""
        if t["reimburses_id"]:
            category_id = str(t["category_id"] or "")  # a payback has its purchase's category (db.py keeps it so)
        new = {
            # Income and transfers take no category, even if the hidden category menu still had one picked.
            "name": name.strip() or t["name"], "category_id": optional_int(category_id) if type_ == "regular" else None,
            # Excluding only takes something out of spending, which income and transfers never count toward.
            "type": type_, "excluded": int(exclude),
        }
        edited = any(new[k] != t[k] for k in new)
        renamed = new["name"] != t["name"]
        message = f"Saved. It's now dated {short_date(when)}." if moved else "Saved."
        with conn:
            conn.execute(
                """UPDATE transactions SET name = :name, category_id = :category_id, type = :type, excluded = :excluded,
                          notes = :notes, reviewed = :reviewed, recurring_id = :recurring_id, date = :date,
                          user_edited = max(user_edited, :edited), name_edited = max(name_edited, :renamed),
                          date_edited = max(date_edited, :moved)
                   WHERE id = :id""",
                {**new, "notes": notes.strip() or None, "reviewed": int(bool(reviewed)),
                 # The part of a split charge isn't the charge: the transaction it's split from keeps the recurring link.
                 "recurring_id": optional_int(recurring_id) if type_ != "transfer" and not t["split_from_id"] else None, "date": when,
                 "edited": int(edited), "renamed": int(renamed), "moved": int(moved),
                 "id": tid},
            )
            if new["type"] != "regular":
                conn.execute("UPDATE transactions SET reimburses_id = NULL WHERE id = ?", (tid,))  # only regular money pays back
            if new["type"] != "transfer":
                # No longer a move between accounts, so neither side is linked to the other.
                conn.execute("UPDATE transactions SET transfer_id = NULL WHERE id = ? OR transfer_id = ?", (tid, tid))
            if tags_shown:  # a form without the tags (say, from an older page) leaves them as they are
                tags.set_tags(conn, tid, tag_ids + [tags.get_or_create(conn, n) for n in tags.parse_names(new_tags)])
            if new["category_id"] != t["category_id"] and not exclude:
                learn_plaid_mapping(conn, t["plaid_category"], new["category_id"])
            if make_rule:
                rule_id = categorize.add_rule(conn, new["name"], "equals", new["category_id"], new["type"])
                changed = categorize.apply_rule(conn, rule_id)
                message = f"Saved, and {changed} other transactions named “{new['name']}” were updated. New ones will match too."
            if new["type"] == "transfer":
                sync.pair_own_transfers(conn)  # the other side of a move between the user's accounts follows
        offer = rename = None
        if new["category_id"] != t["category_id"] and not make_rule:
            offer = rule_offer(conn, get_transaction(conn, tid), new["category_id"])
        if renamed and not make_rule:
            rename = names.rename_offer(conn, get_transaction(conn, tid), t["name"])
        if quiet and not make_rule:
            message = (f"“{new['name']}” is now {category_name(conn, new['category_id'])}." if offer
                       else f"Renamed it “{new['name']}”." if rename else None)
        return redirect(safe_back(back, "/transactions"), message, rule_offer=offer, rename_offer=rename)

    @app.post("/rules/rename-from-transaction/{tid}")
    def rename_from_transaction(tid: int, old: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Gives every transaction named `old` the name transaction `tid` was just given, and new ones too."""
        t = get_transaction(conn, tid)
        if not old.strip() or old == t["name"]:
            return redirect(safe_back(back, "/transactions"), "It already has that name.")
        with conn:
            renamed = names.rename_all(conn, old, t["name"])
        later = f"New ones named “{old.strip()}” will come named “{t['name']}”."
        return redirect(safe_back(back, "/transactions"), f"Renamed {plural(renamed, 'other')}. {later}" if renamed else later)

    @app.post("/rules/from-transaction/{tid}")
    def rule_from_transaction(tid: int, back: str = Form("/transactions"), conn=Depends(get_db)):
        t = get_transaction(conn, tid)
        with conn:
            rule_id = categorize.add_rule(conn, t["name"], "equals", t["category_id"], t["type"])
            changed = categorize.apply_rule(conn, rule_id)
        return redirect(safe_back(back, "/transactions"), f"Rule saved: “{t['name']}” → {category_name(conn, t['category_id'])}. Updated {changed} other transactions.")

    # ---- reports ----

    @app.get("/reports")
    def reports_page(request: Request, conn=Depends(get_db)):
        """A report: what was spent or earned over a span, by month, quarter or year and by category, merchant, account or
        tag, with what it leaves out for itself alone (report_filters). An address that holds its filters otherwise (as
        the filter menu sends them, say) leads to the one that holds them so."""
        query = request.query_params.multi_items()
        filters = report_filters(query)
        pairs = report_pairs(filters)
        if sorted(query) != sorted(pairs):
            return RedirectResponse("/reports" + (f"?{urlencode(pairs)}" if pairs else ""), status_code=303)
        view = report_view(conn, filters, date.today())
        saved = conn.execute("SELECT * FROM saved_reports ORDER BY name COLLATE NOCASE, id").fetchall()
        current = next((r for r in saved if str(r["id"]) == filters["saved"]), None)
        own = urlencode(report_pairs(filters, saved=False))
        aside = reports.everyday(conn)
        return render(
            request, conn, "reports.html", filters=filters, **view, saved_reports=saved, current=current,
            changed=current is not None and current["query"] != own, own_query=own,
            chips=report_chips(conn, filters, view["report_link"]), choices=filter_choices(conn, counted=True),
            menu_count=sum(len(filters[k]) for k in REPORT_MANY) + bool(filters["over"]) + bool(filters["everyday"]),
            has_everyday=any(aside[k] for k in reports.LEAVE_OUTS) or aside["over"] is not None,
            periods=PERIODS, report_by=REPORT_BY, report_shows=REPORT_SHOWS, report_groups=REPORT_GROUPS,
            month_options=[r[0] for r in conn.execute("SELECT DISTINCT substr(date, 1, 7) FROM transactions ORDER BY 1 DESC")],
            merchant_names=merchant_names(conn),
        )

    @app.get("/reports/transactions")
    def report_transactions(request: Request, column: str = "", key: str = "", conn=Depends(get_db)):
        """The transactions a part of a report's chart adds up: one of its columns (`column`, reports.column_of) and one
        of its series (`key`, reports.keys_of; "other" for those folded together), or all of either. In the pane beside
        the report (app.js), or on a page of their own."""
        filters = report_filters([(k, v) for k, v in request.query_params.multi_items() if k not in ("column", "key")])
        span = report_span(conn, filters, date.today())
        leave = report_leave_outs(conn, filters)
        by, group, show = filters["by"], filters["group"], filters["show"]
        groups = reports.category_groups(conn)
        columns = list(dict.fromkeys(reports.column_of(m, by) for m in span["months"]))
        column = column if column in columns else ""
        start, end = span["start"], span["end"]
        if column:
            first, after = reports.column_range(column)
            start, end = max(start, first), min(end, after)
        every = reports.counted(conn, span["start"], span["end"], show, leave)
        rows = [r for r in every if start <= r["day"] < end]
        label = ""
        if key:
            data = reports.grouped(every, columns, by, group, groups)
            folded = set(data["folded"])
            rows = [r for r in rows if any(k in folded if key == "other" else k == key for k in reports.keys_of(r, group, groups))]
            shown = [{"key": key}]
            series_looks(conn, group, shown, every)
            label = shown[0]["label"]
        when = column_labels(column, False)["label"] if column else span["dates"]
        back = "/reports" + (f"?{urlencode(report_pairs(filters))}" if report_pairs(filters) else "")
        rows.sort(key=lambda r: (r["day"], r["id"]), reverse=True)
        return render(request, conn, "report_transactions.html", rows=rows, label=label, when=when, back=back, show=show,
                      total=round(sum(r["value"] for r in rows), 2), several_tags=group == "tag" and key == "other")

    @app.post("/reports/saved")
    def save_report(name: str = Form(""), query: str = Form(""), conn=Depends(get_db)):
        """Saves the report whose address holds `query` as `name`, and shows it."""
        pairs = report_pairs(report_filters(parse_qsl(query)), saved=False)
        name = name.strip()[:80] or "My report"
        with conn:
            rid = conn.execute("INSERT INTO saved_reports (name, query) VALUES (?, ?)", (name, urlencode(pairs))).lastrowid
        return redirect(f"/reports?{urlencode(pairs + [('saved', rid)])}", f"Saved “{name}”. It's listed above the report.")

    @app.post("/reports/saved/{rid}")
    def change_report(rid: int, name: str = Form(""), query: str | None = Form(None), pinned: str | None = Form(None),
                      back: str = Form("/reports"), conn=Depends(get_db)):
        """Renames saved report `rid` (`name`), saves `query` as its filters, or pins it to the dashboard or takes it off
        (`pinned`, "1" or "0")."""
        report = conn.execute("SELECT * FROM saved_reports WHERE id = ?", (rid,)).fetchone()
        if report is None:
            raise HTTPException(404, "Report not found.")
        new_name = name.strip()[:80] or report["name"]
        new_query = urlencode(report_pairs(report_filters(parse_qsl(query)), saved=False)) if query is not None else report["query"]
        pin = int(pinned == "1") if pinned is not None else report["pinned"]
        with conn:
            conn.execute("UPDATE saved_reports SET name = ?, query = ?, pinned = ? WHERE id = ?", (new_name, new_query, pin, rid))
        if pinned is not None:
            message = f"“{new_name}” is on the dashboard." if pin else f"Took “{new_name}” off the dashboard."
        else:
            message = f"Saved “{new_name}”."
        return redirect(safe_back(back, "/reports") if query is None else f"/reports?{new_query}{'&' if new_query else ''}saved={rid}", message)

    @app.post("/reports/saved/{rid}/delete")
    def delete_report(rid: int, conn=Depends(get_db)):
        report = conn.execute("SELECT * FROM saved_reports WHERE id = ?", (rid,)).fetchone()
        if report is None:
            return redirect("/reports")
        with conn:
            conn.execute("DELETE FROM saved_reports WHERE id = ?", (rid,))
        return redirect("/reports" + (f"?{report['query']}" if report["query"] else ""), f"Deleted “{report['name']}”. Its report is still shown here.")

    # ---- everyday spending ----

    @app.get("/everyday")
    def everyday_page(request: Request, back: str = "/", conn=Depends(get_db)):
        """What's set aside from everyday spending (reports.everyday): categories, tags, merchants, single transactions
        and anything over an amount. In the pane (app.js), or on a page of its own."""
        aside = reports.everyday(conn)
        picked = conn.execute(f"""SELECT t.*, a.name AS account_name FROM transactions t JOIN accounts a ON a.id = t.account_id
                                  WHERE t.id IN ({','.join('?' * len(aside['id']))}) ORDER BY t.date DESC""", aside["id"]).fetchall()
        month = reports.current_month(date.today())
        first = reports.shift_month(month, -11)
        start, end = reports.month_range(first)[0], reports.month_range(month)[1]
        spent, everyday_spent = reports.totals(conn, start, end)["spent"], reports.totals(conn, start, end, aside)["spent"]
        return render(request, conn, "everyday.html", aside=aside, picked=picked, choices=filter_choices(conn, counted=True),
                      back=safe_back(back, "/"), merchant_names=merchant_names(conn), set_aside=round(spent - everyday_spent, 2),
                      shown=get_meta(conn, "everyday_shown") == "1", today_year=str(date.today().year))

    @app.post("/everyday")
    def save_everyday(category: list[str] = Form([]), tag: list[str] = Form([]), merchant: list[str] = Form([]),
                      over: str = Form(""), ids: list[str] = Form([]), back: str = Form("/"), quiet: str = Form(""),
                      conn=Depends(get_db)):
        """Sets aside from everyday spending the categories, tags, merchants and transactions (`ids`) ticked, and anything
        over `over`; then shows what's set aside again."""
        try:
            amount = optional_money(over)
        except ValueError:
            return redirect(f"/everyday?{urlencode({'back': safe_back(back, '/')})}", f"“{over.strip()}” isn't an amount.")
        leave = {"category": [c for c in dict.fromkeys(category) if c.isdigit() or c == "none"],
                 "tag": [int(g) for g in dict.fromkeys(tag) if g.isdigit()], "merchant": list(dict.fromkeys(m.strip() for m in merchant if m.strip())),
                 "id": [int(i) for i in dict.fromkeys(ids) if i.isdigit()], "over": abs(amount) if amount else None}
        with conn:
            reports.set_everyday(conn, leave)
        return redirect(f"/everyday?{urlencode({'back': safe_back(back, '/')})}", None if quiet else "Saved what's set aside from everyday spending.")

    @app.post("/everyday/transactions/{tid}")
    def set_aside(tid: int, aside: str = Form("1"), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Sets transaction `tid` aside from everyday spending (`aside` "1"), or counts it as everyday spending again."""
        t = get_transaction(conn, tid)
        leave = reports.everyday(conn)
        with conn:
            reports.set_everyday(conn, {**leave, "id": [i for i in leave["id"] if i != tid] + ([tid] if aside == "1" else [])})
        if aside == "1":
            return redirect(safe_back(back, "/transactions"), f"Set aside “{t['name']}”: it still counts, but not as everyday spending.")
        return redirect(safe_back(back, "/transactions"), f"“{t['name']}” counts as everyday spending again.")

    @app.post("/everyday/shown")
    def show_everyday(shown: str = Form(""), back: str = Form("/"), conn=Depends(get_db)):
        """Shows the dashboard's spending by month split into everyday spending and what's set aside, or not (`shown`)."""
        with conn:
            set_meta(conn, "everyday_shown", "1" if shown == "1" else "")
        return redirect(safe_back(back, "/"))

    # ---- categories and budgets ----

    @app.get("/budgets")
    def old_budgets(request: Request):
        """Budgets are on Categories now; older links lead there."""
        return RedirectResponse("/categories" + (f"?{request.url.query}" if request.url.query else ""), status_code=303)

    @app.get("/categories")
    def categories_page(request: Request, month: str | None = None, edit: str = "", conn=Depends(get_db)):
        """Categories: what each spent in the month against its budget (budget_rows), the others below; with `edit`, the
        month's budgets to change, from then on or for that month alone."""
        today = date.today()
        month = reports.valid_month(month) or reports.current_month(today)
        nodes = budget_rows(conn, reports.category_spending(conn, month), month, today)
        shown = {n["id"] for n in nodes} | {ch["id"] for n in nodes for ch in n["children"]}
        every = conn.execute("SELECT id, name, parent_id FROM categories WHERE excluded = 0 ORDER BY name COLLATE NOCASE").fetchall()
        days_left = (date.fromisoformat(reports.month_range(month)[1]) - today).days if month == reports.current_month(today) else None
        return render(
            request, conn, "categories.html", month=month, nodes=nodes, budget=reports.budget_total(nodes),
            totals=reports.month_totals(conn, month), others=[c for c in every if c["id"] not in shown],
            prev_month=reports.shift_month(month, -1), next_month=reports.shift_month(month, 1),
            this_month=reports.current_month(today), editing=edit == "1", days_left=days_left,
            left_out=conn.execute("SELECT id, name FROM categories WHERE excluded = 1 ORDER BY name COLLATE NOCASE").fetchall(),
            top_level=[c for c in every if c["parent_id"] is None],
        )

    @app.post("/budgets")
    async def save_budgets(request: Request, conn=Depends(get_db)):
        """Saves the budgets typed for a month (`month`, else this one): from then on, or for that month alone (`apply`
        "month"); an empty one means no budget."""
        form = await request.form()
        back = safe_back(str(form.get("back")), "/categories")
        month = reports.valid_month(str(form.get("month") or "")) or reports.current_month(date.today())
        only = form.get("apply") == "month"
        updates = []
        for key, value in form.items():
            if key.startswith("budget_") and key[7:].isdigit():
                try:
                    updates.append((int(key[7:]), optional_money(str(value))))
                except ValueError:
                    return redirect(back, f"“{value}” isn't a number.")
        with conn:
            for cid, amount in updates:
                reports.set_budget(conn, cid, month, amount, only)
        return redirect(back, f"Budgets saved for {month_label(month)} only." if only else f"Budgets saved from {month_label(month)} on.")

    @app.get("/categories/{cid}")
    def category_page(request: Request, cid: int, month: str | None = None, conn=Depends(get_db)):
        """A category's own page (category_view): the month against its budget, its last 12 months, its top merchants,
        the month's transactions, and its settings."""
        cat = conn.execute("SELECT c.*, p.name AS parent_name FROM categories c LEFT JOIN categories p ON p.id = c.parent_id WHERE c.id = ?",
                           (cid,)).fetchone()
        if cat is None:
            raise HTTPException(404, "Category not found.")
        today = date.today()
        month = reports.valid_month(month) or reports.current_month(today)
        return render(request, conn, "category.html", cat=cat, month=month, **category_view(conn, cat, month, today),
                      prev_month=reports.shift_month(month, -1), next_month=reports.shift_month(month, 1),
                      this_month=reports.current_month(today), categories=category_options(conn),
                      top_level=conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL AND id != ? ORDER BY name COLLATE NOCASE",
                                             (cid,)).fetchall())

    @app.post("/categories/{cid}/budget")
    def save_category_budget(cid: int, amount: str = Form(""), month: str = Form(""), apply: str = Form(""), rollover: str = Form(""),
                             conn=Depends(get_db)):
        """Saves category `cid`'s budget for `month`, from then on or for that month alone (`apply` "month"), and whether
        what's left of it or over it rolls over into the next month, counted from that month."""
        month = reports.valid_month(month) or reports.current_month(date.today())
        back = f"/categories/{cid}?month={month}"
        if conn.execute("SELECT 1 FROM categories WHERE id = ?", (cid,)).fetchone() is None:
            raise HTTPException(404, "Category not found.")
        try:
            budget = optional_money(amount)
        except ValueError:
            return redirect(back, f"“{amount.strip()}” isn't a number.")
        with conn:
            reports.set_budget(conn, cid, month, budget, apply == "month")
            conn.execute("UPDATE categories SET rollover_since = CASE WHEN ? THEN coalesce(rollover_since, ?) END WHERE id = ?",
                         (int(bool(rollover)), month, cid))
        return redirect(back, "Budget saved.")

    # ---- recurring ----

    @app.get("/recurring")
    def recurring_page(request: Request, conn=Depends(get_db)):
        today = date.today()
        items = recurring.list_recurring(conn, today)
        return render(
            request, conn, "recurring.html", items=items, today=today,
            monthly=sum(i["monthly"] for i in items if i["active"] and (i["amount"] or 0) < 0),
            summary=recurring.month_summary(conn, items, today),
            candidates=recurring.find_candidates(conn, today), categories=category_options(conn),
            frequencies=list(recurring.FREQUENCIES),
        )

    # Registered before /recurring/{rid}, which would otherwise take "new" for an id.
    @app.get("/recurring/new")
    def new_recurring(request: Request, back: str = "/recurring", conn=Depends(get_db)):
        """Tracking a recurring charge by hand: in the pane (app.js), or on a page of its own."""
        return render(request, conn, "recurring_new.html", back=safe_back(back, "/recurring"), categories=category_options(conn),
                      frequencies=list(recurring.FREQUENCIES))

    @app.get("/recurring/{rid}")
    def recurring_item(request: Request, rid: int, back: str = "/recurring", conn=Depends(get_db)):
        """A tracked recurring charge, to change: in the pane beside the list (app.js), or on a page of its own. It lists
        the charges it has matched, newest first."""
        item = next((r for r in recurring.list_recurring(conn, date.today()) if r["id"] == rid), None)
        if item is None:
            raise HTTPException(404, "Recurring charge not found.")
        charges = conn.execute(f"{_LINK_ROWS} WHERE t.recurring_id = ? ORDER BY t.date DESC, t.id DESC LIMIT 24", (rid,)).fetchall()
        year_ago = (date.today() - timedelta(days=365)).isoformat()
        return render(request, conn, "recurring_edit.html", r=item, charges=charges, back=safe_back(back, "/recurring"), today=date.today(),
                      categories=category_options(conn), frequencies=list(recurring.FREQUENCIES),
                      year_total=round(-sum(c["amount"] for c in charges if c["date"] >= year_ago), 2))

    @app.post("/recurring/track")
    def track_recurring(name: str = Form(...), pattern: str = Form(...), frequency: str = Form("monthly"),
                        amount: str = Form(""), category_id: str = Form(""), back: str = Form("/recurring"), open_it: str = Form("", alias="open"),
                        conn=Depends(get_db)):
        """Starts tracking a recurring charge, and links the past transactions it matches; with `open`, shows it then."""
        back = safe_back(back, "/recurring")
        if frequency not in recurring.FREQUENCIES or not pattern.strip():
            return redirect(back, "Pick a frequency and the text to match.")
        try:
            typical = optional_money(amount)
        except ValueError:
            return redirect(back, f"“{amount.strip()}” isn't an amount.")
        with conn:
            rid, linked = recurring.track(conn, name.strip(), pattern, frequency, typical, optional_int(category_id))
        message = f"Tracking {name}. Linked {plural(linked, 'past transaction')}."
        return redirect(f"/recurring/{rid}?{urlencode({'back': back})}" if open_it else back, message)

    @app.post("/recurring/{rid}")
    def save_recurring(rid: int, name: str = Form(...), frequency: str = Form("monthly"), amount: str = Form(""),
                       category_id: str = Form(""), active: str = Form(""), back: str = Form("/recurring"), quiet: str = Form(""),
                       conn=Depends(get_db)):
        """Saves recurring charge `rid`'s fields, as they change in its pane (`quiet`: no message), or with Save."""
        back = safe_back(back, "/recurring")
        if frequency not in recurring.FREQUENCIES:
            return redirect(back, "Unknown frequency.")
        try:
            typical = optional_money(amount)
        except ValueError:
            return redirect(back, f"“{amount.strip()}” isn't an amount.")
        with conn:
            conn.execute(
                "UPDATE recurring SET name = ?, frequency = ?, amount = ?, category_id = ?, active = ? WHERE id = ?",
                (name.strip(), frequency, typical, optional_int(category_id), int(bool(active)), rid),
            )
        return redirect(back, None if quiet else "Saved.")

    @app.post("/recurring/{rid}/delete")
    def delete_recurring(rid: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM recurring WHERE id = ?", (rid,))
        return redirect("/recurring", "Stopped tracking it. Its transactions are kept.")

    # ---- accounts and Plaid ----

    @app.get("/accounts")
    def accounts_page(request: Request, conn=Depends(get_db)):
        items = conn.execute("SELECT * FROM plaid_items ORDER BY created_at").fetchall()
        return render(
            request, conn, "accounts.html", worth=reports.net_worth(conn), items=items,
            worth_chart=worth_chart(reports.net_worth_history(conn)), imported=get_meta(conn, "copilot_imported_at"),
        )

    # Registered before /accounts/{aid}, which would otherwise take "new" for an account id.
    @app.post("/accounts/new")
    def add_account(name: str = Form(""), conn=Depends(get_db)):
        """Adds an account no bank sends transactions for, such as a gift card balance, to add transactions to by hand."""
        if not name.strip():
            return redirect("/accounts", "Give the account a name.")
        with conn:
            conn.execute("INSERT INTO accounts (name, type, source) VALUES (?, 'other', 'manual')", (name.strip(),))
        return redirect("/accounts", f"Added “{name.strip()}”. Pick it when you add a transaction.")

    @app.get("/accounts/{aid}")
    def account_page(request: Request, aid: int, back: str = "/accounts", conn=Depends(get_db)):
        """An account: its balance as the bank shows it, its latest transactions, and its name and whether it's hidden or
        archived, to change. In the pane beside the accounts (app.js), or on a page of its own."""
        account = next((a for g in reports.net_worth(conn)["groups"] for a in g["accounts"] if a["id"] == aid), None)
        if account is None:
            raise HTTPException(404, "Account not found.")
        latest = conn.execute(f"""SELECT t.*, c.name AS category_name FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
                                   WHERE t.account_id = ? AND {reports.NOT_SPLIT} ORDER BY t.date DESC, t.id DESC LIMIT 8""", (aid,)).fetchall()
        count = conn.execute(f"SELECT count(*) FROM transactions t WHERE t.account_id = ? AND {reports.NOT_SPLIT}", (aid,)).fetchone()[0]
        return render(request, conn, "account_edit.html", a=account, latest=latest, count=count, back=safe_back(back, "/accounts"),
                      item=conn.execute("SELECT * FROM plaid_items WHERE item_id = ?", (account["plaid_item_id"],)).fetchone())

    @app.post("/accounts/{aid}")
    def save_account(aid: int, name: str = Form(...), hidden: str = Form(""), archived: str = Form(""), back: str = Form("/accounts"),
                     quiet: str = Form(""), conn=Depends(get_db)):
        """Saves account `aid`'s name and whether it's hidden or archived, as they change in its pane (`quiet`: no message)."""
        with conn:
            conn.execute("UPDATE accounts SET name = ?, hidden = ?, archived = ? WHERE id = ?",
                         (name.strip() or "Account", int(bool(hidden)), int(bool(archived)), aid))
        return redirect(safe_back(back, "/accounts"), None if quiet else "Account saved.")

    @app.post("/plaid/link-token")
    def plaid_link_token(request: Request, payload: dict = Body(default={}), conn=Depends(get_db)):
        if is_remote(request, settings):
            # A bank's OAuth login on another device needs an HTTPS redirect URI, which this app doesn't have, and a
            # login that breaks partway could still use up one of the Trial plan's 10 Items.
            return JSONResponse({"error": "Connect banks and log in again on the computer running the app, at http://localhost:8080."}, 400)
        client = sync.make_client(settings)
        if client is None:
            return JSONResponse({"error": "Add your Plaid keys to the .env file and restart the app first."}, 400)
        access_token = None
        if payload.get("item_id"):
            item = conn.execute("SELECT access_token FROM plaid_items WHERE item_id = ?", (payload["item_id"],)).fetchone()
            if item is None:
                return JSONResponse({"error": "That connection no longer exists."}, 404)
            access_token = item["access_token"]
        try:
            return {"link_token": client.create_link_token(access_token)}
        except PlaidError as exc:
            return JSONResponse({"error": exc.friendly}, 502)

    @app.post("/plaid/exchange")
    def plaid_exchange(payload: dict = Body(...), conn=Depends(get_db)):
        client = sync.make_client(settings)
        if client is None or not payload.get("public_token"):
            return JSONResponse({"error": "Missing Plaid keys or token."}, 400)
        institution = payload.get("institution") or {}
        name = institution.get("name") or "That bank"
        log.info("Link finished at %s (link_session_id %s)", name, payload.get("link_session_id"))
        # Checked before exchanging the token: the exchange is what counts as a new connection at Plaid.
        if not payload.get("allow_duplicate") and sync.find_duplicate(conn, client.env, institution, payload.get("accounts")):
            return JSONResponse({"duplicate": True, "error": (
                f"{name} is already connected. If that connection stopped working, choose Cancel and use Log in again "
                "on the Accounts page instead. Connecting the same bank twice uses another of your 10 Plaid slots, "
                "and some banks then shut off the first connection."
            )}, 409)
        try:
            item = sync.link_item(conn, client, payload["public_token"], institution)
        except PlaidError as exc:
            log.warning("Saving the connection to %s failed: %s", name, exc)
            return JSONResponse({"error": exc.friendly}, 502)
        if item["status"] == "needs_match":
            return {"redirect": f"/plaid/match/{item['item_id']}"}
        response = JSONResponse({"redirect": "/accounts"})
        bank = item["institution_name"] or "your bank"
        if item["status"] == "ok":
            sync_in_background(settings, [item["item_id"]])
            set_flash(response, f"Connected {bank}. Transactions will appear over the next few minutes.")
        else:
            set_flash(response, f"Connected {bank}, but Plaid couldn't list its accounts yet: {item['error']} "
                                "The app keeps trying on its own, or press Sync now.")
        return response

    @app.post("/plaid/link-exit")
    def plaid_link_exit(payload: dict = Body(default={})):
        # Only the browser learns Link's session id, and Plaid support needs it to look into a failed connection.
        error = payload.get("error") or {}
        bank = (payload.get("institution") or {}).get("name") or "no bank chosen"
        if error:
            log.warning("Link closed with %s %s at %s (link_session_id %s, request_id %s)", error.get("error_type"),
                        error.get("error_code"), bank, payload.get("link_session_id"), payload.get("request_id"))
        else:
            log.info("Link closed without connecting at %s, step %s (link_session_id %s)", bank, payload.get("status"),
                     payload.get("link_session_id"))
        return {"ok": True}

    @app.get("/plaid/match/{item_id}")
    def match_page(request: Request, item_id: str, conn=Depends(get_db)):
        item = conn.execute("SELECT * FROM plaid_items WHERE item_id = ? AND status = 'needs_match'", (item_id,)).fetchone()
        if item is None:
            return redirect("/accounts")
        return render(
            request, conn, "match.html", item=item, pairs=sync.suggest_matches(conn, item),
            existing=conn.execute(
                """SELECT a.id, a.name, a.mask, count(t.id) AS count, min(t.date) AS first, max(t.date) AS last
                   FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
                   WHERE a.plaid_account_id IS NULL GROUP BY a.id ORDER BY a.name"""
            ).fetchall(),
        )

    @app.post("/plaid/match/{item_id}")
    async def save_match(request: Request, item_id: str, conn=Depends(get_db)):
        form = await request.form()
        choices = {key[8:]: optional_int(str(value)) for key, value in form.items() if key.startswith("account_")}
        try:
            sync.apply_matches(conn, item_id, choices)
        except ValueError as exc:
            return redirect(f"/plaid/match/{item_id}", str(exc))
        sync_in_background(settings, [item_id])
        return redirect("/accounts", "Accounts matched. New transactions will appear over the next few minutes.")

    @app.post("/plaid/items/{item_id}/repaired")
    def plaid_repaired(item_id: str, payload: dict = Body(default={}), conn=Depends(get_db)):
        # Update mode keeps the existing access token, so there's nothing to exchange.
        log.info("Logged in again through Link (link_session_id %s)", payload.get("link_session_id"))
        with conn:
            # An Item still waiting for its accounts to be matched stays that way.
            conn.execute("UPDATE plaid_items SET status = 'ok', error = NULL WHERE item_id = ? AND status != 'needs_match'", (item_id,))
        sync_in_background(settings, [item_id])
        response = JSONResponse({"redirect": "/accounts"})
        set_flash(response, "Connection updated. Syncing now.")
        return response

    @app.post("/plaid/items/{item_id}/remove")
    def plaid_remove(item_id: str, delete_data: str = Form(""), conn=Depends(get_db)):
        sync.disconnect_item(conn, sync.make_client(settings), item_id, bool(delete_data))
        return redirect("/accounts", "Disconnected." + (" Its transactions were deleted." if delete_data else " Its history was kept."))

    @app.post("/sync")
    def sync_now(back: str = Form("/")):
        if not settings.plaid_configured:
            return redirect(safe_back(back), "Add your Plaid keys to the .env file first.")
        results = sync.run_sync(settings)
        message = "; ".join(f"{name}: {text}" for name, text in results) or "No connected banks to sync yet."
        return redirect(safe_back(back), message)

    # ---- categories and rules ----

    @app.get("/rules")
    def rules_page(request: Request, conn=Depends(get_db)):
        """Rules, tags and the mapping of Plaid's categories to the user's; each category's own look and settings are on
        its page (category_page)."""
        cats = conn.execute(
            """SELECT c.*, p.name AS parent, (SELECT count(*) FROM transactions t WHERE t.category_id = c.id) AS uses
               FROM categories c LEFT JOIN categories p ON p.id = c.parent_id
               ORDER BY coalesce(p.name, c.name) COLLATE NOCASE, c.parent_id IS NOT NULL, c.name COLLATE NOCASE"""
        ).fetchall()
        seen_codes = {r[0] for r in conn.execute("SELECT DISTINCT plaid_category FROM transactions WHERE plaid_category IS NOT NULL")}
        mapping = dict(conn.execute("SELECT plaid_category, category_id FROM plaid_category_map").fetchall())
        codes = sorted(set(categorize.PLAID_HINTS) | seen_codes | set(mapping))
        rules = conn.execute(
            "SELECT r.*, c.name AS category FROM rules r LEFT JOIN categories c ON c.id = r.category_id ORDER BY r.pattern"
        ).fetchall()
        return render(
            request, conn, "rules.html", cats=cats, options=category_options(conn),
            top_level=[c for c in cats if c["parent_id"] is None], codes=codes, mapping=mapping, rules=rules,
            types=categorize.TYPES,
            tag_uses=dict(conn.execute("SELECT tag_id, count(*) FROM transaction_tags GROUP BY tag_id").fetchall()),
        )

    @app.post("/categories")
    def add_category(name: str = Form(...), parent_id: str = Form(""), back: str = Form("/categories"), conn=Depends(get_db)):
        name = name.strip()
        if not name:
            return redirect(safe_back(back, "/categories"), "Give the category a name.")
        with conn:
            categorize.get_or_create_category(conn, name, optional_int(parent_id))
            categorize.auto_map_plaid_categories(conn)
        return redirect(safe_back(back, "/categories"), f"Added {name}.")

    @app.post("/categories/defaults")
    def add_default_categories(conn=Depends(get_db)):
        with conn:
            added = categorize.ensure_default_categories(conn)
        return redirect("/categories", "Added the default categories." if added else "You already have categories.")

    @app.post("/categories/{cid}")
    def save_category(cid: int, name: str = Form(...), parent_id: str = Form(""), excluded: str = Form(""),
                      emoji: str = Form(""), color: str = Form(""), back: str = Form(""), conn=Depends(get_db)):
        """Saves category `cid`'s name, group, emoji and color, and whether it counts in spending."""
        back = safe_back(back, f"/categories/{cid}")
        parent = optional_int(parent_id)
        if parent == cid:
            parent = None
        # An empty emoji goes back to the one chosen from the name. Emoji sequences (flags, skin tones) run to a
        # handful of characters; anything much longer isn't one.
        emoji = emoji.strip()[:16] or None
        try:
            with conn:
                conn.execute("UPDATE categories SET name = ?, parent_id = ?, excluded = ?, emoji = ?, color = ? WHERE id = ?",
                             (name.strip(), parent, int(bool(excluded)), emoji, color if color in looks.COLORS else None, cid))
        except sqlite3.IntegrityError:
            return redirect(back, f"There's already a category called {name.strip()}.")
        return redirect(back, "Category saved.")

    @app.post("/categories/{cid}/delete")
    def delete_category(cid: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
        return redirect("/categories", "Category deleted. Its transactions are now uncategorized.")

    @app.post("/plaid-map")
    async def save_plaid_map(request: Request, conn=Depends(get_db)):
        form = await request.form()
        with conn:
            for key, value in form.items():
                if not key.startswith("map_"):
                    continue
                code, category = key[4:], optional_int(str(value))
                if category is None:
                    conn.execute("DELETE FROM plaid_category_map WHERE plaid_category = ?", (code,))
                else:
                    conn.execute(
                        "INSERT INTO plaid_category_map (plaid_category, category_id) VALUES (?, ?) "
                        "ON CONFLICT (plaid_category) DO UPDATE SET category_id = excluded.category_id",
                        (code, category),
                    )
        return redirect("/rules#plaid", "Plaid category mapping saved. It applies to new transactions.")

    @app.post("/rules")
    def add_rule(pattern: str = Form(...), match: str = Form("contains"), category_id: str = Form(""),
                 type: str = Form(""), rename_to: str = Form(""), apply: str = Form(""), conn=Depends(get_db)):
        if not pattern.strip() or match not in ("equals", "contains"):
            return redirect("/rules#rules", "A rule needs some text to match.")
        category, type_, rename_to = optional_int(category_id), type if type in categorize.TYPES else None, rename_to.strip()
        if category is None and type_ is None and not rename_to:
            return redirect("/rules#rules", "Pick what the rule does: a category, a type or a new name.")
        with conn:
            rule_id = categorize.add_rule(conn, pattern, match, category, type_, rename_to)
            changed = categorize.apply_rule(conn, rule_id) if apply else 0
        return redirect("/rules#rules", "Rule saved." + (f" Updated {changed} transactions." if apply else ""))

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return redirect("/rules#rules", "Rule deleted.")

    # ---- tags ----

    @app.post("/tags")
    def add_tag(name: str = Form(...), color: str = Form(""), conn=Depends(get_db)):
        name = tags.clean_name(name)
        if not name:
            return redirect("/rules#tags", "Give the tag a name.")
        with conn:
            tags.get_or_create(conn, name, color)
        return redirect("/rules#tags", f"Added the tag {name}.")

    @app.post("/tags/{gid}")
    def save_tag(gid: int, name: str = Form(...), color: str = Form(""), conn=Depends(get_db)):
        name = tags.clean_name(name)
        if not name:
            return redirect("/rules#tags", "Give the tag a name.")
        try:
            with conn:
                conn.execute("UPDATE tags SET name = ?, color = ? WHERE id = ?", (name, color if color in looks.COLORS else None, gid))
        except sqlite3.IntegrityError:
            return redirect("/rules#tags", f"There's already a tag called {name}.")
        return redirect("/rules#tags", "Tag saved.")

    @app.post("/tags/{gid}/delete")
    def delete_tag(gid: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM tags WHERE id = ?", (gid,))
        return redirect("/rules#tags", "Tag deleted. Its transactions are still there, without it.")

    @app.post("/tags/{gid}/remove")
    def remove_tag(gid: int, ids: list[int] = Form([]), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Takes tag `gid` off the transactions `ids`."""
        tag = conn.execute("SELECT name FROM tags WHERE id = ?", (gid,)).fetchone()
        if tag is None:
            raise HTTPException(404, "Tag not found.")
        with conn:
            removed = tags.remove(conn, gid, ids)
        return redirect(safe_back(back, "/transactions"), f"Took “{tag['name']}” off {plural(removed, 'transaction')}.")

    # ---- import ----

    def import_path(name: str) -> Path:
        path = (settings.import_dir / name).resolve()
        if path.parent != settings.import_dir.resolve() or path.suffix.lower() != ".csv" or not path.is_file():
            raise HTTPException(404, "File not found in the import folder.")
        return path

    @app.get("/import")
    def import_page(request: Request, conn=Depends(get_db)):
        files = sorted(settings.import_dir.glob("*.csv")) if settings.import_dir.is_dir() else []
        return render(
            request, conn, "import.html", files=[f.name for f in files],
            imported=get_meta(conn, "copilot_imported_at"),
            connected=conn.execute("SELECT count(*) FROM plaid_items").fetchone()[0],
        )

    @app.post("/import/upload")
    async def import_upload(upload: UploadFile = File(...)):
        name = Path(upload.filename or "copilot-export.csv").name
        if not name.lower().endswith(".csv"):
            return redirect("/import", "Choose a .csv file.")
        settings.import_dir.mkdir(parents=True, exist_ok=True)
        (settings.import_dir / name).write_bytes(await upload.read())
        return redirect(f"/import/preview?{urlencode({'file': name})}")

    @app.get("/import/preview")
    def import_preview(request: Request, file: str, conn=Depends(get_db)):
        try:
            parsed = copilot_import.parse(import_path(file).read_text(encoding="utf-8-sig", errors="replace"))
        except copilot_import.CopilotImportError as exc:
            return redirect("/import", f"Couldn't read {file}: {exc}")
        return render(request, conn, "import_preview.html", file=file, s=copilot_import.summarize(parsed))

    @app.post("/import/copilot")
    def import_copilot(file: str = Form(...), flip: str = Form(""), conn=Depends(get_db)):
        try:
            parsed = copilot_import.parse(import_path(file).read_text(encoding="utf-8-sig", errors="replace"))
        except copilot_import.CopilotImportError as exc:
            return redirect("/import", f"Couldn't read {file}: {exc}")
        if flip:
            parsed.sign = -parsed.sign
        r = copilot_import.import_parsed(conn, parsed)
        parts = [f"Imported {r.added} transactions"]
        if r.duplicates:
            parts.append(f"skipped {r.duplicates} already imported")
        if r.pending_skipped:
            parts.append(f"skipped {r.pending_skipped} pending")
        parts.append(f"{r.accounts_created} accounts, {r.categories_created} categories, {r.rules_created} rules and {r.recurring_created} recurring charges created")
        return redirect("/import", ", ".join(parts) + ".")

    # ---- settings and export ----

    @app.get("/settings")
    def settings_page(request: Request, conn=Depends(get_db)):
        return render(
            request, conn, "settings.html",
            counts={
                "transactions": conn.execute("SELECT count(*) FROM transactions").fetchone()[0],
                "accounts": conn.execute("SELECT count(*) FROM accounts").fetchone()[0],
                "rules": conn.execute("SELECT count(*) FROM rules").fetchone()[0],
            },
            backups=backup.backup_summary(settings.backup_dir),
            name_work={"changes": len(names.cleanup(conn)), "offers": len(names.learned(conn))},
        )

    @app.post("/theme")
    def set_theme(theme: str = Form(""), back: str = Form("/")):
        """Shows the app light or dark (`theme`), or as the system does ("system"), from now on. With JavaScript the page
        changes at once (app.js), which sets the same cookie."""
        response = redirect(safe_back(back))
        if theme in ("light", "dark"):
            response.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400, samesite="lax")
        else:
            response.delete_cookie(THEME_COOKIE)
        return response

    @app.get("/names")
    def names_page(request: Request, conn=Depends(get_db)):
        return render(request, conn, "names.html", offers=names.learned(conn), changes=names.cleanup(conn))

    @app.post("/names/rules")
    def learn_names(patterns: list[str] = Form([]), conn=Depends(get_db)):
        """Makes the renaming rules picked from those the names page offers (names.learned)."""
        offers = {o["pattern"]: o for o in names.learned(conn)}
        picked = [offers[p] for p in dict.fromkeys(patterns) if p in offers]
        if not picked:
            return redirect("/names", "Tick the rules to make.")
        with conn:
            for o in picked:
                categorize.add_rename(conn, o["shown"], o["name"])
        return redirect("/names", f"Made {plural(len(picked), 'rule')}: new transactions come named like your earlier ones.")

    @app.post("/names/clean")
    def clean_names(conn=Depends(get_db)):
        with conn:
            renamed = names.clean_up(conn)
        return redirect("/names", f"Renamed {plural(renamed, 'transaction')}. What your bank sent stays under “From your bank” on each one.")

    @app.get("/export.csv")
    def export_csv(conn=Depends(get_db)):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "name", "amount", "category", "parent category", "type", "excluded", "pending",
                         "account", "account mask", "notes", "recurring", "original name", "tags"])
        # A split transaction is written as its parts, which add up to it.
        rows = conn.execute(
            f"""SELECT t.*, a.name AS account, a.mask, c.name AS category, p.name AS parent, rc.name AS recurring
                FROM transactions t JOIN accounts a ON a.id = t.account_id
                LEFT JOIN categories c ON c.id = t.category_id LEFT JOIN categories p ON p.id = c.parent_id
                LEFT JOIN recurring rc ON rc.id = t.recurring_id
                WHERE {reports.NOT_SPLIT} ORDER BY t.date, t.id"""
        ).fetchall()
        tag_names = {g: look["name"] for g, look in tags.tag_looks(conn).items()}
        tagged = tags.tags_by_transaction(conn, [r["id"] for r in rows])
        for r in rows:
            writer.writerow([r["date"], csv_text(r["name"]), f"{r['amount']:.2f}", csv_text(r["category"]), csv_text(r["parent"]),
                             r["type"], r["excluded"], r["pending"], csv_text(r["account"]), r["mask"] or "", csv_text(r["notes"]),
                             csv_text(r["recurring"]), csv_text(r["original_name"]),
                             csv_text(", ".join(sorted(tag_names[g] for g in tagged.get(r["id"], []))))])
        filename = f"monthlight-export-{date.today().isoformat()}.csv"
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---- queries used by several pages -----------------------------------------

# Transactions with the purchase each payback belongs to (`p`), which lends it its date and category (reports.DAY), the
# other side of a move between accounts (`ot`), and the transaction a part is split from (`sp`).
_LISTED = """transactions t LEFT JOIN transactions p ON p.id = t.reimburses_id LEFT JOIN transactions ot ON ot.id = t.transfer_id
             LEFT JOIN transactions sp ON sp.id = t.split_from_id"""


def _linked_day(r: str, own: str = "") -> str:
    # The day of the biggest transaction linked with {r} (links.py) in the month of {own} ({r} unless given), else
    # {own}'s date. Kept to the month, a transaction is listed in the month it counts in.
    own = own or r
    return (f"coalesce((SELECT h.date FROM transactions h WHERE h.link_id = {r}.link_id "
            f"AND substr(h.date, 1, 7) = substr({own}.date, 1, 7) ORDER BY abs(h.amount) DESC, h.id LIMIT 1), {own}.date)")


# The day a transaction is listed on: money that came in through a move between the user's accounts on the day it went
# out, under it; a payback on its purchase's day; and transactions linked together on the biggest one's day, in the
# same month, the parts of a linked split with it. Each still shows its own date. Moves count nowhere, and paybacks
# count on their purchase's day (reports.DAY); a linked transaction counts on its own date, but in the month it's
# listed in.
SHOWN_DAY = f"""CASE WHEN t.type = 'transfer' AND t.amount > 0 AND ot.id IS NOT NULL THEN ot.date
                     WHEN p.id IS NOT NULL THEN {_linked_day('p')} WHEN sp.id IS NOT NULL THEN {_linked_day('sp', 't')}
                     ELSE {_linked_day('t')} END"""


def account_labels(conn) -> dict[int, str]:
    """Returns each account's name as lists show it, by id: with its last digits when another account has that name."""
    rows = conn.execute("SELECT id, name, mask FROM accounts").fetchall()
    names = Counter(r["name"] for r in rows)
    return {r["id"]: f"{r['name']} ••{r['mask']}" if names[r["name"]] > 1 and r["mask"] else r["name"] for r in rows}


def merchant_names(conn, limit: int = 400) -> list[str]:
    """Returns the names spending goes by, the most used first, one spelling each, for a field that suggests them."""
    return [r[0] for r in conn.execute(
        "SELECT max(name) FROM transactions WHERE type = 'regular' AND amount < 0 GROUP BY lower(trim(name)) ORDER BY count(*) DESC LIMIT ?",
        (limit,))]


def sidebar_groups(conn, folded: set[str]) -> list[dict]:
    """Returns the sidebar's accounts by group: each group's "kind", "label", "balance", whether it's "folded" (its kind
    is in `folded`), and its "accounts" in use (not hidden or archived), with their balances shown the way the Accounts
    page shows them. Each account's "label" is its name, with its last digits when another one has the same name."""
    groups = []
    for g in reports.net_worth(conn)["groups"]:
        shown = [a for a in g["accounts"] if not a["hidden"] and not a["archived"]]
        known = [a["balance"] for a in shown if a["balance"] is not None]
        if shown:  # NOTE: its balance adds up what's listed; the Accounts page's counts archived accounts too
            groups.append({"kind": g["kind"], "label": g["label"], "folded": g["kind"] in folded, "accounts": shown,
                           "balance": round(sum(known), 2) if known else None})
    names = Counter(a["name"] for g in groups for a in g["accounts"])
    for a in (a for g in groups for a in g["accounts"]):
        a["label"] = f"{a['name']} ••{a['mask']}" if names[a["name"]] > 1 and a["mask"] else a["name"]
    return groups


def listed_months(conn, **criteria) -> list[tuple[str, int]]:
    """Returns (month, how many) for each month with transactions matching `criteria`, newest first, by the day each
    is listed on (SHOWN_DAY); see transaction_filter for the criteria."""
    clause, params = transaction_filter(**criteria)
    return [tuple(r) for r in conn.execute(
        f"SELECT substr({SHOWN_DAY}, 1, 7) AS m, count(*) FROM {_LISTED} WHERE {clause} GROUP BY m ORDER BY m DESC", params)]


def month_chunk(months: list[tuple[str, int]], newest: str, oldest: str | None = None) -> list[str]:
    """Returns the months of `months` ((month, how many), newest first) to list together, newest first, from `newest`:
    - through `oldest`, when it's given
    - else as many as it takes to list PAGE_SIZE transactions or more"""
    chunk, listed = [], 0
    for m, n in months:
        if m > newest or (oldest and m < oldest) or (not oldest and listed >= PAGE_SIZE):
            continue
        chunk.append(m)
        listed += n
    return chunk


def by_month(days: list[dict]) -> list[dict]:
    """Returns days from group_by_day, newest first, grouped as [{"month", "days"}]."""
    months: list[dict] = []
    for day in days:
        if not months or months[-1]["month"] != day["date"][:7]:
            months.append({"month": day["date"][:7], "days": []})
        months[-1]["days"].append(day)
    return months


def find_transactions(conn, *, limit: int = PAGE_SIZE, offset: int = 0, by_amount: bool = False, **criteria) -> tuple[list[dict], int]:
    """Returns (transactions matching `criteria`, total number matching); see transaction_filter for the criteria.
    They come newest day first and, within a day, biggest amount first, money in and out alike, then by name; the parts
    of a split follow one another where the split's total puts them. `by_amount`: biggest amount first, then newest.
    Each transaction has "tags", its tags from tags.tag_looks, and "links", how many others it's linked with (links.py);
    a part also has "split_links", how many its split is linked with."""
    clause, params = transaction_filter(**criteria)
    # Not every bank gives the time of day, so a day goes by amount, then name. A split goes as the row for it above its
    # parts (with_split_heads), by its total and name, and its parts follow it by their own amounts.
    order = ("abs(t.amount) DESC, shown_date DESC, t.name COLLATE NOCASE, t.id DESC" if by_amount else
             """shown_date DESC, abs(coalesce(sp.amount, t.amount)) DESC, coalesce(sp.name, t.name) COLLATE NOCASE,
                coalesce(t.split_from_id, t.id) DESC, abs(t.amount) DESC, t.name COLLATE NOCASE, t.id DESC""")
    rows = conn.execute(
        f"""SELECT t.*, a.name AS account_name, a.mask AS account_mask, a.hidden AS account_hidden,
                   c.name AS category_name, c.excluded AS category_excluded, r.name AS recurring_name,
                   {SHOWN_DAY} AS shown_date, {reports.CATEGORY} AS shown_category_id,
                   p.name AS purchase_name, p.date AS purchase_date,
                   (SELECT sum(b.amount) FROM transactions b WHERE b.reimburses_id = t.id) AS paid_back,
                   (SELECT oa.name FROM transactions o JOIN accounts oa ON oa.id = o.account_id WHERE o.id = t.transfer_id) AS other_account,
                   sp.amount AS split_total, sp.pending AS split_pending, sp.name AS split_name,
                   (SELECT count(*) FROM transactions s WHERE s.split_from_id = t.split_from_id) AS split_count,
                   (SELECT sum(s.amount) FROM transactions s WHERE s.split_from_id = t.split_from_id) AS split_siblings_sum,
                   (SELECT count(*) FROM transactions s WHERE s.split_from_id = t.split_from_id AND s.excluded = 1) AS split_excluded,
                   (SELECT count(*) FROM transactions s WHERE s.split_from_id = t.id) AS split_parts,
                   (SELECT sum(s.amount) FROM transactions s WHERE s.split_from_id = t.id) AS split_sum,
                   (SELECT count(*) FROM transactions l WHERE l.link_id = t.link_id AND l.id != t.id) AS links,
                   sp.link_id AS split_link_id, (SELECT count(*) FROM transactions l WHERE l.link_id = sp.link_id AND l.id != sp.id) AS split_links
            FROM {_LISTED} JOIN accounts a ON a.id = t.account_id
            LEFT JOIN categories c ON c.id = {reports.CATEGORY}
            LEFT JOIN recurring r ON r.id = t.recurring_id
            WHERE {clause}
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    total = conn.execute(f"SELECT count(*) FROM {_LISTED} WHERE {clause}", params).fetchone()[0]
    looks_by_id = tags.tag_looks(conn)
    rank = {g: i for i, g in enumerate(looks_by_id)}  # tag_looks is ordered by name
    tagged = tags.tags_by_transaction(conn, [r["id"] for r in rows])
    return [dict(r, tags=[looks_by_id[g] for g in sorted(tagged.get(r["id"], []), key=rank.get)]) for r in rows], total


def listed_totals(conn, **criteria) -> dict:
    """Returns what the transactions matching `criteria` add up to, counted as the dashboard counts them (reports.py):
    {"spent", "income", "net"}, and "moved", the money that went to the user's other accounts, which counts nowhere."""
    clause, params = transaction_filter(**criteria)
    counted = "t.excluded = 0 AND coalesce(p.excluded, 0) = 0 AND a.hidden = 0"
    row = conn.execute(
        f"""SELECT coalesce(-sum(CASE WHEN t.type = 'regular' AND {counted} AND coalesce(c.excluded, 0) = 0 THEN t.amount END), 0) AS spent,
                   coalesce(sum(CASE WHEN t.type = 'income' AND {counted} THEN t.amount END), 0) AS income,
                   coalesce(-sum(CASE WHEN t.type = 'transfer' AND t.amount < 0 THEN t.amount END), 0) AS moved
            FROM {_LISTED} JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = {reports.CATEGORY}
            WHERE {clause} AND {reports.NOT_SPLIT}""",
        params,
    ).fetchone()
    spent, income = round(row["spent"], 2), round(row["income"], 2)
    return {"spent": spent, "income": income, "net": round(income - spent, 2), "moved": round(row["moved"], 2)}


def transaction_filter(*, dates: tuple[str, str] | None = None, between: tuple[str, str] | None = None,
                       accounts: list[int] = (), categories: list[str] = (), types: list[str] = (), tags: list[int] = (),
                       amount: tuple | None = None, q: str | None = None, review: str | bool = "",
                       hide_transfers: bool = False, linked_transfers: bool = False) -> tuple[str, list]:
    """Returns the SQL condition on transactions `t` (with `p` and `ot`, see _LISTED) for these criteria and its parameters.
    - `dates` and `between` (first day, day after the last) go by the day a transaction is listed on (SHOWN_DAY)
    - `accounts`, `categories`, `types` and `tags`: any of them. Category "none" means regular transactions waiting for
      a category (income and transfers don't take one, and excluded ones have none, db.py); "excluded", the excluded
      ones. A payback goes by its purchase's category.
    - `amount` is (lowest, highest) from amount_range, money in and out alike
    - `review`: "1" (or True) those waiting for review, among them a transaction split into parts whose parts need a
      look, which shows only then (otherwise its parts show instead); "done" those reviewed
    - `hide_transfers` leaves out moves between the user's accounts whose two sides are linked; `linked_transfers`
      keeps only those"""
    waiting = review is True or review == "1"
    where, params = ["1 = 1"], []
    if not waiting:
        where.append(reports.NOT_SPLIT)
    if tags:
        where.append(f"EXISTS (SELECT 1 FROM transaction_tags tt WHERE tt.transaction_id = t.id AND tt.tag_id IN ({','.join('?' * len(tags))}))")
        params += list(tags)
    for start, end in [span for span in (dates, between) if span]:
        where.append(f"{SHOWN_DAY} >= ? AND {SHOWN_DAY} < ?")
        params += [start, end]
    if accounts:
        where.append(f"t.account_id IN ({','.join('?' * len(accounts))})")
        params += list(accounts)
    if categories:
        ids = [int(c) for c in categories if c.isdigit()]
        either = [f"{reports.CATEGORY} IN ({','.join('?' * len(ids))})"] if ids else []
        params += ids
        if "none" in categories:
            either.append(f"({reports.CATEGORY} IS NULL AND t.type = 'regular' AND t.excluded = 0 AND coalesce(p.excluded, 0) = 0)")
        if "excluded" in categories:
            either.append("t.excluded = 1")
        where.append(f"({' OR '.join(either)})")
    if types:
        where.append(f"t.type IN ({','.join('?' * len(types))})")
        params += list(types)
    if amount:
        low, high = amount
        if low is not None:
            where.append("abs(t.amount) >= ?")
            params.append(low - 0.005)
        if high is not None:
            where.append("abs(t.amount) <= ?")
            params.append(high + 0.005)
    if q:
        where.append("(instr(lower(t.name), ?) > 0 OR instr(lower(coalesce(t.original_name, '')), ?) > 0 OR instr(lower(coalesce(t.notes, '')), ?) > 0)")
        params += [q.lower()] * 3
    if waiting:
        where.append(f"(t.reviewed = 0 OR {reports.SPLIT_OFF})")
    elif review == "done":
        where.append("t.reviewed = 1")
    linked = "(t.type = 'transfer' AND t.transfer_id IS NOT NULL)"
    if hide_transfers:
        where.append(f"NOT {linked}")
    if linked_transfers:
        where.append(linked)
    return " AND ".join(where), params


def group_by_day(rows, today: date) -> list[dict]:
    """Returns transactions (in find_transactions' order) grouped by date as [{"date", "label", "spent", "rows"}], where
    "spent" is the day's spending among them, counted as on the dashboard. Within a day, amounts that cancel out sit
    together (see offsetting_together), so do transactions linked together, as equals (linked_together, chained), and
    the parts of a split sit together under a row for it (parts_together, with_split_heads)."""
    groups: list[dict] = []
    for t in rows:
        if not groups or groups[-1]["date"] != t["shown_date"]:
            groups.append({"date": t["shown_date"], "label": day_label(t["shown_date"], today), "spent": 0.0, "rows": []})
        group = groups[-1]
        group["rows"].append(t)
        if t["type"] == "regular" and not (t["excluded"] or t["category_excluded"] or t["account_hidden"] or t.get("split_parts")):
            group["spent"] = round(group["spent"] - t["amount"], 2)
    for group in groups:
        group["rows"] = chained(joined_to_their_link(with_split_heads(paybacks_under_purchases(parts_together(
            linked_together(offsetting_together(group["rows"])))))))
    return groups


def joined_to_their_link(rows: list) -> list[dict]:
    """Returns the rows as dicts with "joined" set on one that belongs to the run headed by an earlier row: the other
    side of its move, its purchase, or its split (a part under the split's row), or a payback of a row already in the
    run. "more" is set when the next row joins the same run, so the list can draw them as a group, all at one level."""
    out = [dict(t, joined=False, more=False) for t in rows]
    head, run = None, set()
    for t in out:
        if head and (t["reimburses_id"] in run or t["transfer_id"] in run or (t.get("split_from_id") and t["split_from_id"] == head["id"])):
            t["joined"] = True
            run.add(t["id"])
        else:
            head, run = t, {t["id"]}
    for t, after in pairwise(out):
        t["more"] = t["joined"] and after["joined"]
    return out


def chained(rows: list[dict]) -> list[dict]:
    """Returns the rows with what the list needs to draw one line through transactions linked together (links.py),
    which are equals, from the first of them to the last: "chain_down" on each but the last, "chain_up" on each but
    the first, and "chain_through" on a row between them that hangs under one of them (its payback)."""
    first, last = {}, {}
    for i, t in enumerate(rows):
        t.update(chain_up=False, chain_down=False, chain_through=False)
        if t.get("links"):
            first.setdefault(t["link_id"], i)
            last[t["link_id"]] = i
    for key, start in first.items():
        for i in range(start, last[key] + 1):
            t = rows[i]
            if t.get("links") and t["link_id"] == key:
                t["chain_up"], t["chain_down"] = i > start, i < last[key]
            else:
                t["chain_through"] = True
    return rows


def parts_together(rows: list) -> list:
    """Returns one day's transactions in their order, except that the parts of a split follow one another, in their
    order: right under the split's own row when that's listed (as on the review list), else where the first of them
    shows up."""
    listed = {t["id"] for t in rows}
    ordered, placed = [], set()
    for t in rows:
        if t["id"] in placed or t.get("split_from_id") in listed:
            continue  # a part whose split is listed goes under it
        if t.get("split_from_id"):
            group = [r for r in rows if r.get("split_from_id") == t["split_from_id"]]
        else:
            group = [t] + [r for r in rows if r.get("split_from_id") == t["id"]]
        placed.update(r["id"] for r in group)
        ordered += group
    return ordered


def _link_key(t) -> int | None:
    # What a row is linked with (links.py): its own link, or for a part, its split's.
    if t.get("links"):
        return t["link_id"]
    return t["split_link_id"] if t.get("split_links") else None


def linked_together(rows: list) -> list:
    """Returns one day's transactions in their order, except that transactions linked together (links.py), with the
    parts of a split that's linked, follow one another where the first of them shows up, biggest first (a part by its
    split's total); of two the same size, the one dated that day first."""
    size = lambda r: abs(r["split_total"] if r.get("split_from_id") else r["amount"])
    ordered, placed = [], set()
    for t in rows:
        if t["id"] in placed:
            continue
        key, group = _link_key(t), [t]
        if key is not None:
            group = sorted((r for r in rows if _link_key(r) == key), key=lambda r: (-size(r), r["date"] != r["shown_date"]))
        placed.update(r["id"] for r in group)
        ordered += group
    return ordered


def with_split_heads(rows: list) -> list:
    """Returns one day's transactions with a row for each split put above its first part there (unless the split's own
    row is listed, as on the review list): a dict with "kind": "split", the split's "id", "name" and total "amount",
    how many parts it has ("split_count") and how many of them are excluded ("split_excluded"), what they add up to, and
    what it's linked with ("link_id", "links"). Each part gets "under_head", except a part paying back a purchase that's
    listed there, or listed on its purchase's day: it stays a payback, with no row for its split."""
    listed = {t["id"] for t in rows}
    headed: set[int] = set()
    out = []
    for t in rows:
        split = t.get("split_from_id")
        # Its split shows on its own day; here it's with the purchase it pays back (paybacks_under_purchases), or on
        # that purchase's day while the purchase isn't listed, say in a search.
        if split and t.get("reimburses_id") and (t["reimburses_id"] in listed or t.get("shown_date", t.get("date")) != t.get("date")):
            out.append(t)
            continue
        if split and split not in listed and split not in headed:
            headed.add(split)
            out.append({"kind": "split", "id": split, "name": t["split_name"], "amount": t["split_total"], "split_count": t["split_count"],
                        "split_excluded": t.get("split_excluded"),
                        "split_siblings_sum": t["split_siblings_sum"], "pending": t["split_pending"], "account_name": t["account_name"],
                        "account_mask": t["account_mask"], "account_id": t["account_id"], "type": t["type"],
                        "reimburses_id": None, "transfer_id": None, "split_from_id": None,
                        "link_id": t.get("split_link_id"), "links": t.get("split_links")})
        out.append(dict(t, under_head=bool(split)) if split else t)
    return out


def paybacks_under_purchases(rows: list) -> list:
    """Returns one day's transactions with each payback moved right under the purchase it pays back, when that
    purchase is among them."""
    ids = {t["id"] for t in rows}
    moved = sorted((t for t in rows if t["reimburses_id"] in ids), key=lambda t: (t["date"], t["id"]))  # in the order they came
    ordered = []
    for t in rows:
        if t["reimburses_id"] in ids:
            continue
        ordered.append(t)
        ordered += [b for b in moved if b["reimburses_id"] == t["id"]]
    return ordered



def offsetting_together(rows: list) -> list:
    """Returns one day's transactions in their order, except that one that cancels out a later one (the same amount
    in and out, like the two sides of a transfer, or a refund) is followed right away by that other half. A transfer
    linked to its other side (transfer_id) pairs only with that one, and the money going out comes first, with the
    side it went to under it. A part of a split, or a transaction linked with others (links.py), stays with its group:
    it pairs only with a linked other side."""
    placed: set[int] = set()
    ordered = []
    for i, t in enumerate(rows):
        if i in placed:
            continue
        placed.add(i)
        ordered.append(t)
        later = [j for j in range(i + 1, len(rows)) if j not in placed]
        if t["transfer_id"]:
            halves = [j for j in later if rows[j]["id"] == t["transfer_id"]]
        elif t.get("split_from_id") or t.get("links"):
            halves = []
        else:
            halves = [j for j in later if not (rows[j]["transfer_id"] or rows[j].get("split_from_id") or rows[j].get("links"))
                      and t["amount"] and abs(rows[j]["amount"] + t["amount"]) < 0.005]
        if halves:
            # Another account first (a move between accounts), then the same type (both transfers), then the nearest.
            j = min(halves, key=lambda j: (rows[j]["account_id"] == t["account_id"], rows[j]["type"] != t["type"], j))
            placed.add(j)
            ordered[-1:] = [rows[j], t] if t["transfer_id"] and t["amount"] > 0 else [t, rows[j]]
    return ordered


# ---- charts -------------------------------------------------------------------

def budget_rows(conn, nodes: list[dict], month: str, today: date) -> list[dict]:
    """Returns category_spending's `nodes`, each (and each subcategory) with what the Categories page shows beside it:
    "expected", the recurring charges in it still to come this month (a group's with its subcategories'), and in the
    month under way "pace": how far through the month it is ("percent") and how far spending is ahead of that share of
    what it has to spend ("ahead", below zero when it's behind)."""
    current = month == reports.current_month(today)
    groups = reports.category_groups(conn)
    due: Counter = Counter()
    if current:
        for r in recurring.month_summary(conn, recurring.list_recurring(conn, today), today)["due_items"]:
            due[r["category_id"]] -= r["amount"]
            if r["category_id"] is not None and groups.get(r["category_id"]) not in (None, r["category_id"]):
                due[("group", groups[r["category_id"]])] -= r["amount"]
    share = today.day / calendar.monthrange(*map(int, month.split("-")))[1] if current else None
    for node in nodes:
        for n in [node, *node["children"]]:
            n["expected"] = round(due.get(n["id"], 0.0) + (due.get(("group", n["id"]), 0.0) if n is node else 0.0), 2)
            n["pace"] = ({"percent": share * 100, "ahead": round(n["spent"] - n["total_budget"] * share, 2)}
                         if share is not None and n["total_budget"] else None)
    return nodes


def category_view(conn, cat, month: str, today: date) -> dict:
    """Returns what a category's page shows for `month`:
    - the month: spent, its budget, what rolls over and what's left (category_spending), with expected charges and pace
    - its last 12 months as columns, with its budget and the average of the months that are over, and the year so far
    - its top merchants over those months, and the month's transactions by day"""
    this_month = reports.current_month(today)
    family = [cat["id"]] + [r[0] for r in conn.execute("SELECT id FROM categories WHERE parent_id = ? AND excluded = 0", (cat["id"],))]
    nodes = budget_rows(conn, reports.category_spending(conn, month), month, today)
    node = next((n for top in nodes for n in [top, *top["children"]] if n["id"] == cat["id"]), None)
    if node is None:  # nothing spent and no budget this month
        budget = reports.effective_budgets(conn, month).get(cat["id"])
        node = {"spent": 0.0, "budget": budget, "carry": 0.0, "total_budget": budget, "expected": 0.0, "pace": None,
                "rollover": cat["rollover_since"] if cat["rollover_since"] and cat["rollover_since"] <= month else None}
    first = reports.shift_month(month, -11)
    history = conn.execute("SELECT min(date) FROM transactions").fetchone()[0] or reports.month_range(month)[0]
    months = [m for m in month_list(first, month) if m >= history[:7]] or [month]
    spent = reports.spending_by_category(conn, reports.month_range(months[0])[0], reports.month_range(month)[1])
    values = [round(sum(spent.get((m, c), 0.0) for c in family), 2) + 0.0 for m in months]
    done = [v for m, v in zip(months, values) if m < this_month]
    average = round(sum(done) / len(done), 2) if done else None
    year = reports.spending_by_category(conn, f"{month[:4]}-01-01", reports.month_range(month)[1])
    geometry = charts.bar_heights(values + [node["budget"] or 0.0, average or 0.0])
    high = geometry["high"]
    bars = [{"month": m, "spent": v, "height": h, "selected": m == month, "tick": datetime.strptime(m, "%Y-%m").strftime("%b"),
             "year": m[:4] if i == 0 or m.endswith("-01") else ""} for i, (m, v, h) in enumerate(zip(months, values, geometry["heights"]))]
    markers = [{"label": label, "value": value, "top": charts.y_percent(value, 0.0, high)}
               for label, value in (("Budget", node["budget"]), ("Average", average)) if value]  # NOTE: a month's own, without what rolled over
    merchants: dict[str, dict] = {}
    for r in reports.counted(conn, reports.month_range(months[0])[0], reports.month_range(month)[1]):
        if r["category_id"] in family:
            m = merchants.setdefault(reports.merchant_key(r["merchant"]), {"name": r["merchant"], "total": 0.0, "count": 0})
            m["total"] += r["value"]
            m["count"] += 1
    top = sorted((m for m in merchants.values() if m["total"] > 0), key=lambda m: -m["total"])[:8]
    for m in top:
        m.update(total=round(m["total"], 2), share=m["total"] / top[0]["total"] * 100)
    rows, count = find_transactions(conn, dates=reports.month_range(month), categories=[str(c) for c in family], limit=-1)
    return {"look": looks.category_looks(conn).get(cat["id"], looks.UNCATEGORIZED), "node": node, "family": family,
            "chart": {"bars": bars, "ticks": geometry["ticks"], "markers": markers}, "average": average, "months_counted": len(done),
            "year_spent": round(sum(v for (_, c), v in year.items() if c in family), 2) + 0.0,
            "top_merchants": top, "days": group_by_day(rows, today), "count": count,
            "budget_now": reports.effective_budgets(conn, month).get(cat["id"])}


def review_view(conn, month: str, today: date) -> dict:
    """Returns what a month's review shows:
    - spent against the budget, income, the share of income kept, and each compared with the month before
    - the categories and merchants whose spending changed most from the month before, and the biggest expenses
    - the budgets kept and those gone over, everyday spending when some is set aside, and net worth at the start
      and end of the month"""
    before = reports.shift_month(month, -1)
    (start, end), (p_start, p_end) = reports.month_range(month), reports.month_range(before)
    now, then = reports.totals(conn, start, end), reports.totals(conn, p_start, p_end)
    nodes = reports.category_spending(conn, month)
    groups = reports.category_groups(conn)
    rows_now, rows_then = reports.counted(conn, start, end), reports.counted(conn, p_start, p_end)

    def changed(group: str) -> list[dict]:
        """The series of `group` whose spending changed most, biggest change first, with this month's and the last."""
        totals = {}
        for when, rows in (("now", rows_now), ("then", rows_then)):
            for r in rows:
                for key in reports.keys_of(r, group, groups):
                    entry = totals.setdefault(key, {"key": key, "now": 0.0, "then": 0.0, "names": Counter()})
                    entry[when] += r["value"]
                    entry["names"][" ".join((r["merchant"] or "").split())] += 1
        out = sorted(({**e, "now": round(e["now"], 2), "then": round(e["then"], 2), "diff": round(e["now"] - e["then"], 2)}
                      for e in totals.values()), key=lambda e: (-abs(e["diff"]), e["key"]))
        out = [e for e in out if abs(e["diff"]) >= 0.01][:6]
        widest = max((abs(e["diff"]) for e in out), default=0) or 1
        names = dict(conn.execute("SELECT id, name FROM categories").fetchall())
        for e in out:
            e["share"] = abs(e["diff"]) / widest * 50  # of the half of the bar on its side
            if group == "category":
                e["label"] = names.get(int(e["key"]), "A category") if e["key"].isdigit() else "Uncategorized"
                e["category_id"] = int(e["key"]) if e["key"].isdigit() else None
            else:
                e["label"] = e["names"].most_common(1)[0][0] if e["names"] else e["key"]
        return out

    budgeted = [n for n in nodes if n["total_budget"]]
    history = [(day, value) for day, value in reports.net_worth_history(conn) if day < end]
    worth_start = next((value for day, value in reversed(history) if day < start), None)
    in_month = [(day, value) for day, value in history if day >= start]
    worth_end = in_month[-1][1] if in_month else worth_start
    aside = reports.everyday(conn)
    everyday = reports.totals(conn, start, end, aside)["spent"] if any(aside[k] for k in reports.LEAVE_OUTS) or aside["over"] else None
    this_month = reports.current_month(today)
    return {
        "month": month, "before": before, "now": now, "then": then, "budget": reports.budget_total(nodes),
        "spent_change": change(now["spent"], then["spent"]), "income_change": change(now["income"], then["income"]),
        "categories": changed("category"), "merchants": changed("merchant"),
        "biggest": reports.biggest(conn, start, end, limit=5),
        "kept": [n for n in budgeted if n["spent"] <= n["total_budget"]], "over": [n for n in budgeted if n["spent"] > n["total_budget"]],
        "everyday": everyday, "worth_start": worth_start, "worth_end": worth_end,
        "worth_chart": worth_chart(([(day, value) for day, value in history if day < start][-1:] + in_month) if in_month else []),
        "under_way": month == this_month, "previous": before, "next": reports.shift_month(month, 1) if month < this_month else None,
        "count": len(rows_now),
    }


def column_labels(column: str, first: bool) -> dict:
    """Returns how a report's column (reports.column_of) is named: "tick" under its bar, "year" under that on the first
    one and at each new year, and "label" in full, like "March 2026", "Q1 2026" or "2026"."""
    if "-Q" in column:
        return {"tick": column[5:], "year": column[:4] if first or column.endswith("Q1") else "", "label": f"{column[5:]} {column[:4]}"}
    if len(column) == 4:
        return {"tick": column, "year": "", "label": column}
    return {"tick": datetime.strptime(column, "%Y-%m").strftime("%b"), "year": column[:4] if first or column.endswith("-01") else "",
            "label": month_label(column)}


def series_looks(conn, group: str, series: list[dict], rows: list[dict]) -> None:
    """Gives each of a report's `series` (reports.grouped) its "label", its "emoji" (a category's) and its "color": a
    category's or a tag's own, an account's by its place among the accounts, a merchant's from its name, so a series
    keeps its color as the report changes; gray for "Other". No two series share a color (chart_colors)."""
    wanted, order = [], lambda s: (0, int(s["key"]), "") if s["key"].isdigit() else (1, 0, s["key"])
    if group == "category":
        names = dict(conn.execute("SELECT id, name FROM categories").fetchall())
        category_looks = looks.category_looks(conn)
        for s in series:
            look = category_looks.get(int(s["key"]), looks.UNCATEGORIZED) if s["key"].isdigit() else looks.UNCATEGORIZED
            s.update(label=names.get(int(s["key"]), "A category") if s["key"].isdigit() else "Uncategorized", emoji=look["emoji"])
            wanted.append(look["color"])
    elif group == "merchant":
        spellings: dict[str, Counter] = {}
        for r in rows:
            spellings.setdefault(reports.merchant_key(r["merchant"]), Counter())[" ".join((r["merchant"] or "").split())] += 1
        for s in series:
            s["label"] = spellings[s["key"]].most_common(1)[0][0] if s["key"] in spellings else s["key"]
            wanted.append(SERIES_HUES[zlib.crc32(s["key"].encode()) % len(SERIES_HUES)])
    elif group == "account":
        labels = account_labels(conn)
        place = {aid: i for i, aid in enumerate(sorted(labels))}
        for s in series:
            s["label"] = labels.get(int(s["key"]), "An account") if s["key"].isdigit() else s["key"]
            wanted.append(SERIES_HUES[place[int(s["key"])] % len(SERIES_HUES)] if s["key"].isdigit() and int(s["key"]) in place else None)
    else:
        tag_looks = tags.tag_looks(conn)
        for s in series:
            tag = tag_looks.get(int(s["key"])) if s["key"].isdigit() else None
            s["label"] = tag["name"] if tag else "No tag"
            wanted.append(tag["color"] if tag else "gray")
    other = bool(series) and series[-1]["key"] == "other"
    named = [(s, color) for s, color in zip(series, wanted) if s["key"] != "other"]
    # NOTE: colors go out in the series' own order (their ids or names), not by size, so one doesn't change with its rank
    named.sort(key=lambda pair: order(pair[0]))
    for (s, _), color in zip(named, chart_colors([color for _, color in named], other)):
        s["color"] = color
    for s in series:
        s.setdefault("emoji", "")
        if s["key"] == "other":
            s.update(label="Other", color="gray")


def report_view(conn, filters: dict, today: date) -> dict:
    """Returns what the report page shows for `filters` (report_filters):
    - the span it covers, and those it's compared with
    - the totals: spent, income, net, the share of income kept, and spent a month on average, each with its changes
    - the chart: the measure by column and series (reports.grouped), each series with its look and links
    - the top merchants and the biggest transactions"""
    span = report_span(conn, filters, today)
    leave = report_leave_outs(conn, filters)
    by, group, show = filters["by"], filters["group"], filters["show"]
    groups = reports.category_groups(conn)
    columns = list(dict.fromkeys(reports.column_of(m, by) for m in span["months"]))
    rows = reports.counted(conn, span["start"], span["end"], show, leave)
    data = reports.grouped(rows, columns, by, group, groups)
    series = data["series"]
    series_looks(conn, group, series, rows)
    # NOTE: a span that starts before the history does has less in it, so comparing with it would mislead
    first = conn.execute("SELECT min(date) FROM transactions").fetchone()[0] or span["start"]
    earlier = [e for e in earlier_spans(span, filters) if e["start"] >= (first[:7] + "-01" if span["whole"] else first)]
    pairs = report_pairs(filters)

    def link(path: str = "/reports", **changes) -> str:
        """Returns the address of this report, or of `path` with its filters, with these filters changed."""
        changed = report_pairs({**filters, **changes}) if changes else pairs
        return path + (f"?{urlencode(changed)}" if changed else "")

    def segment(column: str = "", key: str = "") -> str:
        """Returns the address of the transactions a part of the chart adds up (report_transactions)."""
        return "/reports/transactions?" + urlencode(pairs + [(k, v) for k, v in (("column", column), ("key", key)) if v])

    # Each series' change from the span before, and the links to its transactions and to leaving it out.
    before: Counter = Counter()
    for r in reports.counted(conn, earlier[0]["start"], earlier[0]["end"], show, leave) if earlier else []:
        for key in reports.keys_of(r, group, groups):
            before["other" if key in data["folded"] else key] += r["value"]
    leave_name = {"category": "not_category", "merchant": "not_merchant", "account": "not_account", "tag": "not_tag"}[group]
    for s in series:
        s["change"] = change(s["total"], round(before.get(s["key"], 0.0), 2)) if earlier else None
        s["link"] = segment(key=s["key"])
        leaves = s["key"] != "other" and not (group == "tag" and s["key"] == "none")
        s["leave_out"] = link(**{leave_name: filters[leave_name] + [s["label"] if group == "merchant" else s["key"]]}) if leaves else ""

    # The chart, stacked so neighbors tell apart best, each column's whole and each part linked, and the average of the
    # columns that are over.
    stack = [series[i] for i in charts.best_order([s["color"] for s in series])]
    chart = charts.stacked_bars(columns, stack)
    column_totals = Counter()
    for r in rows:
        column_totals[reports.column_of(r["day"], by)] += r["value"]
    this_month = reports.current_month(today)
    for i, col in enumerate(chart["columns"]):
        col.update(column_labels(col["column"], i == 0), total=round(column_totals[col["column"]], 2), link=segment(column=col["column"]))
        for s, part in zip(stack, col["segments"]):
            part.update(color=s["color"], label=s["label"], link=segment(col["column"], s["key"]))
    for s in series:
        s["cells"] = [{"value": s["values"][c], "link": segment(c, s["key"])} for c in columns]
    done = [c for c in columns if reports.column_range(c)[1] <= reports.month_range(this_month)[0]] or columns
    average = sum(column_totals[c] for c in done) / len(done) if len(columns) > 1 else None
    if average is not None and average > 0:
        chart["average"] = {"value": round(average, 2), "top": charts.y_percent(min(average, chart["high"]), 0.0, chart["high"])}

    # The totals, each compared with the spans before.
    now = reports.totals(conn, span["start"], span["end"], leave)
    then = [{"label": e["label"], "dates": span_dates(e["start"], e["end"]), **reports.totals(conn, e["start"], e["end"], leave)}
            for e in earlier]
    if span["whole"]:
        full = [m for m in span["months"] if m < this_month] or span["months"]
        averaged = reports.totals(conn, reports.month_range(full[0])[0], reports.month_range(full[-1])[1], leave)
        per_month = {"spent": round(averaged["spent"] / len(full), 2), "income": round(averaged["income"] / len(full), 2),
                     "months": len(full), "left_out": len(full) < len(span["months"])}
    else:
        months = (date.fromisoformat(span["end"]) - date.fromisoformat(span["start"])).days / 30.4375
        per_month = {"spent": round(now["spent"] / months, 2), "income": round(now["income"] / months, 2), "months": None, "left_out": False}
    for t in then:
        t["changes"] = {k: change(now[k], t[k]) for k in ("spent", "income", "net")}
        t["rate_change"] = round(now["rate"] - t["rate"], 1) if now["rate"] is not None and t["rate"] is not None else None

    # The top merchants (by what the report shows) and the biggest transactions, each to see or leave out.
    merchants: dict[str, dict] = {}
    for r in rows:
        m = merchants.setdefault(reports.merchant_key(r["merchant"]), {"total": 0.0, "count": 0, "names": Counter()})
        m["total"] += r["value"]
        m["count"] += 1
        m["names"][" ".join((r["merchant"] or "").split())] += 1
    top = sorted(({"key": k, "label": m["names"].most_common(1)[0][0], "total": round(m["total"], 2), "count": m["count"]}
                  for k, m in merchants.items() if m["total"] > 0), key=lambda m: (-m["total"], m["key"]))[:10]
    for m in top:
        m["link"] = "/reports/transactions?" + urlencode(report_pairs({**filters, "group": "merchant"}) + [("key", m["key"])])
        m["leave_out"] = link(not_merchant=filters["not_merchant"] + [m["label"]])
        m["share"] = m["total"] / top[0]["total"] * 100
    big = reports.biggest(conn, span["start"], span["end"], show, leave)
    for b in big:
        b["leave_out"] = link(not_id=filters["not_id"] + [str(b["id"])])
    return {
        "span": span, "earlier": earlier, "now": now, "then": then, "per_month": per_month, "series": series, "stack": stack, "chart": chart,
        "data": data, "top_merchants": top, "biggest": big, "report_link": link, "pairs": pairs,
        "several_tags": group == "tag" and any(len(r["tags"]) > 1 for r in rows),
    }



def pace_chart(pace: dict, month: str, budget: float) -> dict | None:
    """Returns the dashboard's line chart of spending added up day by day, the month's over the month before's,
    with the budget across it; None when neither month has any spending."""
    if not any(pace["this"]) and not any(pace["last"]):
        return None
    last_month = reports.shift_month(month, -1)
    this_abbr, last_abbr = (datetime.strptime(m, "%Y-%m").strftime("%b") for m in (month, last_month))
    this_days = calendar.monthrange(*map(int, month.split("-")))[1]
    labels = [" · ".join(f"{abbr} {day}" for abbr, length in ((this_abbr, this_days), (last_abbr, len(pace["last"])))
                         if day <= length)
              for day in range(1, pace["days"] + 1)]
    chart = charts.line_chart(labels, [
        {"name": month_name(last_month), "values": pace["last"], "role": "context"},
        {"name": month_name(month), "values": pace["this"], "role": "main", "area": True},
    ], markers=[{"label": "Budget", "value": budget}] if budget else [])
    chart["x_ticks"] = [{"label": f"{this_abbr} {day}", "left": charts.x_percent(day - 1, pace["days"])}
                        for day in (1, 8, 15, 22, 29) if day <= pace["days"]]
    return chart


def months_chart(series: list[dict], selected: str, everyday: list[dict] | None = None) -> dict:
    """Returns the dashboard's columns of monthly spending, from monthly_series, with `selected` marked. With
    `everyday` (monthly_series without what's set aside from everyday spending), each bar also has "everyday", "aside"
    (what's set aside) and "aside_share", that part's share of the bar, drawn on top."""
    geometry = charts.bar_heights([m["spent"] for m in series])
    bars = [{**m, "height": height, "selected": m["month"] == selected,
             "tick": datetime.strptime(m["month"], "%Y-%m").strftime("%b"),
             "year": m["month"][:4] if i == 0 or m["month"].endswith("-01") else ""}
            for i, (m, height) in enumerate(zip(series, geometry["heights"]))]
    for bar, day_to_day in zip(bars, everyday or []):
        aside = round(bar["spent"] - day_to_day["spent"], 2)
        bar.update(everyday=day_to_day["spent"], aside=aside,
                   aside_share=min(100.0, aside / bar["spent"] * 100) if bar["spent"] > 0 and aside > 0 else 0.0)
    return {"bars": bars, "ticks": geometry["ticks"], "everyday": everyday is not None}


def pinned_reports(conn, today: date) -> list[dict]:
    """Returns the saved reports pinned to the dashboard, each with its "link", "span" (report_span), "total" (what it
    shows came to) and "bars": a column for each of its months, quarters or years, with its height and total."""
    out = []
    for r in conn.execute("SELECT * FROM saved_reports WHERE pinned = 1 ORDER BY name COLLATE NOCASE, id"):
        filters = report_filters(parse_qsl(r["query"]))
        span = report_span(conn, filters, today)
        rows = reports.counted(conn, span["start"], span["end"], filters["show"], report_leave_outs(conn, filters))
        columns = list(dict.fromkeys(reports.column_of(m, filters["by"]) for m in span["months"]))
        totals = Counter()
        for row in rows:
            totals[reports.column_of(row["day"], filters["by"])] += row["value"]
        geometry = charts.bar_heights([totals[c] for c in columns])
        out.append({"id": r["id"], "name": r["name"], "span": span, "show": filters["show"],
                    "link": f"/reports?{r['query']}{'&' if r['query'] else ''}saved={r['id']}",
                    "total": round(sum(row["value"] for row in rows), 2),
                    "bars": [{"height": h, "total": round(totals[c], 2), **column_labels(c, i == 0)}
                             for i, (c, h) in enumerate(zip(columns, geometry["heights"]))]})
    return out


def worth_chart(history: list[tuple[str, float]]) -> dict | None:
    """Returns a line chart of net worth over the days in `history` (from reports.net_worth_history); None when it
    has fewer than two days."""
    if len(history) < 2:
        return None
    chart = charts.line_chart([short_date(day) for day, _ in history],
                              [{"name": "Net worth", "values": [value for _, value in history], "role": "main"}], zero=False)
    last = len(history) - 1
    chart["x_ticks"] = [{"label": short_date(history[i][0]), "left": charts.x_percent(i, len(history))}
                        for i in sorted({0, last // 2, last})]
    return chart


# Transactions as the payback section of a transaction's page lists them.
_LINK_ROWS = """SELECT t.*, a.name AS account_name, a.mask AS account_mask
                FROM transactions t JOIN accounts a ON a.id = t.account_id"""


def payback_problem(conn, payback, purchase) -> str | None:
    """Returns why `payback` can't pay back `purchase` (None means unlinking), or None if it can."""
    if purchase is None:
        return None
    for row in (payback, purchase):
        if splits.parts_of(conn, row["id"]):
            return f"“{row['name']}” is split into parts. Link one of its parts instead."
    if payback["amount"] <= 0 or payback["type"] == "transfer":
        return "Only money coming in, other than a transfer, can pay back a purchase."
    if purchase["amount"] >= 0 or purchase["type"] != "regular" or purchase["reimburses_id"]:
        return "Pick a purchase: money that went out, other than a transfer."
    if conn.execute("SELECT 1 FROM transactions WHERE reimburses_id = ?", (payback["id"],)).fetchone():
        return f"“{payback['name']}” has paybacks of its own, so it can't pay back something else."
    if links.is_linked(conn, payback):
        return f"“{payback['name']}” is linked with other transactions. Unlink it first."
    return None


def transfer_problem(conn, t, other) -> str | None:
    """Returns why `t` and `other` can't be the two sides of one move between accounts, or None if they can."""
    if other["id"] == t["id"] or other["account_id"] == t["account_id"]:
        return "Pick the other side, on another of your accounts."
    if (t["amount"] < 0) == (other["amount"] < 0):
        return "One side has to be money out and the other money in."
    for row in (t, other):
        if splits.parts_of(conn, row["id"]):
            return f"“{row['name']}” is split into parts, so it can't be one side of a move."
    for row in (t, other):
        if row["reimburses_id"] or conn.execute("SELECT 1 FROM transactions WHERE reimburses_id = ?", (row["id"],)).fetchone():
            return f"“{row['name']}” is linked to a payback. Unlink that first."
        if links.is_linked(conn, row):
            return f"“{row['name']}” is linked with other transactions. Unlink it first."
    return None


def bank_details(t) -> dict:
    """Returns what the bank says about transaction `t` (sync._details) for its page, with the time it was made as local
    time, like "6:22 PM", and the day it was made as that time's local day; an empty dict when there's nothing."""
    details = json.loads(t["plaid_details"]) if t["plaid_details"] else {}
    if details.get("time"):
        made_at = sync.purchase_time(details["time"])
        if made_at is None:
            del details["time"]
        else:
            details["time"] = made_at.strftime("%-I:%M %p")
            if details.get("made"):
                details["made"] = made_at.date().isoformat()  # NOTE: some banks' purchase date is the UTC day
    return details


def cash_account(conn) -> int | None:
    """Returns the id of the manual Cash account, or None until the first cash transaction creates it."""
    row = conn.execute("SELECT id FROM accounts WHERE source = 'manual' AND name = 'Cash' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def get_transaction(conn, tid: int):
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if t is None:
        raise HTTPException(404, "Transaction not found.")
    return t


def rule_offer(conn, t, category_id: int | None) -> dict | None:
    """Returns the offer, after transaction `t` was put in `category_id`, to always put transactions with its name
    there: its id and name, the category's name, and how many others it would change; None without a category."""
    if category_id is None:
        return None
    others = conn.execute(
        "SELECT count(*) FROM transactions WHERE id != ? AND user_edited = 0 AND lower(trim(name)) = lower(trim(?)) "
        "AND coalesce(category_id, -1) != ?",
        (t["id"], t["name"], category_id),
    ).fetchone()[0]
    return {"id": t["id"], "name": t["name"], "category": category_name(conn, category_id), "others": others}


def category_name(conn, category_id: int | None) -> str | None:
    if category_id is None:
        return None
    row = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    return row["name"] if row else None


def learn_plaid_mapping(conn, plaid_category: str | None, category_id: int | None) -> bool:
    """Remembers category_id for Plaid's category when nothing is mapped to it yet. Returns True if it did.
    Plaid's categories for income, transfers and app transfers such as Zelle are never learned, since what one
    of those was for doesn't carry over to the next."""
    if not plaid_category or category_id is None:
        return False
    if categorize.plaid_type(plaid_category) != "regular" or plaid_category in categorize.APP_TRANSFERS:
        return False
    if conn.execute("SELECT 1 FROM plaid_category_map WHERE plaid_category = ?", (plaid_category,)).fetchone():
        return False
    conn.execute("INSERT INTO plaid_category_map (plaid_category, category_id) VALUES (?, ?)", (plaid_category, category_id))
    return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app(load_settings())
