"""Choosing a name, category and type for incoming transactions: the user's rules first, then Plaid's category."""

import re
import sqlite3
from dataclasses import dataclass

DEFAULT_CATEGORIES = [
    "Groceries", "Restaurants", "Coffee", "Shopping", "Car & Gas", "Rideshare & Transit",
    "Travel", "Rent", "Utilities", "Subscriptions", "Entertainment", "Health", "Personal Care",
    "Home", "Insurance", "Education", "Pets", "Gifts & Donations", "Fees", "Taxes", "Loans",
    "Income",
]

# Plaid's top-level categories (personal_finance_category v2); detailed codes start with one.
PLAID_PRIMARIES = [
    "INCOME", "LOAN_DISBURSEMENTS", "LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT", "BANK_FEES",
    "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT", "MEDICAL",
    "PERSONAL_CARE", "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "TRANSPORTATION", "TRAVEL",
    "RENT_AND_UTILITIES", "OTHER",
]

# For each Plaid category, words to look for in the user's category names, best first.
# "word*" matches any word starting with "word"; otherwise the whole word (or its plural) must match.
PLAID_HINTS = {
    "FOOD_AND_DRINK_GROCERIES": ["grocer*", "supermarket*"],
    "FOOD_AND_DRINK_COFFEE": ["coffee", "cafe", "restaurant*", "dining", "eating out", "food"],
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": ["alcohol", "liquor", "bar", "drink", "restaurant*", "dining"],
    "FOOD_AND_DRINK_FAST_FOOD": ["fast food", "restaurant*", "dining", "eating out", "food"],
    "FOOD_AND_DRINK": ["restaurant*", "dining", "eating out", "food"],
    "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES": ["clothing", "clothes", "apparel", "shopping"],
    "GENERAL_MERCHANDISE_ELECTRONICS": ["electronic*", "tech", "shopping"],
    "GENERAL_MERCHANDISE_PET_SUPPLIES": ["pet", "shopping"],
    "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES": ["gift", "shopping"],
    "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS": ["book", "shopping"],
    "GENERAL_MERCHANDISE": ["shopping", "merchandise"],
    "HOME_IMPROVEMENT": ["home", "household", "house"],
    "MEDICAL_VETERINARY_SERVICES": ["pet", "vet", "medical", "health*"],
    "MEDICAL": ["medical", "health*", "doctor"],
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": ["fitness", "gym", "personal care", "health*"],
    "PERSONAL_CARE": ["personal care", "beauty", "self care"],
    "ENTERTAINMENT": ["entertainment", "fun", "leisure"],
    "TRANSPORTATION_GAS": ["gas", "fuel", "car", "auto*", "transport*"],
    "TRANSPORTATION_PARKING": ["parking", "car", "auto*", "transport*"],
    "TRANSPORTATION_TOLLS": ["toll", "car", "auto*", "transport*"],
    "TRANSPORTATION_TAXIS_AND_RIDE_SHARES": ["rideshare*", "ride share*", "taxi", "transit", "transport*"],
    "TRANSPORTATION_PUBLIC_TRANSIT": ["transit", "transport*"],
    "TRANSPORTATION": ["transport*", "car", "auto*"],
    "GENERAL_SERVICES_AUTOMOTIVE": ["car", "auto*", "transport*"],
    "TRAVEL": ["travel*", "vacation", "trip"],
    "RENT_AND_UTILITIES_RENT": ["rent", "housing", "mortgage"],
    "RENT_AND_UTILITIES_INTERNET_AND_CABLE": ["internet", "utilit*", "bill"],
    "RENT_AND_UTILITIES_TELEPHONE": ["phone", "utilit*", "bill"],
    "RENT_AND_UTILITIES": ["utilit*", "bill"],
    "GENERAL_SERVICES_INSURANCE": ["insurance", "bill"],
    "GENERAL_SERVICES_EDUCATION": ["education", "school", "tuition"],
    "GENERAL_SERVICES_CHILDCARE": ["childcare", "child care", "kid"],
    "GENERAL_SERVICES": ["service", "bill"],
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS": ["donation", "charity", "gift"],
    "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT": ["tax"],
    "GOVERNMENT_AND_NON_PROFIT": ["tax", "government"],
    "BANK_FEES": ["fee", "bank"],
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT": ["mortgage", "housing", "rent", "loan"],
    "LOAN_PAYMENTS_CAR_PAYMENT": ["car", "auto*", "loan"],
    "LOAN_PAYMENTS": ["loan", "debt"],
    "INCOME_SALARY": ["paycheck", "salary", "income"],
    "INCOME_INTEREST_EARNED": ["interest", "income"],
    "INCOME_DIVIDENDS": ["dividend", "income"],
    "INCOME": ["income"],
}

