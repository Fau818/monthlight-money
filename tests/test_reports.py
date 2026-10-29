from datetime import date

from app import reports, tags


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


def report_setup(conn):
    """Two months of made-up spending: a group with a subcategory, a payback, a split, tags, and a hidden account."""
    card = conn.execute("INSERT INTO accounts (name, type) VALUES ('Card', 'credit')").lastrowid
    checking = conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'depository')").lastrowid
    hidden = conn.execute("INSERT INTO accounts (name, type, hidden) VALUES ('Old', 'depository', 1)").lastrowid
    food = conn.execute("INSERT INTO categories (name) VALUES ('Food')").lastrowid
    groceries = conn.execute("INSERT INTO categories (name, parent_id) VALUES ('Groceries', ?)", (food,)).lastrowid
    school = conn.execute("INSERT INTO categories (name) VALUES ('School')").lastrowid
    add = lambda account, day, amount, name, category=None, **extra: conn.execute(
        f"INSERT INTO transactions (account_id, date, amount, name, category_id, source{''.join(', ' + k for k in extra)}) "
        f"VALUES (?, ?, ?, ?, ?, 'manual'{', ?' * len(extra)})", (account, day, amount, name, category, *extra.values())).lastrowid
    ids = {
        "grocer": add(card, "2026-09-03", -80.0, "Green Grocer", groceries),
        "cafe": add(card, "2026-09-04", -20.0, "Corner Cafe", food),
        "tuition": add(checking, "2026-09-10", -1500.0, "City College", school),
        "dinner": add(card, "2026-09-20", -90.0, "Noodle Bar", food),
        "pay": add(checking, "2026-09-15", 3000.0, "Payroll", None, type="income"),
        "grocer2": add(card, "2026-10-02", -60.0, "green grocer", groceries),
        "order": add(card, "2026-10-05", -50.0, "Megastore", None),
        "hidden": add(hidden, "2026-10-06", -999.0, "Hidden", food),
    }
    ids["friend"] = add(checking, "2026-10-01", 30.0, "Zelle from a friend", food, reimburses_id=ids["dinner"])  # counts in September
    ids["part1"] = conn.execute("INSERT INTO transactions (account_id, date, amount, name, category_id, split_from_id, source) "
                                "VALUES (?, '2026-10-05', -30, 'Soap', ?, ?, 'split')", (card, groceries, ids["order"])).lastrowid
    ids["part2"] = conn.execute("INSERT INTO transactions (account_id, date, amount, name, category_id, split_from_id, source) "
                                "VALUES (?, '2026-10-05', -20, 'Snacks', ?, ?, 'split')", (card, food, ids["order"])).lastrowid
    trip = tags.get_or_create(conn, "Trip")
    tags.add(conn, trip, [ids["dinner"], ids["part2"]])
    return {"card": card, "checking": checking, "food": food, "groceries": groceries, "school": school, "trip": trip, **ids}


def test_a_report_adds_up_by_column_and_series(conn):
    ids = report_setup(conn)
    rows = reports.counted(conn, "2026-09-01", "2026-11-01")
    # Counted as the dashboard counts: the payback in September with its purchase, the split by its parts, no hidden account.
    assert round(sum(r["value"] for r in rows), 2) == reports.month_totals(conn, "2026-09")["spent"] + reports.month_totals(conn, "2026-10")["spent"]
    groups = reports.category_groups(conn)
    data = reports.grouped(rows, ["2026-09", "2026-10"], "month", "category", groups)
    by_key = {s["key"]: s for s in data["series"]}
    assert set(by_key) == {str(ids["food"]), str(ids["school"])}  # a subcategory adds up in its group
    assert by_key[str(ids["food"])]["values"] == {"2026-09": 160.0, "2026-10": 110.0}  # 80 + 20 + 90 - 30, then 60 + 30 + 20
    assert [s["key"] for s in data["series"]] == [str(ids["school"]), str(ids["food"])]  # biggest first
    merchants = reports.grouped(rows, ["2026-09", "2026-10"], "month", "merchant", groups)
    assert {s["key"]: s["total"] for s in merchants["series"]}["green grocer"] == 140.0  # whatever the case
    assert {s["key"]: s["total"] for s in merchants["series"]}["megastore"] == 50.0  # a split's parts go by its name
    assert {s["key"]: s["total"] for s in merchants["series"]}["noodle bar"] == 60.0  # a payback by its purchase's
    accounts = {s["key"]: s["total"] for s in reports.grouped(rows, [], "month", "account", groups)["series"]}
    assert accounts == {str(ids["card"]): 270.0, str(ids["checking"]): 1500.0}  # the payback with its purchase's card
    tagged = {s["key"]: s["total"] for s in reports.grouped(rows, [], "month", "tag", groups)["series"]}
    assert tagged == {str(ids["trip"]): 80.0, "none": 1690.0}  # the payback goes by its purchase's tags too
    income = reports.counted(conn, "2026-09-01", "2026-11-01", "income")
    assert [r["value"] for r in income] == [3000.0]


