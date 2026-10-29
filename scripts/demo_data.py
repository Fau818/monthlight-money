"""Builds a database of made-up transactions, for trying the app or taking screenshots without real data.

Steps:
1. Make the accounts, the categories and their budgets.
2. Add 14 months of transactions up to today: bills, a paycheck every other Friday, everyday spending, paired moves
   between accounts, and tuition twice a year.
3. Add a split, a purchase friends paid back, tags, recurring charges, and daily balances for the net worth chart.
4. Let one budget roll over and another differ for a month; set the tuition aside from everyday spending, and show it
   apart on the dashboard.

Run it with a folder that has no database yet, then point the app at that folder:

    uv run --env-file .env.example python scripts/demo_data.py demo
    DATA_DIR=demo BACKUP_DIR=demo/backups IMPORT_DIR=demo/import uv run --env-file .env.example uvicorn app.main:app --port 8081
"""

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # NOTE: so `app` imports when run as a script

from app import db, recurring, splits, tags

# name, type, last 4 digits, balance today, credit limit
ACCOUNTS = [
    ("Everyday Checking", "depository", "1234", 4820.55, None),
    ("High-Yield Savings", "depository", "5678", 12400.00, None),
    ("Rewards Card", "credit", "4321", 642.18, 8000),
    ("Travel Card", "credit", "8765", 214.90, 5000),
]
# group (None for a category on its own): [(category, monthly budget)]
CATEGORIES = {
    "Food & Drink": [("Groceries", 500), ("Restaurants", 300), ("Coffee", 80)],
    "Transportation": [("Car & Rideshare", 120), ("Gas", 140)],
    "Home": [("Rent", 1850), ("Utilities", 200), ("Household", 120)],
    None: [("Shopping", 250), ("Entertainment", 120), ("Subscriptions", 45), ("Travel", None), ("Health", 60),
           ("Personal Care", 60), ("Education", None)],
}
# day of the month, name, amount (a range for bills that vary), category, account
MONTHLY = [
    (1, "Maple Street Apartments", -1850.00, "Rent", "Everyday Checking"),
    (3, "Streamflix", -15.49, "Subscriptions", "Rewards Card"),
    (9, "Tunebox Music", -10.99, "Subscriptions", "Rewards Card"),
    (12, "Metro Water", (-42, -28), "Utilities", "Everyday Checking"),
    (18, "City Power & Light", (-98, -82), "Utilities", "Everyday Checking"),
    (20, "Fiberline Internet", -60.00, "Utilities", "Everyday Checking"),
    (22, "Cloud Locker", -2.99, "Subscriptions", "Rewards Card"),
]
# names, category, account, average days apart, amount range
EVERYDAY = [
    (["Green Basket Market"], "Groceries", "Rewards Card", 4.5, (38, 95)),
    (["Corner Grocer"], "Groceries", "Everyday Checking", 8, (12, 35)),
    (["Bean There Cafe", "The Daily Grind"], "Coffee", "Rewards Card", 2.5, (4.25, 7.5)),
    (["Taco Fiesta", "Noodle House", "Pizza Palace"], "Restaurants", "Rewards Card", 6, (14, 58)),
    (["QuickRide"], "Car & Rideshare", "Rewards Card", 7, (11, 32)),
    (["FuelUp Station"], "Gas", "Travel Card", 10, (36, 58)),
    (["MegaMart Online"], "Shopping", "Rewards Card", 10, (18, 95)),
    (["Bookworm Books"], "Shopping", "Travel Card", 30, (14, 32)),
    (["Starlight Cinema"], "Entertainment", "Rewards Card", 15, (24, 36)),
    (["Home Goods Depot"], "Household", "Everyday Checking", 30, (25, 70)),
    (["Wellness Pharmacy"], "Health", "Everyday Checking", 30, (9, 30)),
]
PAYCHECK = ("Acme Corp Payroll", 2450.00)
# month and day, amount: a semester's tuition, the kind of big, rare cost that isn't everyday spending
TUITION = [((1, 12), -4850.00), ((8, 28), -4850.00)]
# name, text its transactions hold, typical amount, category
RECURRING = [("Rent", "maple street", -1850.00, "Rent"), ("Streamflix", "streamflix", -15.49, "Subscriptions"),
             ("Tunebox Music", "tunebox", -10.99, "Subscriptions"), ("Fiberline Internet", "fiberline", -60.00, "Utilities"),
             ("City Power & Light", "city power", -90.00, "Utilities"), ("Cloud Locker", "cloud locker", -2.99, "Subscriptions")]


