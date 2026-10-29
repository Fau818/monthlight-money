"""How categories look, as in Copilot: an emoji and a color, chosen from the name unless the user picked them."""

import sqlite3

from .categorize import keyword_regex

# Colors a category can have: the stylesheet's --c-* colors, in the picker's order, around the color wheel. Gray is for
# things that don't need to stand out, like transfers.
COLORS = ("red", "orange", "brown", "yellow", "lime", "green", "aqua", "teal", "sky", "blue", "violet", "purple", "magenta",
          "gray")

# (words to look for in a category's name, emoji, color), most specific first; the first match wins.
# Words follow categorize.PLAID_HINTS: "word*" matches words starting with "word", otherwise the whole word or its plural.
HINTS = [
    (["grocer*", "supermarket*"], "🛒", "green"),
    (["coffee", "cafe", "café", "tea"], "☕", "brown"),
    (["alcohol", "bar", "liquor", "beer", "wine", "nightlife"], "🍺", "yellow"),
    (["fast food"], "🍟", "orange"),
    (["restaurant*", "dining", "eating out", "food", "takeout", "take out", "lunch", "dinner", "meal", "delivery"], "🍔", "orange"),
    (["income", "paycheck*", "salary", "salaries", "payroll", "wage", "earning*", "bonus", "refund", "reimburse*", "dividend"], "💰", "green"),
    (["saving*", "invest*", "retirement", "401k", "ira"], "📈", "lime"),
    (["rideshare*", "ride share*", "taxi", "uber", "lyft"], "🚕", "yellow"),
    (["parking", "toll"], "🅿️", "sky"),
    (["car", "auto*", "vehicle", "transport*"], "🚗", "blue"),
    (["transit", "train", "subway", "metro", "bus", "commut*"], "🚇", "sky"),
    (["travel*", "vacation*", "trip", "flight", "airline", "hotel", "lodging", "airbnb", "holiday"], "✈️", "teal"),
    (["rent", "housing", "mortgage", "apartment"], "🏠", "violet"),
    (["utilit*", "electric*", "water", "power", "energy", "bill"], "💡", "yellow"),
    (["gas", "fuel"], "⛽", "blue"),
    (["internet", "phone", "mobile", "cell*", "wifi", "cable"], "📱", "sky"),
    (["subscription*", "streaming", "software", "membership", "app"], "📺", "purple"),
    (["entertainment", "fun", "movie", "cinema", "game", "gaming", "music", "concert", "event", "leisure", "hobb*"], "🎉", "purple"),
    (["clothing", "clothes", "apparel", "fashion", "shoe"], "👕", "magenta"),
    (["electronic*", "tech", "gadget", "computer"], "💻", "blue"),
    (["shop*", "merchandise", "amazon", "retail"], "🛍️", "magenta"),
    (["fitness", "gym", "sport*", "workout", "exercise"], "🏋️", "lime"),
    (["health*", "medical", "doctor", "pharmac*", "dental", "dentist", "hospital", "medicine", "wellness", "therapy"], "🏥", "aqua"),
    (["personal care", "beauty", "hair*", "self care", "spa", "cosmetic*", "salon", "grooming"], "💅", "magenta"),
    (["home", "household", "house", "furniture", "garden*", "decor"], "🏡", "green"),
    (["insurance"], "🛡️", "blue"),
    (["education", "school", "tuition", "book", "course", "class", "learning", "college", "universit*", "student"], "🎓", "violet"),
    (["kid", "child*", "baby", "babies", "family", "daycare"], "🧸", "yellow"),
    (["pet", "vet", "dog", "cat", "animal"], "🐾", "brown"),
    (["gift", "donation", "charit*", "giving", "church"], "🎁", "magenta"),
    (["fee", "charge", "penalt*"], "💸", "red"),
    (["tax", "irs"], "🧾", "violet"),
    (["loan", "debt", "credit card"], "🏦", "blue"),
    (["work", "business", "office", "professional"], "💼", "brown"),
    (["cash", "atm"], "💵", "green"),
    (["transfer*"], "↔️", "gray"),
    (["other", "misc*", "general", "uncategorized"], "📦", "gray"),
]
_COMPILED = [([keyword_regex(word) for word in words], emoji, color) for words, emoji, color in HINTS]

FALLBACK_EMOJI = "🏷️"
# Colors for categories nothing suggests anything for, or whose suggestion another category already has, in the order
# they're handed out. Red is left out: next to a budget it would read as "over".
FALLBACK_COLORS = ("blue", "orange", "aqua", "yellow", "magenta", "green", "violet", "lime", "purple", "teal", "brown", "sky")
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
    """Returns {category id: {"emoji", "color"}} for every category.
    - emoji: the user's pick, else what the name suggests, else what its group's name suggests, else a tag
    - color: the user's pick, else the suggested one unless a category ahead of it has it, else a color none has
      (once every color is taken, the suggested one after all, or one that depends on its id)
    """
    rows = {r["id"]: r for r in conn.execute("SELECT id, name, parent_id, emoji, color FROM categories")}
    groups = {row["parent_id"] for row in rows.values()}
    looks, wanted = {}, []
    for cid, row in rows.items():
        own, parent = suggest(row["name"]), rows.get(row["parent_id"])
        suggested = own or (suggest(parent["name"]) if parent else None)
        looks[cid] = {"emoji": (row["emoji"] or "").strip() or (suggested[0] if suggested else FALLBACK_EMOJI)}
        if row["color"] in COLORS:
            looks[cid]["color"] = row["color"]
        else:
            # NOTE: transactions sit on the categories with none under them, so those whose own names suggest a color
            # go ahead of groups and the rest, the oldest first
            wanted.append((cid in groups or own is None, cid, suggested[1] if suggested else None))
    wanted.sort()
    # Two categories in one color look alike in the list, so each color goes to one category only. Gray is meant not
    # to stand out, so any number can have it.
    taken = {look["color"] for look in looks.values() if "color" in look}
    for _, cid, color in wanted:
        if color and (color == "gray" or color not in taken):
            looks[cid]["color"] = color
            taken.add(color)
    free = [color for color in FALLBACK_COLORS if color not in taken]
    for _, cid, color in wanted:
        if "color" not in looks[cid]:
            looks[cid]["color"] = free.pop(0) if free else color or FALLBACK_COLORS[(cid - 1) % len(FALLBACK_COLORS)]
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