def test_a_report_leaves_out_what_it_is_told(conn):
    ids = report_setup(conn)
    spent = lambda **leave: round(sum(r["value"] for r in reports.counted(conn, "2026-09-01", "2026-11-01", leave=leave)), 2)
    assert spent() == 1770.0
    assert spent(category=[str(ids["food"])]) == 1500.0  # a group with its subcategories
    assert spent(category=[str(ids["groceries"])]) == 1600.0  # 80 + 60 + 30 left out
    assert spent(account=[ids["card"]]) == 1500.0  # the payback, in checking, goes with its purchase on the card
    assert spent(tag=[ids["trip"]]) == 1690.0  # the dinner, its payback and the tagged part
    assert spent(merchant=["GREEN GROCER", "megastore"]) == 1580.0
    assert spent(id=[ids["dinner"]]) == 1710.0  # with its payback
    assert spent(id=[ids["order"]]) == 1720.0  # a split, with its parts
    assert spent(over=1000) == 270.0
    assert spent(over=60) == 130.0  # the 90 dinner and its payback go together; 60 itself stays
    assert reports.totals(conn, "2026-09-01", "2026-10-01", {"category": [str(ids["school"])]}) == \
        {"spent": 160.0, "income": 3000.0, "net": 2840.0, "rate": 94.7}
    merged = reports.merge_leave_outs({"tag": [1], "over": 500}, {"tag": [1, 2], "over": 100})
    assert merged["tag"] == [1, 2] and merged["over"] == 100


def test_past_eight_series_the_smallest_fold_into_other(conn):
    rows = [{"day": "2026-10-01", "value": float(v), "merchant": f"Shop {v}", "category_id": None, "account_id": 1, "tags": []}
            for v in range(1, 11)]
    data = reports.grouped(rows, ["2026-10"], "month", "merchant", {})
    assert [s["key"] for s in data["series"]] == ["shop 10", "shop 9", "shop 8", "shop 7", "shop 6", "shop 5", "shop 4", "other"]
    assert data["series"][-1]["total"] == 6.0 and data["folded"] == ["shop 3", "shop 2", "shop 1"]


def test_columns_are_months_quarters_or_years():
    assert [reports.column_of("2026-02-14", by) for by in ("month", "quarter", "year")] == ["2026-02", "2026-Q1", "2026"]
    assert reports.column_of("2026-12", "quarter") == "2026-Q4"
    assert reports.column_range("2026-Q2") == ("2026-04-01", "2026-07-01")
    assert reports.column_range("2026-Q4") == ("2026-10-01", "2027-01-01")
    assert reports.column_range("2026") == ("2026-01-01", "2027-01-01")
    assert reports.column_range("2026-09") == ("2026-09-01", "2026-10-01")


def test_the_biggest_come_first(conn):
    ids = report_setup(conn)
    assert [r["id"] for r in reports.biggest(conn, "2026-09-01", "2026-11-01", limit=3)] == [ids["tuition"], ids["dinner"], ids["grocer"]]
    dinner = next(r for r in reports.biggest(conn, "2026-09-01", "2026-11-01") if r["id"] == ids["dinner"])
    assert dinner["paid_back"] == 30.0
    assert [r["name"] for r in reports.biggest(conn, "2026-09-01", "2026-11-01", "income")] == ["Payroll"]


def test_what_is_set_aside_from_everyday_spending_is_kept(conn):
    assert reports.everyday(conn) == reports.no_leave_outs()
    reports.set_everyday(conn, {"category": ["3"], "id": [7], "over": 2000.0, "tag": []})
    assert reports.everyday(conn) == {**reports.no_leave_outs(), "category": ["3"], "id": [7], "over": 2000.0}
    conn.execute("UPDATE meta SET value = '[1, 2' WHERE key = 'everyday'")  # unreadable: nothing set aside
    assert reports.everyday(conn) == reports.no_leave_outs()
    conn.execute("""UPDATE meta SET value = '{"id": [1, {"x": 2}], "over": "lots"}' WHERE key = 'everyday'""")
    assert reports.everyday(conn) == {**reports.no_leave_outs(), "id": [1]}


