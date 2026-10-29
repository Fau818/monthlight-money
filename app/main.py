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
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
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
# (path, label, icon). The main pages are also the tab bar on a phone; the others sit under More there.
NAV = [
    ("/", "Dashboard", "home"), ("/transactions", "Transactions", "list"), ("/budgets", "Budgets", "pie"),
    ("/recurring", "Recurring", "repeat"), ("/accounts", "Accounts", "bank"),
]
MORE_NAV = [("/categories", "Categories & rules", "tag"), ("/import", "Import", "download"), ("/settings", "Settings", "sliders")]
FOLDED_COOKIE = "sidebar_folded"  # the account groups folded in the sidebar, by kind: "credit,loan" (app.js sets it)
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
    percent=percent, plural=plural, nav=NAV, more_nav=MORE_NAV, static_version=STATIC_VERSION,
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
        return render(
            request, conn, "dashboard.html",
            month=month, prev_month=reports.shift_month(month, -1), next_month=reports.shift_month(month, 1),
            this_month=reports.current_month(today), totals=reports.month_totals(conn, month),
            nodes=nodes, budget=budget, pace=pace, pace_chart=pace_chart(pace, month, budget),
            months=months_chart(reports.monthly_series(conn, 12, month), month),
            worth=reports.net_worth(conn), worth_chart=worth_chart(history[-90:]),
            upcoming=recurring.upcoming(conn, 30, today)[:5], review=review, to_review=to_review,
            categories=category_options(conn),
            has_data=conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None,
        )

    # ---- transactions ----

    @app.get("/transactions")
    def transactions(request: Request, month: str = "", account: str = "", category: str = "", type: str = "", tag: str = "",
                     amount: str = "", q: str = "", review: str = "", transfers: str = "", select: str = "", oldest: str = "",
                     conn=Depends(get_db)):
        month = reports.valid_month(month) or ""
        account = account if account.isdigit() else ""  # a hand-typed URL can hold anything; ignore what isn't an id
        category = category if category == "none" or category.isdigit() else ""
        type_ = type if type in categorize.TYPES else None
        amount = amount.strip()
        criteria = {"month": month or None, "account": optional_int(account), "category": category or None, "q": q.strip() or None,
                    "review": review == "1", "tag": int(tag) if tag.isdigit() else None, "amount": amount_range(amount) if amount else None}
        # Moves between the user's own accounts, with both sides found and linked, are hidden unless asked for. A
        # transfer without its other side shows, since it may be money that went to someone else. The review list
        # shows every new one once; looking for an amount shows them all, as the other side may be what's looked for.
        hide = transfers != "1" and type_ is None and not criteria["review"] and not criteria["amount"]
        # The list goes a few months at a time, newest first, through `oldest` once more were loaded (app.js does that
        # as it scrolls near the end): whole months, as many as it takes to list PAGE_SIZE transactions or more.
        months_listed = listed_months(conn, **criteria, type_=type_, hide_transfers=hide)
        shown = month_chunk(months_listed, months_listed[0][0], reports.valid_month(oldest)) if months_listed and not month else []
        rest = [m for m, _ in months_listed if shown and m < shown[-1]]
        older = month_chunk(months_listed, rest[0]) if rest else []
        between = (reports.month_range(shown[-1])[0], reports.month_range(shown[0])[1]) if shown else None
        rows, _ = find_transactions(conn, **criteria, between=between, type_=type_, hide_transfers=hide, limit=-1)
        hidden = find_transactions(conn, **criteria, linked_transfers=True, limit=0)[1] if hide else 0
        filters = {k: v for k, v in {"month": month, "account": account, "category": category, "type": type, "tag": tag,
                                     "amount": amount, "q": q, "review": review, "transfers": transfers, "select": select}.items() if v}
        # Each month starts with its totals, as on the dashboard, when the list shows all of it.
        whole_months = set(filters) <= {"month", "transfers", "select"}
        today = date.today()
        budget = reports.budget_total(reports.category_spending(conn, reports.current_month(today))) if whole_months else 0

        def without(*keys: str) -> str:
            rest = {k: v for k, v in filters.items() if k not in keys}
            return "/transactions" + (f"?{urlencode(rest)}" if rest else "")

        return render(
            request, conn, "transactions.html",
            months=[dict(m, totals=reports.month_totals(conn, m["month"]) if whole_months else None, budget=budget,
                         current=m["month"] == reports.current_month(today)) for m in by_month(group_by_day(rows, today))],
            rows=rows, total=sum(n for _, n in months_listed), filters=filters, query=urlencode(filters),
            older=older and "/transactions?" + urlencode({**filters, "oldest": older[-1]}) + f"#m{older[0]}", older_month=older and older[0],
            sums=transaction_sums(conn, **criteria, type_=type_, hide_transfers=hide),
            amount_unreadable=bool(amount) and criteria["amount"] is None,
            categories=category_options(conn), hidden_transfers=hidden,
            show_transfers_link="/transactions?" + urlencode({**filters, "transfers": "1"}),
            hide_transfers_link=without("transfers"),
            review_link="/transactions?" + urlencode({**filters, "review": "1"}), all_link=without("review"),
            selecting=select == "1", select_link="/transactions?" + urlencode({**filters, "select": "1"}), done_link=without("select"),
            accounts=conn.execute("SELECT id, name, mask FROM accounts ORDER BY name").fetchall(),
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

    @app.post("/transactions/bulk")
    def bulk_edit(ids: list[int] = Form([]), action: str = Form(""), tag_id: str = Form(""), category_id: str = Form(""),
                  name: str = Form(""), back: str = Form("/transactions"), conn=Depends(get_db)):
        """Does `action` to the transactions selected in the list (`ids`): tag or untag (tag_id), category (category_id),
        review, exclude, include, link or unlink (links.py), or combine (into one named `name`, see splits.combine)."""
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
                tag = conn.execute("SELECT id, name FROM tags WHERE id = ?", (optional_int(tag_id),)).fetchone() if tag_id.isdigit() else None
                if tag is None:
                    return redirect(back, "Pick a tag first.")
                if action == "tag":
                    tags.add(conn, tag["id"], [r["id"] for r in rows])
                    return redirect(back, f"Tagged {count(len(rows))} “{tag['name']}”.")
                return redirect(back, f"Took “{tag['name']}” off {count(tags.remove(conn, tag['id'], [r['id'] for r in rows]))}.")
            if action == "category":
                new_category = optional_int(category_id)  # "none" means uncategorized
                if not category_id or (new_category is not None and category_name(conn, new_category) is None):
                    return redirect(back, "Pick a category first.")
                conn.executemany("UPDATE transactions SET category_id = ?, excluded = 0, user_edited = 1, reviewed = 1 WHERE id = ?",
                                 [(new_category, r["id"]) for r in regular])
                return redirect(back, f"{count(len(regular))} now in {category_name(conn, new_category) or 'Uncategorized'}.{skipped}")
            if action == "review":
                conn.executemany("UPDATE transactions SET reviewed = 1 WHERE id = ?", [(r["id"],) for r in rows])
                return redirect(back, f"Marked {count(len(rows))} as reviewed.")
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

    # ---- budgets ----

    @app.get("/budgets")
    def budgets(request: Request, month: str | None = None, edit: str = "", conn=Depends(get_db)):
        today = date.today()
        month = reports.valid_month(month) or reports.current_month(today)
        nodes = reports.category_spending(conn, month)
        shown = {n["id"] for n in nodes} | {ch["id"] for n in nodes for ch in n["children"]}
        unbudgeted = conn.execute("SELECT id, name FROM categories WHERE excluded = 0 ORDER BY name").fetchall()
        # Days left in the current month, today included, for "about $X a day" (None for other months).
        days_left = None
        if month == reports.current_month(today):
            days_left = (date.fromisoformat(reports.month_range(month)[1]) - today).days
        return render(
            request, conn, "budgets.html", month=month, nodes=nodes, budget=reports.budget_total(nodes),
            totals=reports.month_totals(conn, month), others=[c for c in unbudgeted if c["id"] not in shown],
            prev_month=reports.shift_month(month, -1), next_month=reports.shift_month(month, 1),
            this_month=reports.current_month(today), editing=edit == "1", days_left=days_left,
        )

    @app.post("/budgets")
    async def save_budgets(request: Request, conn=Depends(get_db)):
        form = await request.form()
        updates = []
        for key, value in form.items():
            if key.startswith("budget_"):
                try:
                    updates.append((optional_money(str(value)), int(key[7:])))
                except ValueError:
                    return redirect(safe_back(str(form.get("back")), "/budgets"), f"“{value}” isn't a number.")
        with conn:
            conn.executemany("UPDATE categories SET budget = ? WHERE id = ?", updates)
        return redirect(safe_back(str(form.get("back")), "/budgets"), "Budgets saved.")

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

    @app.post("/recurring/track")
    def track_recurring(name: str = Form(...), pattern: str = Form(...), frequency: str = Form("monthly"),
                        amount: str = Form(""), category_id: str = Form(""), conn=Depends(get_db)):
        if frequency not in recurring.FREQUENCIES or not pattern.strip():
            return redirect("/recurring", "Pick a frequency and the text to match.")
        try:
            typical = optional_money(amount)
        except ValueError:
            return redirect("/recurring", f"“{amount.strip()}” isn't an amount.")
        with conn:
            _, linked = recurring.track(conn, name.strip(), pattern, frequency, typical, optional_int(category_id))
        return redirect("/recurring", f"Tracking {name}. Linked {linked} past transactions.")

    @app.post("/recurring/{rid}")
    def save_recurring(rid: int, name: str = Form(...), frequency: str = Form("monthly"), amount: str = Form(""),
                       category_id: str = Form(""), active: str = Form(""), conn=Depends(get_db)):
        if frequency not in recurring.FREQUENCIES:
            return redirect("/recurring", "Unknown frequency.")
        try:
            typical = optional_money(amount)
        except ValueError:
            return redirect("/recurring", f"“{amount.strip()}” isn't an amount.")
        with conn:
            conn.execute(
                "UPDATE recurring SET name = ?, frequency = ?, amount = ?, category_id = ?, active = ? WHERE id = ?",
                (name.strip(), frequency, typical, optional_int(category_id), int(bool(active)), rid),
            )
        return redirect("/recurring", "Saved.")

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

    @app.post("/accounts/{aid}")
    def save_account(aid: int, name: str = Form(...), hidden: str = Form(""), archived: str = Form(""), conn=Depends(get_db)):
        with conn:
            conn.execute("UPDATE accounts SET name = ?, hidden = ?, archived = ? WHERE id = ?",
                         (name.strip() or "Account", int(bool(hidden)), int(bool(archived)), aid))
        return redirect("/accounts", "Account saved.")

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

    @app.get("/categories")
    def categories_page(request: Request, conn=Depends(get_db)):
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
            request, conn, "categories.html", cats=cats, options=category_options(conn),
            top_level=[c for c in cats if c["parent_id"] is None], codes=codes, mapping=mapping, rules=rules,
            types=categorize.TYPES,
            tag_uses=dict(conn.execute("SELECT tag_id, count(*) FROM transaction_tags GROUP BY tag_id").fetchall()),
        )

    @app.post("/categories")
    def add_category(name: str = Form(...), parent_id: str = Form(""), conn=Depends(get_db)):
        name = name.strip()
        if not name:
            return redirect("/categories", "Give the category a name.")
        with conn:
            categorize.get_or_create_category(conn, name, optional_int(parent_id))
            categorize.auto_map_plaid_categories(conn)
        return redirect("/categories", f"Added {name}.")

    @app.post("/categories/defaults")
    def add_default_categories(conn=Depends(get_db)):
        with conn:
            added = categorize.ensure_default_categories(conn)
        return redirect("/categories", "Added the default categories." if added else "You already have categories.")

    @app.post("/categories/{cid}")
    def save_category(cid: int, name: str = Form(...), parent_id: str = Form(""), excluded: str = Form(""),
                      emoji: str = Form(""), color: str = Form(""), conn=Depends(get_db)):
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
            return redirect("/categories", f"There's already a category called {name.strip()}.")
        return redirect("/categories", "Category saved.")

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
        return redirect("/categories#plaid", "Plaid category mapping saved. It applies to new transactions.")

    @app.post("/rules")
    def add_rule(pattern: str = Form(...), match: str = Form("contains"), category_id: str = Form(""),
                 type: str = Form(""), rename_to: str = Form(""), apply: str = Form(""), conn=Depends(get_db)):
        if not pattern.strip() or match not in ("equals", "contains"):
            return redirect("/categories#rules", "A rule needs some text to match.")
        category, type_, rename_to = optional_int(category_id), type if type in categorize.TYPES else None, rename_to.strip()
        if category is None and type_ is None and not rename_to:
            return redirect("/categories#rules", "Pick what the rule does: a category, a type or a new name.")
        with conn:
            rule_id = categorize.add_rule(conn, pattern, match, category, type_, rename_to)
            changed = categorize.apply_rule(conn, rule_id) if apply else 0
        return redirect("/categories#rules", "Rule saved." + (f" Updated {changed} transactions." if apply else ""))

    @app.post("/rules/{rule_id}/delete")
    def delete_rule(rule_id: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        return redirect("/categories#rules", "Rule deleted.")

    # ---- tags ----

    @app.post("/tags")
    def add_tag(name: str = Form(...), color: str = Form(""), conn=Depends(get_db)):
        name = tags.clean_name(name)
        if not name:
            return redirect("/categories#tags", "Give the tag a name.")
        with conn:
            tags.get_or_create(conn, name, color)
        return redirect("/categories#tags", f"Added the tag {name}.")

    @app.post("/tags/{gid}")
    def save_tag(gid: int, name: str = Form(...), color: str = Form(""), conn=Depends(get_db)):
        name = tags.clean_name(name)
        if not name:
            return redirect("/categories#tags", "Give the tag a name.")
        try:
            with conn:
                conn.execute("UPDATE tags SET name = ?, color = ? WHERE id = ?", (name, color if color in looks.COLORS else None, gid))
        except sqlite3.IntegrityError:
            return redirect("/categories#tags", f"There's already a tag called {name}.")
        return redirect("/categories#tags", "Tag saved.")

    @app.post("/tags/{gid}/delete")
    def delete_tag(gid: int, conn=Depends(get_db)):
        with conn:
            conn.execute("DELETE FROM tags WHERE id = ?", (gid,))
        return redirect("/categories#tags", "Tag deleted. Its transactions are still there, without it.")

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


def find_transactions(conn, *, limit: int = PAGE_SIZE, offset: int = 0, **criteria) -> tuple[list[dict], int]:
    """Returns (transactions matching `criteria`, total number matching); see transaction_filter for the criteria.
    They come newest day first and, within a day, biggest amount first, money in and out alike, then by name; the parts
    of a split follow one another where the split's total puts them. Each transaction has "tags", its tags from
    tags.tag_looks, and "links", how many others it's linked with (links.py); a part also has "split_links", how many
    its split is linked with."""
    clause, params = transaction_filter(**criteria)
    # Not every bank gives the time of day, so a day goes by amount, then name. A split goes as the row for it above its
    # parts (with_split_heads), by its total and name, and its parts follow it by their own amounts.
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
            ORDER BY shown_date DESC, abs(coalesce(sp.amount, t.amount)) DESC, coalesce(sp.name, t.name) COLLATE NOCASE,
                     coalesce(t.split_from_id, t.id) DESC, abs(t.amount) DESC, t.name COLLATE NOCASE, t.id DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    total = conn.execute(f"SELECT count(*) FROM {_LISTED} WHERE {clause}", params).fetchone()[0]
    looks_by_id = tags.tag_looks(conn)
    rank = {g: i for i, g in enumerate(looks_by_id)}  # tag_looks is ordered by name
    tagged = tags.tags_by_transaction(conn, [r["id"] for r in rows])
    return [dict(r, tags=[looks_by_id[g] for g in sorted(tagged.get(r["id"], []), key=rank.get)]) for r in rows], total


def transaction_sums(conn, **criteria) -> dict:
    """Returns {"out", "in"}: the money out (as a positive number) and in across every transaction matching
    `criteria`, as listed, so excluded ones count too."""
    clause, params = transaction_filter(**criteria)
    row = conn.execute(
        f"""SELECT coalesce(-sum(CASE WHEN t.amount < 0 THEN t.amount END), 0) AS money_out,
                   coalesce(sum(CASE WHEN t.amount > 0 THEN t.amount END), 0) AS money_in
            FROM {_LISTED} WHERE {clause} AND {reports.NOT_SPLIT}""",
        params,
    ).fetchone()
    return {"out": round(row["money_out"], 2), "in": round(row["money_in"], 2)}


def transaction_filter(*, month: str | None = None, between: tuple[str, str] | None = None, account: int | None = None,
                       category: str | None = None, type_: str | None = None, tag: int | None = None, amount: tuple | None = None,
                       q: str | None = None, review: bool = False, hide_transfers: bool = False,
                       linked_transfers: bool = False) -> tuple[str, list]:
    """Returns the SQL condition on transactions `t` (with `p` and `ot`, see _LISTED) for these criteria and its parameters.
    Category "none" means regular transactions waiting for a category: income and transfers don't take one, and
    excluded ones have none (db.py). A
    payback goes by its purchase's month and category. `month` and `between` (first day, day after the last) go by the
    day a transaction is listed on (SHOWN_DAY). `amount` is (lowest, highest) from amount_range, money in and
    out alike. A transaction split into parts shows only on the review list (`review`), when the parts need a look:
    otherwise its parts show instead. `hide_transfers` leaves out moves between the user's accounts whose two sides
    are linked; `linked_transfers` keeps only those."""
    where, params = ["1 = 1"], []
    if not review:
        where.append(reports.NOT_SPLIT)
    if tag:
        where.append("EXISTS (SELECT 1 FROM transaction_tags tt WHERE tt.transaction_id = t.id AND tt.tag_id = ?)")
        params.append(tag)
    for start, end in ([reports.month_range(month)] if month else []) + ([between] if between else []):
        where.append(f"{SHOWN_DAY} >= ? AND {SHOWN_DAY} < ?")
        params += [start, end]
    if account:
        where.append("t.account_id = ?")
        params.append(account)
    if category == "none":
        where.append(f"{reports.CATEGORY} IS NULL AND t.type = 'regular' AND t.excluded = 0 AND coalesce(p.excluded, 0) = 0")
    elif category:
        where.append(f"{reports.CATEGORY} = ?")
        params.append(optional_int(category))
    if type_:
        where.append("t.type = ?")
        params.append(type_)
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
    if review:
        where.append(f"(t.reviewed = 0 OR {reports.SPLIT_OFF})")
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


def months_chart(series: list[dict], selected: str) -> dict:
    """Returns the dashboard's columns of monthly spending, from monthly_series, with `selected` marked."""
    geometry = charts.bar_heights([m["spent"] for m in series])
    bars = [{**m, "height": height, "selected": m["month"] == selected,
             "tick": datetime.strptime(m["month"], "%Y-%m").strftime("%b"),
             "year": m["month"][:4] if i == 0 or m["month"].endswith("-01") else ""}
            for i, (m, height) in enumerate(zip(series, geometry["heights"]))]
    return {"bars": bars, "ticks": geometry["ticks"]}


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
