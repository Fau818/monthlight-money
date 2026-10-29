"""SQLite storage: schema and connections."""

import sqlite3
from pathlib import Path

# Amounts are signed from the account holder's view: positive = money in, negative = money out.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS plaid_items (
    item_id TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    env TEXT NOT NULL,
    institution_id TEXT,
    institution_name TEXT,
    cursor TEXT,
    status TEXT NOT NULL DEFAULT 'ok',  -- ok | needs_match | login_required | error
    error TEXT,
    update_status TEXT,                 -- Plaid's transactions_update_status
    pending_accounts TEXT,              -- JSON: Plaid accounts waiting to be matched
    consent_expires_at TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    institution TEXT,
    type TEXT NOT NULL DEFAULT 'depository',  -- depository | credit | loan | investment | other
    subtype TEXT,
    mask TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    balance REAL,          -- as Plaid reports it: for credit and loan accounts, the amount owed
    available REAL,
    credit_limit REAL,
    balance_updated_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,      -- left out of every total
    archived INTEGER NOT NULL DEFAULT 0,    -- no longer used: its history still counts, but it isn't offered for new transactions
    source TEXT NOT NULL DEFAULT 'manual',    -- plaid | copilot | manual
    plaid_account_id TEXT UNIQUE,
    plaid_item_id TEXT REFERENCES plaid_items(item_id) ON DELETE SET NULL,
    history_start TEXT,    -- first and last dates of the history this account had before
    history_cutoff TEXT,   -- it was connected to Plaid (e.g. imported from Copilot)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    budget REAL,                          -- monthly budget in dollars, NULL for none
    excluded INTEGER NOT NULL DEFAULT 0,  -- left out of spending totals
    emoji TEXT,                           -- picked by the user; NULL means one chosen from the name (looks.py)
    color TEXT                            -- one of looks.COLORS, or NULL in the same way
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_categories_name ON categories(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS recurring (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,                 -- lowercase text found in the transaction name
    amount REAL,                           -- typical amount, negative for charges
    frequency TEXT NOT NULL DEFAULT 'monthly',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    name TEXT NOT NULL,
    original_name TEXT,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    type TEXT NOT NULL DEFAULT 'regular',  -- regular | income | transfer
    excluded INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0,
    user_edited INTEGER NOT NULL DEFAULT 0,  -- edited by hand; rules and syncs leave it alone
    name_edited INTEGER NOT NULL DEFAULT 0,  -- renamed by hand; renaming rules and cleanups leave its name alone
    date_edited INTEGER NOT NULL DEFAULT 0,  -- date set by hand; syncs keep it
    own_date TEXT,                           -- while it's a part dated like its split, the date it had before; it gets it back on leaving
    reimburses_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,  -- the purchase this money pays back
    transfer_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,  -- the other side of a move between accounts
    split_from_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,  -- the transaction this is a part of (splits.py)
    link_id INTEGER,                         -- shared by transactions linked as belonging together (links.py)
    recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL,
    source TEXT NOT NULL,                    -- plaid | copilot | manual
    import_key TEXT UNIQUE,                  -- identifies a Copilot CSV row across re-imports
    plaid_transaction_id TEXT UNIQUE,
    plaid_category TEXT,                     -- Plaid's detailed category, e.g. FOOD_AND_DRINK_COFFEE
    plaid_details TEXT,                      -- JSON: what else the bank says about it, for its page (sync._details)
    logo_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_transactions_account ON transactions(account_id, date);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,                 -- lowercase
    match TEXT NOT NULL DEFAULT 'equals',  -- equals | contains
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    type TEXT,
    rename_to TEXT,                        -- the name to give them, or NULL to keep theirs
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (pattern, match)
);

CREATE TABLE IF NOT EXISTS plaid_category_map (
    plaid_category TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS balance_history (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    balance REAL NOT NULL,
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT                            -- one of looks.COLORS, or NULL for one that depends on its id (tags.py)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tags_name ON tags(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS saved_reports (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',     -- the report's filters, as its address holds them (main.report_pairs)
    pinned INTEGER NOT NULL DEFAULT 0,  -- shown on the dashboard
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (transaction_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_transaction_tags_tag ON transaction_tags(tag_id);
"""

# Before name_edited, user_edited stood for any change by hand. A row edited by hand whose name isn't the one the bank
# gives it (its merchant's, else its own) was renamed, and so was one without the bank's details to tell.
NAMES_EDITED_BEFORE = """UPDATE transactions SET name_edited = 1
    WHERE user_edited = 1 AND CASE WHEN json_valid(plaid_details)
        THEN name IS NOT coalesce(nullif(plaid_details ->> '$.merchant', ''), plaid_details ->> '$.bank_name') ELSE 1 END"""

# Columns added after databases already existed. CREATE TABLE IF NOT EXISTS leaves an existing table as it is,
# so connect() adds these to older databases, each with the statements that then fill it in, if any.
ADDED_COLUMNS = {
    "accounts": [("archived", "INTEGER NOT NULL DEFAULT 0")],
    "categories": [("emoji", "TEXT"), ("color", "TEXT")],
    "transactions": [("date_edited", "INTEGER NOT NULL DEFAULT 0"),
                     ("reimburses_id", "INTEGER REFERENCES transactions(id) ON DELETE SET NULL"),
                     ("transfer_id", "INTEGER REFERENCES transactions(id) ON DELETE SET NULL"),
                     ("split_from_id", "INTEGER REFERENCES transactions(id) ON DELETE SET NULL"),
                     ("plaid_details", "TEXT"), ("link_id", "INTEGER"), ("own_date", "TEXT"),
                     ("name_edited", "INTEGER NOT NULL DEFAULT 0", NAMES_EDITED_BEFORE)],
    "rules": [("rename_to", "TEXT")],
}
# Indexes and triggers on columns in ADDED_COLUMNS, which can only be made once those columns exist.
ADDED_INDEXES = ["CREATE INDEX IF NOT EXISTS ix_transactions_split ON transactions(split_from_id)",
                 "CREATE INDEX IF NOT EXISTS ix_transactions_link ON transactions(link_id)",
                 # A part of a split is dated like the transaction it's split from: one order, one date. Its parts follow
                 # when that date changes, by hand or by a sync, and a part takes it when it's made, joins a split, or is
                 # given another date.
                 """CREATE TRIGGER IF NOT EXISTS parts_follow_the_date AFTER UPDATE OF date ON transactions BEGIN
                        UPDATE transactions SET date = NEW.date WHERE split_from_id = NEW.id AND date != NEW.date;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS new_part_takes_the_date AFTER INSERT ON transactions WHEN NEW.split_from_id IS NOT NULL BEGIN
                        UPDATE transactions SET date = (SELECT o.date FROM transactions o WHERE o.id = NEW.split_from_id) WHERE id = NEW.id;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS part_keeps_the_date AFTER UPDATE OF date, split_from_id ON transactions
                    WHEN NEW.split_from_id IS NOT NULL BEGIN
                        UPDATE transactions SET date = (SELECT o.date FROM transactions o WHERE o.id = NEW.split_from_id)
                        WHERE id = NEW.id AND date != (SELECT o.date FROM transactions o WHERE o.id = NEW.split_from_id);
                    END""",
                 # A transaction that joins a split or a combined line keeps the date it had, and gets it back when it
                 # leaves, however it leaves: unsplit, ungrouped, taken out of the split, or set free when the bank removes
                 # the transaction it was split from (ON DELETE SET NULL fires this too).
                 """CREATE TRIGGER IF NOT EXISTS joining_part_keeps_its_own_date AFTER UPDATE OF split_from_id ON transactions
                    WHEN OLD.split_from_id IS NULL AND NEW.split_from_id IS NOT NULL BEGIN
                        UPDATE transactions SET own_date = OLD.date WHERE id = NEW.id AND own_date IS NULL;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS leaving_part_gets_its_own_date_back AFTER UPDATE OF split_from_id ON transactions
                    WHEN OLD.split_from_id IS NOT NULL AND NEW.split_from_id IS NULL BEGIN
                        UPDATE transactions SET date = coalesce(own_date, date), own_date = NULL WHERE id = NEW.id;
                    END""",
                 # A transaction is either in a category or excluded from spending, never both, as in Copilot: excluding
                 # it clears its category, however it's excluded.
                 """CREATE TRIGGER IF NOT EXISTS excluded_has_no_category AFTER UPDATE OF excluded, category_id ON transactions
                    WHEN NEW.excluded = 1 AND NEW.category_id IS NOT NULL BEGIN
                        UPDATE transactions SET category_id = NULL WHERE id = NEW.id;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS new_excluded_has_no_category AFTER INSERT ON transactions
                    WHEN NEW.excluded = 1 AND NEW.category_id IS NOT NULL BEGIN
                        UPDATE transactions SET category_id = NULL WHERE id = NEW.id;
                    END""",
                 # Money that pays back a purchase has the purchase's category, as it counts in it (reports.CATEGORY),
                 # unless it's excluded: its paybacks follow when the purchase's category changes, and a payback takes it
                 # when it's made, linked, counted again, or given another one.
                 """CREATE TRIGGER IF NOT EXISTS paybacks_follow_the_category AFTER UPDATE OF category_id ON transactions BEGIN
                        UPDATE transactions SET category_id = NEW.category_id
                        WHERE reimburses_id = NEW.id AND excluded = 0 AND category_id IS NOT NEW.category_id;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS new_payback_takes_the_category AFTER INSERT ON transactions
                    WHEN NEW.reimburses_id IS NOT NULL AND NEW.excluded = 0 BEGIN
                        UPDATE transactions SET category_id = (SELECT p.category_id FROM transactions p WHERE p.id = NEW.reimburses_id) WHERE id = NEW.id;
                    END""",
                 """CREATE TRIGGER IF NOT EXISTS payback_keeps_the_category AFTER UPDATE OF category_id, reimburses_id, excluded ON transactions
                    WHEN NEW.reimburses_id IS NOT NULL AND NEW.excluded = 0 BEGIN
                        UPDATE transactions SET category_id = (SELECT p.category_id FROM transactions p WHERE p.id = NEW.reimburses_id)
                        WHERE id = NEW.id AND category_id IS NOT (SELECT p.category_id FROM transactions p WHERE p.id = NEW.reimburses_id);
                    END"""]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        for name, declaration, *fill in columns:
            if name in _columns(conn, table):
                continue
            # NOTE: a column goes in with what fills it, committed, or not at all, and once: another connection may
            # have added it while this one waited for the write lock
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if name not in _columns(conn, table):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    for statement in fill:
                        conn.execute(statement)
    for statement in ADDED_INDEXES:
        conn.execute(statement)


def connect(path: Path | str) -> sqlite3.Connection:
    """Opens the database at `path`, creating it and any missing tables, and returns the connection."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Request handlers may hop threads between the dependency and the endpoint; each
    # connection is still used by one request at a time.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Returns the stored value for `key`, or `default` if it isn't set."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Stores `value` under `key`, replacing any earlier value."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
