from datetime import date

from app import reports


def setup(conn):
    card = conn.execute("INSERT INTO accounts (name, type, balance) VALUES ('Card', 'credit', 300)").lastrowid
    checking = conn.execute("INSERT INTO accounts (name, type, balance) VALUES ('Checking', 'depository', 2000)").lastrowid
    hidden = conn.execute("INSERT INTO accounts (name, type, balance, hidden) VALUES ('Old', 'depository', 999, 1)").lastrowid
    food = conn.execute("INSERT INTO categories (name, budget) VALUES ('Food', 500)").lastrowid
    groceries = conn.execute("INSERT INTO categories (name, parent_id, budget) VALUES ('Groceries', ?, 300)", (food,)).lastrowid
    fun = conn.execute("INSERT INTO categories (name, budget) VALUES ('Fun', 100)").lastrowid
    skip = conn.execute("INSERT INTO categories (name, excluded) VALUES ('Reimbursable', 1)").lastrowid
    rows = [
        (card, "2026-10-02", -120, "Store", groceries, "regular", 0),
        (card, "2026-10-03", -30, "Cafe", food, "regular", 0),
        (card, "2026-10-04", 20, "Store refund", groceries, "regular", 0),
        (card, "2026-10-05", -60, "Concert", fun, "regular", 0),
        (card, "2026-10-06", -45, "Mystery", None, "regular", 0),
        (card, "2026-10-07", -200, "Work trip", skip, "regular", 0),
        (card, "2026-10-08", -80, "Excluded", fun, "regular", 1),
        (checking, "2026-10-09", 3000, "Paycheck", None, "income", 0),
        (checking, "2026-10-10", -500, "Card payment", None, "transfer", 0),
        (hidden, "2026-10-11", -999, "Hidden account", fun, "regular", 0),
        (card, "2026-09-15", -10, "Last month", fun, "regular", 0),
    ]
    conn.executemany(
        "INSERT INTO transactions (account_id, date, amount, name, category_id, type, excluded, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')",
        rows,
    )
    conn.execute("INSERT INTO balance_history VALUES (?, '2026-10-01', 250), (?, '2026-10-01', 1800), (?, '2026-10-02', 2100)", (card, checking, checking))
    return {"food": food, "groceries": groceries, "fun": fun}


def test_month_helpers():
    assert reports.shift_month("2026-01", -1) == "2025-12"
    assert reports.shift_month("2026-12", 1) == "2027-01"
    assert reports.month_range("2026-12") == ("2026-12-01", "2027-01-01")
    assert reports.valid_month("2026-13") is None and reports.valid_month("x") is None
    assert reports.valid_month("2026-02") == "2026-02"


def test_totals_count_only_real_spending(conn):
    setup(conn)
    # 120 + 30 - 20 + 60 + 45: transfers, excluded transactions, excluded categories and hidden accounts don't count.
    assert reports.month_totals(conn, "2026-10") == {"spent": 235.0, "income": 3000.0, "net": 2765.0}


def test_category_spending_rolls_up_groups(conn):
    ids = setup(conn)
    nodes = {n["name"]: n for n in reports.category_spending(conn, "2026-10")}
    assert set(nodes) == {"Food", "Fun", "Uncategorized"}
    food = nodes["Food"]
    assert (food["spent"], food["own_spent"], food["total_budget"]) == (130.0, 30.0, 500)
    assert [(c["name"], c["spent"]) for c in food["children"]] == [("Groceries", 100.0)]
    assert nodes["Uncategorized"]["spent"] == 45.0
    assert reports.budget_total(list(nodes.values())) == 600
    assert next(iter(reports.category_spending(conn, "2026-10")))["name"] == "Food"  # largest first
    assert ids["groceries"] == food["children"][0]["id"]


def test_series_and_net_worth(conn):
    setup(conn)
    series = reports.monthly_series(conn, 3, "2026-10")
    assert [(s["month"], s["spent"]) for s in series] == [("2026-08", 0.0), ("2026-09", 10.0), ("2026-10", 235.0)]
    worth = reports.net_worth(conn)
    assert (worth["assets"], worth["liabilities"], worth["net"]) == (2000.0, 300.0, 1700.0)
    # A card shows what's owed on it as the bank does, positive; its net worth share is negative.
    credit = next(g for g in worth["groups"] if g["kind"] == "credit")
    assert (credit["balance"], credit["total"]) == (300.0, -300.0)
    assert reports.net_worth_history(conn) == [("2026-10-01", 1550.0), ("2026-10-02", 1850.0)]


