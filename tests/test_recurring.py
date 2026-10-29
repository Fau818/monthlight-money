from datetime import date, timedelta

from app import recurring


def add(conn, account, day, name, amount):
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, ?, ?, ?, 'plaid')",
        (account, day.isoformat(), amount, name),
    )


def test_frequency_and_next_date():
    days = [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 16)]
    assert recurring.infer_frequency(days) == "monthly"
    assert recurring.infer_frequency([date(2026, 1, 1), date(2026, 1, 15), date(2026, 1, 29)]) == "biweekly"
    assert recurring.infer_frequency([date(2025, 3, 1), date(2026, 3, 1)]) == "yearly"
    assert recurring.next_date(date(2026, 1, 31), "monthly") == date(2026, 2, 28)
    assert recurring.next_date(date(2026, 11, 30), "quarterly") == date(2027, 2, 28)
    assert recurring.next_date(date(2026, 1, 1), "weekly") == date(2026, 1, 8)


def test_find_candidates_spots_steady_charges_only(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    today = date(2026, 10, 14)
    for months_back in range(5):
        add(conn, account, today - timedelta(days=30 * months_back + 2), "Spotify", -11.99)
    for days_back in (3, 9, 40, 41, 100):  # irregular
        add(conn, account, today - timedelta(days=days_back), "Uber", -18.0)
    for weeks_back in range(6):  # weekly but the amount swings
        add(conn, account, today - timedelta(weeks=weeks_back), "Trader Joe's", -20.0 - weeks_back * 25)
    for months_back in range(4, 9):  # monthly but stopped months ago
        add(conn, account, today - timedelta(days=30 * months_back), "Old Gym", -40.0)
    found = recurring.find_candidates(conn, today)
    assert [(c["name"], c["frequency"], c["amount"]) for c in found] == [("Spotify", "monthly", -11.99)]


def test_track_links_past_charges_and_matcher(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    add(conn, account, date(2026, 9, 1), "Netflix.com", -15.49)
    add(conn, account, date(2026, 9, 2), "Netflix.com", 15.49)  # a refund, not the charge
    add(conn, account, date(2026, 9, 3), "Netflix gift card", -100.0)  # too far off the usual amount
    rid, linked = recurring.track(conn, "Netflix", "Netflix", "monthly", -15.49, None)
    assert linked == 1
    m = recurring.Matcher(conn)
    assert m.match("NETFLIX.COM", -17.99) == rid
    assert m.match("Netflix.com", 15.49) is None

    [item] = recurring.list_recurring(conn, today=date(2026, 9, 20))
    assert (item["last_date"], item["next_date"], item["late"]) == (date(2026, 9, 1), date(2026, 10, 1), False)
    assert recurring.upcoming(conn, 30, today=date(2026, 9, 20))[0]["id"] == rid
    assert recurring.list_recurring(conn, today=date(2026, 10, 20))[0]["late"] is True
    assert round(recurring.monthly_cost(-120, "yearly"), 2) == 10.0


def test_month_summary_splits_paid_and_still_due(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    add(conn, account, date(2026, 9, 1), "Netflix", -15.49)
    add(conn, account, date(2026, 10, 1), "Netflix", -15.49)
    add(conn, account, date(2026, 9, 20), "Gym", -40.0)
    recurring.track(conn, "Netflix", "netflix", "monthly", -15.49, None)
    recurring.track(conn, "Gym", "gym", "monthly", -40.0, None)
    today = date(2026, 10, 14)
    summary = recurring.month_summary(conn, recurring.list_recurring(conn, today), today)
    assert summary["paid"] == 15.49  # Netflix on Oct 1; its next charge is in November
    assert summary["due"] == 40.0 and [r["name"] for r in summary["due_items"]] == ["Gym"]


def test_charges_come_back_on_their_day_after_a_shorter_month():
    assert recurring.step(date(2026, 1, 31), "monthly", 1) == date(2026, 2, 28)
    assert recurring.step(date(2026, 1, 31), "monthly", 2) == date(2026, 3, 31)  # not the 28th
    assert recurring.step(date(2026, 1, 2), "biweekly", 3) == date(2026, 2, 13)
    item = {"active": 1, "late": False, "last_date": date(2026, 9, 3), "frequency": "monthly"}
    assert recurring.occurrences(item, date(2026, 10, 1), date(2027, 1, 1)) == [date(2026, 10, 3), date(2026, 11, 3), date(2026, 12, 3)]
    assert recurring.occurrences({**item, "active": 0}, date(2026, 10, 1), date(2027, 1, 1)) == []
    assert recurring.occurrences({**item, "late": True}, date(2026, 10, 1), date(2027, 1, 1)) == []


def test_a_month_of_recurring_charges_and_the_next_payday(conn):
    account = conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'depository')").lastrowid
    add(conn, account, date(2026, 10, 2), "Acme Payroll", 2000)
    add(conn, account, date(2026, 9, 25), "Netflix", -15.49)
    add(conn, account, date(2026, 10, 1), "Rent", -1500)
    pay, _ = recurring.track(conn, "Payroll", "acme payroll", "biweekly", 2000, None)
    netflix, _ = recurring.track(conn, "Netflix", "netflix", "monthly", -15.49, None)
    rent, _ = recurring.track(conn, "Rent", "rent", "monthly", -1500, None)
    today = date(2026, 10, 10)
    month = recurring.calendar_month(conn, recurring.list_recurring(conn, today), "2026-10", today)
    days = [(e["day"].day, e["name"], e["done"]) for e in month["events"]]
    # What came before today, then what's expected: paychecks every other week, Netflix on the 25th.
    assert days == [(1, "Rent", True), (2, "Payroll", True), (16, "Payroll", False), (25, "Netflix", False), (30, "Payroll", False)]
    assert [len(w) for w in month["weeks"]] == [7] * 5 and month["weeks"][0][0]["date"] == date(2026, 9, 27)  # Sunday first
    assert month["weeks"][1][6]["today"] and not month["weeks"][0][0]["in_month"]
    # The next payday is the 16th; nothing is due before it. After it, Netflix is.
    assert month["payday"]["day"] == date(2026, 10, 16) and month["due_before_payday"] == 0
    # A paycheck a day late is still the next payday; once it came, the one after is, with Netflix due before it.
    late = recurring.calendar_month(conn, recurring.list_recurring(conn, date(2026, 10, 17)), "2026-10", date(2026, 10, 17))
    assert late["payday"]["day"] == date(2026, 10, 16) and (16, "Payroll", False) in [(e["day"].day, e["name"], e["done"]) for e in late["events"]]
    conn.execute("INSERT INTO transactions (account_id, date, amount, name, recurring_id, source) VALUES (?, '2026-10-16', 2000, 'Acme Payroll', ?, 'plaid')",
                 (account, pay))
    later = recurring.calendar_month(conn, recurring.list_recurring(conn, date(2026, 10, 17)), "2026-10", date(2026, 10, 17))
    assert later["payday"]["day"] == date(2026, 10, 30) and later["due_before_payday"] == 15.49
    assert [e["item"]["id"] for e in later["before_payday"]] == [netflix] and rent


def test_paydays_on_one_day_and_a_split_charge_that_came(conn):
    account = conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'depository')").lastrowid
    add(conn, account, date(2026, 10, 1), "Job one", 1000)
    add(conn, account, date(2026, 10, 1), "Job two", 500)
    bill = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-10-05', -80, 'Phone bill', 'plaid')",
                        (account,)).lastrowid
    for name, pattern, amount in (("Job one", "job one", 1000), ("Job two", "job two", 500), ("Phone", "phone bill", -80)):
        recurring.track(conn, name, pattern, "monthly", amount, None)
    # Split, the bill keeps its link to the recurring charge on the transaction it's split from; its parts have none.
    conn.execute("INSERT INTO transactions (account_id, date, amount, name, split_from_id, source) VALUES (?, '2026-10-05', -50, 'Line', ?, 'split'), "
                 "(?, '2026-10-05', -30, 'Phone', ?, 'split')", (account, bill, account, bill))
    month = recurring.calendar_month(conn, recurring.list_recurring(conn, date(2026, 10, 20)), "2026-10", date(2026, 10, 20))
    assert ("Phone", True) in [(e["name"], e["done"]) for e in month["events"]]
    assert month["payday"]["day"] == date(2026, 11, 1)  # both jobs pay that day: no error comparing them