def build(folder: Path, today: date | None = None, seed: int = 2) -> Path:
    """Makes a database of made-up data in `folder` and returns its path. Refuses to touch an existing database."""
    path = folder / "money.db"
    if path.exists():
        raise SystemExit(f"{path} already exists; pick an empty folder.")
    today, rng = today or date.today(), random.Random(seed)
    conn = db.connect(path)
    with conn:
        accounts = {name: conn.execute(
            "INSERT INTO accounts (name, type, mask, balance, credit_limit, balance_updated_at, source) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (name, kind, mask, balance, limit, today.isoformat())).lastrowid for name, kind, mask, balance, limit in ACCOUNTS}
        categories = {}
        for group, members in CATEGORIES.items():
            parent = conn.execute("INSERT INTO categories (name) VALUES (?)", (group,)).lastrowid if group else None
            for name, budget in members:
                categories[name] = conn.execute("INSERT INTO categories (name, parent_id, budget) VALUES (?, ?, ?)",
                                                (name, parent, budget)).lastrowid

        def add(day, name, amount, category=None, account="Everyday Checking", kind="regular", **extra):
            fresh = (today - day).days <= 1  # the last two days wait for review, as new transactions do
            columns = {"account_id": accounts[account], "date": day.isoformat(), "amount": round(amount, 2), "name": name,
                       "category_id": categories.get(category), "type": kind, "reviewed": int(not fresh), "source": "manual",
                       **extra}
            return conn.execute(f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                list(columns.values())).lastrowid

        def move(day, amount, source, target, out_name, in_name):
            out = add(day, out_name, -amount, account=source, kind="transfer", reviewed=1)  # paired moves need no review
            came_in = add(day, in_name, amount, account=target, kind="transfer", reviewed=1)
            conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (came_in, out))
            conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (out, came_in))

        start = recurring.add_months(today.replace(day=1), -13)
        day = start
        while day <= today:
            for when, name, amount, category, account in MONTHLY:
                if day.day == when:
                    add(day, name, rng.uniform(*amount) if isinstance(amount, tuple) else amount, category, account)
            if day.weekday() == 4 and (day - start).days // 7 % 2 == 0:  # every other Friday
                add(day, PAYCHECK[0], PAYCHECK[1], kind="income")
            if day.day == 25:
                move(day, round(rng.uniform(900, 1400), 2), "Everyday Checking", "Rewards Card", "Rewards Card Payment", "Payment Thank You")
            if day.day == 26:
                move(day, 500.00, "Everyday Checking", "High-Yield Savings", "Transfer to Savings", "Transfer from Checking")
            for (month, when), amount in TUITION:
                if (day.month, day.day) == (month, when):
                    add(day, "State University Bursar", amount, "Education")
            for names, category, account, every, (low, high) in EVERYDAY:
                if rng.random() < 1 / every:
                    add(day, rng.choice(names), -rng.uniform(low, high), category, account, pending=int(day == today))
            day += timedelta(days=1)

        # A team dinner two friends paid back, and an online order split into what it held.
        dinner_day = today - timedelta(days=4)
        dinner = add(dinner_day, "Sushi Garden", -96.00, "Restaurants", "Travel Card", notes="Team dinner, split three ways")
        for friend, after in (("Zelle From Alex Kim", 1), ("Venmo From Sam Rivera", 2)):
            add(dinner_day + timedelta(days=after), friend, 32.00, "Restaurants", reimburses_id=dinner)
        order_id = add(today - timedelta(days=2), "MegaMart Online", -142.37, "Shopping", "Rewards Card")
        order = conn.execute("SELECT * FROM transactions WHERE id = ?", (order_id,)).fetchone()
        parts, _ = splits.read_parts(["", "", ""], ["Household supplies", "Snacks", "Shampoo and soap"], ["58.12", "41.25", ""],
                                     [str(categories["Household"]), str(categories["Groceries"]), str(categories["Personal Care"])],
                                     14237, order["name"])
        splits.save(conn, order, parts)
        add(today - timedelta(days=24), "SkyHigh Airlines", -129.00, "Travel", "Travel Card")

        weekend = tags.get_or_create(conn, "Lake weekend", "aqua")
        work = tags.get_or_create(conn, "Work expense", "blue")
        weekend_rows = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE category_id IN (?, ?) ORDER BY date DESC LIMIT 3",
                                                   (categories["Gas"], categories["Restaurants"]))]
        tags.add(conn, weekend, weekend_rows)
        tags.add(conn, work, [dinner])
        for name, pattern, amount, category in RECURRING:
            recurring.track(conn, name, pattern, "monthly", amount, categories[category])
        recurring.track(conn, "Paycheck", PAYCHECK[0].lower(), "biweekly", PAYCHECK[1], None)

        # Restaurants roll over from four months ago, and Shopping had a bigger budget for December alone.
        conn.execute("UPDATE categories SET rollover_since = ? WHERE id = ?",
                     (recurring.add_months(today.replace(day=1), -4).strftime("%Y-%m"), categories["Restaurants"]))
        december = f"{today.year - 1 if today.month < 12 else today.year}-12"
        conn.execute("INSERT INTO budgets (category_id, month, amount) VALUES (?, ?, 400), (?, ?, 250)",
                     (categories["Shopping"], december, categories["Shopping"], f"{int(december[:4]) + 1}-01"))

        # The tuition is set aside from everyday spending, and the dashboard shows it apart.
        db.set_meta(conn, "everyday", json.dumps({"category": [str(categories["Education"])]}))
        db.set_meta(conn, "everyday_shown", "1")

        # Balances by day, walked back from today's, for the net worth chart.
        for name, kind, _, balance, _ in ACCOUNTS:
            value = balance
            for back in range((today - start).days + 1):
                conn.execute("INSERT INTO balance_history (account_id, date, balance) VALUES (?, ?, ?)",
                             (accounts[name], (today - timedelta(days=back)).isoformat(), round(value, 2)))
                value = max(0.0, value - rng.uniform(-60, 60) * (0.2 if kind == "credit" else 1))
    conn.close()
    return path


if __name__ == "__main__":
    made = build(Path(sys.argv[1] if len(sys.argv) > 1 else "demo"))
    folder = made.parent
    print(f"Made {made}. Start the app on it with:\n  DATA_DIR={folder} BACKUP_DIR={folder}/backups IMPORT_DIR={folder}/import "
          "uv run --env-file .env.example uvicorn app.main:app --port 8081")