TYPES = ("regular", "income", "transfer")

# Plaid's confidence levels at which its category is used without asking; lower ones are left for review.
CONFIDENT = {"VERY_HIGH", "HIGH"}


def plaid_primary(detailed: str | None) -> str | None:
    """Returns the top-level Plaid category that a detailed category belongs to."""
    if not detailed:
        return None
    # Longest first, in case Plaid ever adds a primary that is a prefix of another.
    for primary in sorted(PLAID_PRIMARIES, key=len, reverse=True):
        if detailed == primary or detailed.startswith(primary + "_"):
            return primary
    return None


# Money sent or received through apps such as Zelle and Venmo usually goes to or comes from other people, so it
# counts as spending, with money in offsetting it like a friend paying you back. Copilot counted it the same way.
APP_TRANSFERS = {"TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "TRANSFER_IN_TRANSFER_IN_FROM_APPS"}


def plaid_type(detailed: str | None) -> str:
    """Returns the transaction type ('regular', 'income' or 'transfer') that Plaid's category implies."""
    primary = plaid_primary(detailed)
    if primary == "INCOME":
        return "income"
    if detailed in APP_TRANSFERS:
        return "regular"
    if primary in ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_DISBURSEMENTS"):
        return "transfer"
    if detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        return "transfer"
    return "regular"


def keyword_regex(keyword: str) -> re.Pattern:
    """Returns a case-insensitive pattern for `keyword` as PLAID_HINTS writes it: "word*" finds any word starting
    with "word"; otherwise the whole word or its plural must appear."""
    if keyword.endswith("*"):
        return re.compile(r"\b" + re.escape(keyword[:-1]), re.IGNORECASE)
    return re.compile(r"\b" + re.escape(keyword) + r"(s|es)?\b", re.IGNORECASE)


def auto_map_plaid_categories(conn: sqlite3.Connection) -> int:
    """Maps each unmapped Plaid category to a matching user category by name. Returns how many were added."""
    categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    mapped = {r[0] for r in conn.execute("SELECT plaid_category FROM plaid_category_map")}
    added = 0
    for code, keywords in PLAID_HINTS.items():
        if code in mapped:
            continue
        match = next(
            (c["id"] for kw in keywords for c in categories if keyword_regex(kw).search(c["name"])),
            None,
        )
        if match is not None:
            conn.execute("INSERT INTO plaid_category_map (plaid_category, category_id) VALUES (?, ?)", (code, match))
            added += 1
    return added


def ensure_default_categories(conn: sqlite3.Connection) -> bool:
    """Adds the default categories and their Plaid mappings when no categories exist. Returns True if it added them."""
    if conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        return False
    conn.executemany("INSERT INTO categories (name) VALUES (?)", [(n,) for n in DEFAULT_CATEGORIES])
    auto_map_plaid_categories(conn)
    return True


def get_or_create_category(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    """Returns the id of the category with this name (any letter case), creating it if needed."""
    row = conn.execute("SELECT id FROM categories WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO categories (name, parent_id) VALUES (?, ?)", (name, parent_id)).lastrowid


@dataclass
class Rule:
    id: int
    pattern: str
    match: str
    category_id: int | None
    type: str | None
    rename_to: str | None = None

    def matches(self, *names: str | None) -> bool:
        for name in names:
            text = (name or "").strip().lower()
            if not text:
                continue
            if self.match == "equals" and text == self.pattern:
                return True
            if self.match == "contains" and self.pattern in text:
                return True
        return False


class Categorizer:
    """Picks a name, category and type for new transactions; loads the rules and the Plaid map once."""

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute("SELECT id, pattern, match, category_id, type, rename_to FROM rules").fetchall()
        # Exact matches win over "contains" rules, and longer patterns over shorter ones.
        self.rules = sorted(
            (Rule(r["id"], r["pattern"], r["match"], r["category_id"], r["type"], r["rename_to"]) for r in rows),
            key=lambda r: (r.match != "equals", -len(r.pattern)),
        )
        self.plaid_map = dict(conn.execute("SELECT plaid_category, category_id FROM plaid_category_map").fetchall())

    def rule_for(self, *names: str | None, sets: str | None = None) -> Rule | None:
        """Returns the first rule matching any of the names, or None; with `sets`, the first one that sets that part
        ("category_id", "type" or "rename_to"), so a rule that leaves a part as it is lets the next one decide it."""
        return next((r for r in self.rules if r.matches(*names) and (sets is None or getattr(r, sets))), None)

    def rename(self, *names: str | None) -> str | None:
        """Returns the name a rule gives transactions with any of these names, or None when none renames them."""
        rule = self.rule_for(*names, sets="rename_to")
        return rule.rename_to if rule else None

    def categorize(self, name: str | None, original_name: str | None, plaid_category: str | None,
                   confidence: str | None = None, account_type: str | None = None, *,
                   also: tuple[str | None, ...] = ()) -> tuple[int | None, str]:
        """Returns (category_id, type) for a transaction with these names (and the other names in `also`, like the
        bank's own) and Plaid category, on an account of `account_type`. Plaid's category is used only at `confidence`
        HIGH or VERY_HIGH; the type it implies (income, transfer) is used regardless. Income and transfers get no
        category, and everything on a loan account is a transfer, whatever the rules say."""
        if account_type == "loan":
            # A loan account shows the other side of payments made from a bank account, which count as spending
            # there, and the interest those payments cover.
            return None, "transfer"
        type_ = plaid_type(plaid_category)
        category = None
        # An app transfer such as Zelle says nothing about what it paid for, so its category can only come from a rule.
        if type_ == "regular" and plaid_category not in APP_TRANSFERS and confidence in CONFIDENT:
            category = self.plaid_map.get(plaid_category) or self.plaid_map.get(plaid_primary(plaid_category))
        names = (name, original_name, *also)
        if rule := self.rule_for(*names, sets="type"):
            type_ = rule.type
        if rule := self.rule_for(*names, sets="category_id"):
            category = rule.category_id
        return (category if type_ == "regular" else None), type_


def add_rule(conn: sqlite3.Connection, pattern: str, match: str, category_id: int | None, type_: str | None,
             rename_to: str | None = None) -> int:
    """Creates a rule, or replaces the one with the same pattern and match type; a rule that already renames keeps its
    new name unless given one. Returns the rule's id."""
    pattern = pattern.strip().lower()
    conn.execute(
        "INSERT INTO rules (pattern, match, category_id, type, rename_to) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (pattern, match) DO UPDATE SET category_id = excluded.category_id, type = excluded.type, "
        "rename_to = coalesce(excluded.rename_to, rename_to)",
        (pattern, match, category_id, type_, (rename_to or "").strip() or None),
    )
    return conn.execute("SELECT id FROM rules WHERE pattern = ? AND match = ?", (pattern, match)).fetchone()["id"]


def add_rename(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Makes new transactions named `old` (any letter case) come named `new`, keeping what the rule for that name
    already does to their category and type. Rules that renamed others to `old` rename them to `new` instead.
    Returns the rule's id."""
    pattern = old.strip().lower()
    conn.execute("INSERT INTO rules (pattern, match, rename_to) VALUES (?, 'equals', ?) "
                 "ON CONFLICT (pattern, match) DO UPDATE SET rename_to = excluded.rename_to", (pattern, new.strip()))
    conn.execute("UPDATE rules SET rename_to = ? WHERE lower(rename_to) = ? AND pattern != ?", (new.strip(), pattern, pattern))
    return conn.execute("SELECT id FROM rules WHERE pattern = ? AND match = 'equals'", (pattern,)).fetchone()["id"]


def apply_rule(conn: sqlite3.Connection, rule_id: int) -> int:
    """Applies a rule to existing transactions, and returns how many changed:
    - its category and type: to those not edited by hand, excluded, or on loan accounts (see Categorizer.categorize)
    - its new name: to those not renamed by hand"""
    r = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if r["match"] == "equals":
        where = "(lower(trim(name)) = :p OR lower(trim(coalesce(original_name, ''))) = :p)"
    else:
        where = "(instr(lower(name), :p) > 0 OR instr(lower(coalesce(original_name, '')), :p) > 0)"
    values = {"p": r["pattern"], "cat": r["category_id"], "type": r["type"], "name": r["rename_to"]}
    changed = {row[0] for row in conn.execute(
        f"""UPDATE transactions
            SET category_id = coalesce(:cat, category_id), type = coalesce(:type, type),
                excluded = CASE WHEN coalesce(:type, type) = 'regular' THEN excluded ELSE 0 END
            WHERE user_edited = 0 AND excluded = 0 AND {where}
              AND account_id NOT IN (SELECT id FROM accounts WHERE type = 'loan')
              AND (coalesce(category_id, -1) != coalesce(:cat, category_id, -1) OR type != coalesce(:type, type))
            RETURNING id""",
        values,
    )}
    if r["rename_to"]:
        changed |= {row[0] for row in conn.execute(
            f"UPDATE transactions SET name = :name WHERE name_edited = 0 AND {where} AND name != :name RETURNING id", values)}
    return len(changed)
