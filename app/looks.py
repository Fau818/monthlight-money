"""How categories look, as in Copilot: an emoji and a color, chosen from the name unless the user picked them."""

import sqlite3

from .categorize import keyword_regex

# Colors a category can have: the stylesheet's --c-* colors. The first eight are the chart palette, in its fixed
# order; gray is for things that don't need to stand out, like transfers.
COLORS = ("blue", "orange", "aqua", "yellow", "magenta", "green", "violet", "red", "gray")

# (words to look for in a category's name, emoji, color), most specific first; the first match wins.
# Words follow categorize.PLAID_HINTS: "word*" matches words starting with "word", otherwise the whole word or its plural.
HINTS = [
    (["grocer*", "supermarket*"], "🛒", "green"),
    (["coffee", "cafe", "café", "tea"], "☕", "orange"),
    (["alcohol", "bar", "liquor", "beer", "wine", "nightlife"], "🍺", "orange"),
    (["fast food"], "🍟", "orange"),
    (["restaurant*", "dining", "eating out", "food", "takeout", "take out", "lunch", "dinner", "meal", "delivery"], "🍔", "orange"),
    (["income", "paycheck*", "salary", "salaries", "payroll", "wage", "earning*", "bonus", "refund", "reimburse*", "dividend"], "💰", "green"),
    (["saving*", "invest*", "retirement", "401k", "ira"], "📈", "green"),
    (["rideshare*", "ride share*", "taxi", "uber", "lyft"], "🚕", "yellow"),
    (["parking", "toll"], "🅿️", "blue"),
    (["car", "auto*", "vehicle", "transport*"], "🚗", "blue"),
    (["transit", "train", "subway", "metro", "bus", "commut*"], "🚇", "blue"),
    (["travel*", "vacation*", "trip", "flight", "airline", "hotel", "lodging", "airbnb", "holiday"], "✈️", "aqua"),
    (["rent", "housing", "mortgage", "apartment"], "🏠", "violet"),
    (["utilit*", "electric*", "water", "power", "energy", "bill"], "💡", "yellow"),
    (["gas", "fuel"], "⛽", "blue"),
    (["internet", "phone", "mobile", "cell*", "wifi", "cable"], "📱", "blue"),
    (["subscription*", "streaming", "software", "membership", "app"], "📺", "violet"),
    (["entertainment", "fun", "movie", "cinema", "game", "gaming", "music", "concert", "event", "leisure", "hobb*"], "🎉", "magenta"),
    (["clothing", "clothes", "apparel", "fashion", "shoe"], "👕", "magenta"),
    (["electronic*", "tech", "gadget", "computer"], "💻", "blue"),
    (["shop*", "merchandise", "amazon", "retail"], "🛍️", "magenta"),
    (["fitness", "gym", "sport*", "workout", "exercise"], "🏋️", "aqua"),
    (["health*", "medical", "doctor", "pharmac*", "dental", "dentist", "hospital", "medicine", "wellness", "therapy"], "🏥", "aqua"),
    (["personal care", "beauty", "hair*", "self care", "spa", "cosmetic*", "salon", "grooming"], "💅", "magenta"),
    (["home", "household", "house", "furniture", "garden*", "decor"], "🏡", "green"),
    (["insurance"], "🛡️", "blue"),
    (["education", "school", "tuition", "book", "course", "class", "learning", "college", "universit*", "student"], "🎓", "violet"),
    (["kid", "child*", "baby", "babies", "family", "daycare"], "🧸", "yellow"),
    (["pet", "vet", "dog", "cat", "animal"], "🐾", "orange"),
    (["gift", "donation", "charit*", "giving", "church"], "🎁", "magenta"),
    (["fee", "charge", "penalt*"], "💸", "red"),
    (["tax", "irs"], "🧾", "violet"),
    (["loan", "debt", "credit card"], "🏦", "blue"),
    (["work", "business", "office", "professional"], "💼", "blue"),
    (["cash", "atm"], "💵", "green"),
    (["transfer*"], "↔️", "gray"),
    (["other", "misc*", "general", "uncategorized"], "📦", "gray"),
]
_COMPILED = [([keyword_regex(word) for word in words], emoji, color) for words, emoji, color in HINTS]

FALLBACK_EMOJI = "🏷️"
# Colors for categories nothing suggests anything for. Red is left out: next to a budget it would read as "over".
FALLBACK_COLORS = ("blue", "orange", "aqua", "yellow", "magenta", "green", "violet")
INCOME = {"emoji": "💰", "color": "green"}
TRANSFER = {"emoji": "↔️", "color": "gray"}  # money moving between the user's accounts, not a repeat
UNCATEGORIZED = {"emoji": "", "color": "gray"}  # drawn as a question mark
EXCLUDED = {"emoji": "🚫", "color": "gray"}  # left out of spending, in place of a category


def suggest(name: str) -> tuple[str, str] | None:
    """Returns the (emoji, color) for the first hint whose words appear in a category name, or None if none do."""
    for patterns, emoji, color in _COMPILED:
        if any(p.search(name) for p in patterns):
            return emoji, color
    return None


def category_looks(conn: sqlite3.Connection) -> dict[int, dict]:
    """Returns {category id: {"emoji", "color"}} for every category. Each part is the user's pick if they made one,
    else what the name suggests, else what its group's name suggests, else a tag in a color that depends on its id."""
    rows = {r["id"]: r for r in conn.execute("SELECT id, name, parent_id, emoji, color FROM categories")}
    looks = {}
    for cid, row in rows.items():
        parent = rows.get(row["parent_id"])
        suggested = suggest(row["name"]) or (suggest(parent["name"]) if parent else None)
        emoji, color = suggested or (FALLBACK_EMOJI, FALLBACK_COLORS[(cid - 1) % len(FALLBACK_COLORS)])
        looks[cid] = {
            "emoji": (row["emoji"] or "").strip() or emoji,
            "color": row["color"] if row["color"] in COLORS else color,
        }
    return looks


def transaction_look(looks: dict[int, dict], type_: str, category_id: int | None, excluded: bool = False) -> dict:
    """Returns the emoji and color to show for a transaction of `type_` in `category_id`, or excluded from spending."""
    if excluded and type_ == "regular":
        return EXCLUDED
    if type_ == "income":
        return INCOME
    if type_ == "transfer":
        return TRANSFER
    return looks.get(category_id, UNCATEGORIZED) if category_id is not None else UNCATEGORIZED