def test_budgets_can_differ_by_month(conn):
    ids = setup(conn)  # Food has a budget of 500, Groceries 300 and Fun 100, every month
    conn.execute("INSERT INTO budgets (category_id, month, amount) VALUES (?, '2026-10', 650), (?, '2026-12', NULL)",
                 (ids["food"], ids["fun"]))
    assert reports.effective_budgets(conn, "2026-09")[ids["food"]] == 500  # before any month of its own
    assert reports.effective_budgets(conn, "2026-10")[ids["food"]] == 650
    assert reports.effective_budgets(conn, "2027-03")[ids["food"]] == 650  # until a later month says otherwise
    assert reports.effective_budgets(conn, "2026-12")[ids["fun"]] is None  # none from December on
    nodes = {n["name"]: n for n in reports.category_spending(conn, "2026-10")}
    assert (nodes["Food"]["budget"], nodes["Food"]["total_budget"]) == (650, 650)


def test_a_budget_that_rolls_over_carries_what_was_left_or_over(conn):
    ids = setup(conn)
    conn.execute("UPDATE categories SET rollover_since = '2026-09' WHERE id IN (?, ?)", (ids["fun"], ids["food"]))
    # Fun: 100 a month; September spent 10, so 90 carries into October, where 60 was spent: 130 left.
    fun = next(n for n in reports.category_spending(conn, "2026-10") if n["name"] == "Fun")
    assert (fun["carry"], fun["total_budget"], fun["spent"], fun["rollover"]) == (90.0, 190.0, 60.0, "2026-09")
    # Into November: 90 + (100 - 60) left. Food counts its groceries too: 500 - 130 in October, nothing in September.
    carry = reports.rollover_carry(conn, "2026-11")
    assert carry == {ids["fun"]: 130.0, ids["food"]: 870.0}
    assert reports.rollover_carry(conn, "2026-09") == {}  # from the month it's switched on, nothing has carried yet
    # Going over carries too, as less to spend.
    conn.execute("UPDATE categories SET budget = 20 WHERE id = ?", (ids["fun"],))
    assert reports.rollover_carry(conn, "2026-11")[ids["fun"]] == -30.0  # 20 - 10, then 20 - 60


def test_a_budget_is_set_from_a_month_on_or_for_that_month_alone(conn):
    ids = setup(conn)  # Food: 500 every month
    budget = lambda m: reports.effective_budgets(conn, m)[ids["food"]]
    assert reports.set_budget(conn, ids["food"], "2026-10", 600, only=True)
    assert [budget(m) for m in ("2026-09", "2026-10", "2026-11", "2027-02")] == [500, 600, 500, 500]
    assert reports.set_budget(conn, ids["food"], "2026-12", 700)  # from December on
    assert [budget(m) for m in ("2026-10", "2026-11", "2026-12", "2027-02")] == [600, 500, 700, 700]
    assert reports.set_budget(conn, ids["food"], "2026-10", 450)  # from October on: the later ones go
    assert [budget(m) for m in ("2026-09", "2026-10", "2026-12")] == [500, 450, 450]
    assert not reports.set_budget(conn, ids["food"], "2026-11", 450)  # the same as it is: nothing written
    assert reports.set_budget(conn, ids["food"], "2026-11", None)
    assert budget("2026-11") is None and budget("2026-10") == 450


def test_an_accounts_balance_over_time(conn):
    setup(conn)
    card = conn.execute("SELECT * FROM accounts WHERE name = 'Card'").fetchone()
    assert reports.account_history(conn, dict(card, kept_by_hand=0)) == [("2026-10-01", 250.0)]
    # Kept by hand: what its transactions had left on it, day by day; on a card, what was owed.
    gift = conn.execute("INSERT INTO accounts (name, type, source) VALUES ('Gift card', 'other', 'manual')").lastrowid
    owed = conn.execute("INSERT INTO accounts (name, type, source) VALUES ('Store card', 'credit', 'manual')").lastrowid
    conn.executemany("INSERT INTO transactions (account_id, date, amount, name, excluded, source) VALUES (?, ?, ?, 'x', ?, 'manual')",
                     [(gift, "2026-10-01", 50, 0), (gift, "2026-10-03", -20, 0), (gift, "2026-10-03", -5, 1), (owed, "2026-10-02", -40, 0)])
    assert reports.account_history(conn, {"id": gift, "type": "other", "kept_by_hand": 1}) == [("2026-10-01", 50.0), ("2026-10-03", 30.0)]
    assert reports.account_history(conn, {"id": owed, "type": "credit", "kept_by_hand": 1}) == [("2026-10-02", 40.0)]