def test_an_account_kept_by_hand_has_what_its_transactions_leave(conn):
    setup(conn)
    credit = conn.execute("INSERT INTO accounts (name, type, source) VALUES ('Store credit', 'other', 'manual')").lastrowid
    conn.execute("INSERT INTO accounts (name, type, source) VALUES ('Gift card', 'other', 'manual')")
    rows = [
        (credit, "2026-10-01", 100, "Top-up", "transfer", 0),  # bought with money from the bank
        (credit, "2026-10-02", 15, "Refund to the credit", "regular", 0),
        (credit, "2026-10-02", -30, "Outdated copy", "regular", 1),  # excluded: it counts nowhere, here neither
        (credit, "2026-10-03", -40, "Order", "regular", 0),  # split below, and counted through its parts
    ]
    ids = [conn.execute("INSERT INTO transactions (account_id, date, amount, name, type, excluded, source) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
                        r).lastrowid for r in rows]
    conn.execute("INSERT INTO transactions (account_id, date, amount, name, split_from_id, excluded, source) VALUES "
                 "(?, '2026-10-03', -25, 'Part', ?, 0, 'split'), (?, '2026-10-03', -15, 'Excluded part', ?, 1, 'split')",
                 (credit, ids[3], credit, ids[3]))
    worth = reports.net_worth(conn)
    other = next(g for g in worth["groups"] if g["kind"] == "other")
    assert [(a["name"], a["balance"], a["kept_by_hand"]) for a in other["accounts"]] == [("Gift card", 0.0, 1), ("Store credit", 90.0, 1)]
    assert (other["balance"], worth["net"]) == (90.0, 1790.0)
    # Accounts with a balance of their own keep it, even with transactions.
    assert next(a for a in worth["groups"][0]["accounts"] if a["name"] == "Checking")["balance"] == 2000
    # By each day, what was left on it then: 100 on Oct 1, 115 on Oct 2 (the order came later).
    assert reports.net_worth_history(conn) == [("2026-10-01", 1650.0), ("2026-10-02", 1965.0)]


def test_spending_pace_adds_up_day_by_day(conn):
    setup(conn)
    days = reports.daily_spending(conn, "2026-10")
    assert len(days) == 31 and days[:6] == [0.0, 120.0, 30.0, -20.0, 60.0, 45.0]  # counted as in month_totals
    # Mid-month it stops at today, or at the last day with spending if that's later.
    pace = reports.spending_pace(conn, "2026-10", today=date(2026, 10, 5))
    assert pace["this"] == [0.0, 120.0, 150.0, 130.0, 190.0, 235.0]
    assert (len(pace["last"]), pace["last"][14], pace["last_same_day"], pace["days"]) == (30, 10.0, 0.0, 31)
    finished = reports.spending_pace(conn, "2026-10", today=date(2026, 11, 2))
    assert len(finished["this"]) == 31 and finished["this"][-1] == 235.0 and finished["last_same_day"] == 10.0
    assert reports.spending_pace(conn, "2026-12", today=date(2026, 10, 5))["this"] == []


def test_a_payback_counts_with_the_purchase_it_pays_back(conn):
    card = conn.execute("INSERT INTO accounts (name, type) VALUES ('Card', 'credit')").lastrowid
    checking = conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'depository')").lastrowid
    food = conn.execute("INSERT INTO categories (name) VALUES ('Food')").lastrowid
    add = lambda account, day, amount, name, category=None, reimburses=None: conn.execute(
        "INSERT INTO transactions (account_id, date, amount, name, category_id, reimburses_id, source) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
        (account, day, amount, name, category, reimburses)).lastrowid
    purchase = add(card, "2026-03-30", -60.00, "Grocer", food)
    add(checking, "2026-04-02", 30.00, "Zelle from friends", None, purchase)  # paid back the next month, no category of its own
    assert reports.month_totals(conn, "2026-03")["spent"] == 30.0
    assert reports.month_totals(conn, "2026-04")["spent"] == 0.0
    assert [(n["name"], n["spent"]) for n in reports.category_spending(conn, "2026-03")] == [("Food", 30.0)]
    assert reports.daily_spending(conn, "2026-03")[29] == 30.0
    assert [m["spent"] for m in reports.monthly_series(conn, 2, "2026-04")] == [30.0, 0.0]
