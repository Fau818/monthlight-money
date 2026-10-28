from datetime import date
from html import unescape

from app import links, reports, splits, sync
from app.main import chained, find_transactions, group_by_day, offsetting_together
from tests.test_routes import import_sample


def add(conn, account, day, amount, name, type_="regular"):
    return conn.execute("INSERT INTO transactions (account_id, date, amount, name, type, source, reviewed) VALUES (?, ?, ?, ?, ?, 'manual', 1)",
                        (account, day, amount, name, type_)).lastrowid


def rows(conn, *ids):
    return [conn.execute("SELECT * FROM transactions WHERE id = ?", (i,)).fetchone() for i in ids]


def listed(conn, **criteria):
    """Returns the list as [(day, [(id, how it's drawn)])]: "joined" under the row above, "up" and "down" where the line
    joining linked transactions leaves it."""
    drawn = lambda t: " ".join(k for k, on in (("joined", t["joined"]), ("up", t["chain_up"]), ("down", t["chain_down"])) if on)
    return [(g["date"], [(t["id"], drawn(t)) for t in g["rows"]]) for g in group_by_day(find_transactions(conn, **criteria)[0], date(2026, 10, 14))]


def joes(conn):
    """Returns the ids of the sample's two Trader Joe's purchases, on Sep 1 (54.20) and Sep 8 (61.10)."""
    return [conn.execute("SELECT id FROM transactions WHERE name = 'Trader Joe''s' AND date = ?", (d,)).fetchone()[0] for d in ("2026-09-01", "2026-09-08")]


def test_linked_transactions_show_together_and_count_on_their_own_dates(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    rent, rest = add(conn, account, "2026-09-29", -1000, "Zelle to landlord"), add(conn, account, "2026-09-30", -100, "Zelle to landlord")
    lunch = add(conn, account, "2026-09-30", -20, "Lunch")
    before = reports.month_totals(conn, "2026-09"), reports.daily_spending(conn, "2026-09")
    links.link(conn, rows(conn, rent, rest))
    # The smaller payment is listed after the bigger one, on its day, as its equal: a line joins them, and no row is
    # above them. Each counts on its own day.
    assert listed(conn) == [("2026-09-30", [(lunch, "")]), ("2026-09-29", [(rent, "down"), (rest, "up")])]
    assert (reports.month_totals(conn, "2026-09"), reports.daily_spending(conn, "2026-09")) == before
    # One in another month stays in it, as that's the month it counts in.
    october = add(conn, account, "2026-10-01", -50, "Zelle to landlord")
    links.link(conn, rows(conn, rent, october))
    assert listed(conn, month="2026-10") == [("2026-10-01", [(october, "")])]
    assert listed(conn, month="2026-09") == [("2026-09-30", [(lunch, "")]), ("2026-09-29", [(rent, "down"), (rest, "up")])]
    assert {t["id"]: t["links"] for t in find_transactions(conn)[0]} == {rent: 2, rest: 2, october: 2, lunch: 0}


def test_what_can_be_linked(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    a, b, c, d = (add(conn, account, "2026-09-10", -amount, name) for amount, name in ((40, "A"), (10, "B"), (5, "C"), (15, "D")))
    pay = add(conn, account, "2026-09-11", 500, "Paycheck", "income")
    move = add(conn, account, "2026-09-12", -200, "To savings", "transfer")
    back = add(conn, account, "2026-09-12", 5, "Friend")
    conn.execute("UPDATE transactions SET reimburses_id = ? WHERE id = ?", (a, back))
    assert links.link_problem(conn, rows(conn, a)) == "Pick at least two transactions to link."
    assert links.link_problem(conn, rows(conn, a, move)) is None  # a transfer without its other side can be
    savings = conn.execute("INSERT INTO accounts (name) VALUES ('Savings')").lastrowid
    came = add(conn, savings, "2026-09-12", 200, "From checking", "transfer")
    conn.execute("UPDATE transactions SET transfer_id = CASE id WHEN ? THEN ? ELSE ? END WHERE id IN (?, ?)", (move, came, move, move, came))
    assert "linked with its other side already" in links.link_problem(conn, rows(conn, a, move))
    assert "pays back a purchase" in links.link_problem(conn, rows(conn, a, back))
    assert links.link_problem(conn, rows(conn, a, pay)) is None  # income can be, and a purchase with paybacks
    parts, _ = splits.read_parts(["", ""], ["X", "Y"], ["3", ""], ["", ""], 500, "C")
    splits.save(conn, rows(conn, c)[0], parts)
    part = splits.parts_of(conn, c)[0]
    assert links.link_problem(conn, rows(conn, a, c)) is None  # a split links as a whole
    assert links.link_problem(conn, [rows(conn, a)[0], part]) == "“X” is part of a split. Link the split instead."
    assert [r["id"] for r in links.wholes(conn, [part, rows(conn, a)[0], splits.parts_of(conn, c)[1]])] == [c, a]

    # Linking brings along what each was linked with already; the last one left on its own isn't linked anymore.
    first = links.link(conn, rows(conn, a, b))
    assert links.link(conn, rows(conn, pay, d)) != first
    links.link(conn, rows(conn, b, d))
    assert {r[0] for r in conn.execute("SELECT id FROM transactions WHERE link_id = ?", (first,))} == {a, b, pay, d}
    for tid in (a, b, pay):
        links.unlink(conn, rows(conn, tid)[0])
    assert conn.execute("SELECT count(*) FROM transactions WHERE link_id IS NOT NULL").fetchone()[0] == 0


def test_linking_from_the_list_and_unlinking(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    first, second = joes(conn)
    before = reports.month_totals(conn, "2026-09")
    response = client.post("/transactions/bulk", data={"ids": [str(first)], "action": "link", "back": "/transactions?select=1"})
    assert "Pick at least two transactions to link." in unescape(response.text)
    response = client.post("/transactions/bulk", data={"ids": [str(first), str(second)], "action": "link", "back": "/transactions?month=2026-09"})
    september = unescape(response.text)
    assert "Linked 2 transactions." in september and reports.month_totals(conn, "2026-09") == before
    day = september[september.index("Tue, Sep 8"):]
    assert day.index(f'id="t{second}"') < day.index(f'id="t{first}"') < day.index("</section>")  # after the bigger one
    row_tag = lambda tid: day[day.rindex("<li", 0, day.index(f'id="t{tid}"')):day.index(f'id="t{tid}"')]
    assert "chain-down" in row_tag(second) and "chain-up" in row_tag(first) and "joined" not in row_tag(first)
    assert "on Sep 1" in day[day.index(f'id="t{first}"'):day.index("</section>")] and "🔗 Linked" in day
    assert "<span>Tue, Sep 1</span>" not in september
    page_text = unescape(client.get(f"/transactions/{first}").text)
    assert "Linked with" in page_text and "Together" in page_text and "$115.30" in page_text

    response = client.post(f"/transactions/{first}/unlink", data={"back": f"/transactions/{first}"})
    assert "is no longer linked with the others." in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transactions WHERE link_id IS NOT NULL").fetchone()[0] == 0  # one alone isn't linked
    assert "<span>Tue, Sep 1</span>" in unescape(client.get("/transactions?month=2026-09").text)
    # Unlinking from the list.
    client.post("/transactions/bulk", data={"ids": [str(first), str(second)], "action": "link", "back": "/transactions"})
    response = client.post("/transactions/bulk", data={"ids": [str(second)], "action": "unlink", "back": "/transactions"})
    assert "Unlinked 1 transaction." in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transactions WHERE link_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_a_linked_transaction_isnt_split_combined_or_paired(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    first, second = joes(conn)
    with conn:
        links.link(conn, rows(conn, first, second))
    t = rows(conn, second)[0]
    assert splits.split_problem(conn, t) == "It's linked with other transactions. Unlink it first."
    assert splits.combine_candidates(conn, t) is None
    page_text = unescape(client.get(f"/transactions/{second}").text)
    assert "Split this transaction" not in page_text and "Combine with others into one" not in page_text

    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-09-16", "name": "Zelle from Sam", "amount": "25", "direction": "in",
                                           "account": str(checking), "type": "regular"})
    sam = conn.execute("SELECT id FROM transactions WHERE name = 'Zelle from Sam'").fetchone()[0]
    payroll = conn.execute("SELECT id FROM transactions WHERE name = 'Acme Payroll'").fetchone()[0]
    with conn:
        links.link(conn, rows(conn, sam, payroll))
    assert "Paying you back for a purchase?" not in unescape(client.get(f"/transactions/{sam}").text)
    response = client.post(f"/transactions/{sam}/pays-back", data={"purchase_id": str(second), "back": "/transactions"})
    assert "is linked with other transactions. Unlink it first." in unescape(response.text)
    response = client.post(f"/transactions/{sam}/other-side", data={"other_id": str(second), "back": "/transactions"})
    assert "is linked with other transactions. Unlink it first." in unescape(response.text)
    # Made a transfer, it stays linked, as it has no other side.
    client.post(f"/transactions/{sam}", data={"name": "Zelle from Sam", "type": "transfer", "date": "2026-09-16"})
    sam_link, payroll_link = (r["link_id"] for r in rows(conn, sam, payroll))
    assert sam_link is not None and sam_link == payroll_link
    conn.close()


def test_a_linked_transfer_isnt_paired_with_another_account(conn):
    checking, savings = (conn.execute("INSERT INTO accounts (name) VALUES (?)", (n,)).lastrowid for n in ("Checking", "Savings"))
    out = add(conn, checking, "2026-09-12", -300, "Through a friend", "transfer")
    back = add(conn, savings, "2026-09-12", 300, "From a friend", "transfer")
    other = add(conn, checking, "2026-09-12", -100, "Through a friend too", "transfer")
    links.link(conn, rows(conn, out, other))  # money moved through a friend in two payments
    assert sync.link_transfers(conn) == 0 and rows(conn, out)[0]["transfer_id"] is None
    links.unlink(conn, rows(conn, other)[0])  # unlinked, it pairs with its other side
    assert sync.link_transfers(conn) == 1 and rows(conn, out)[0]["transfer_id"] == back


def test_deleting_a_linked_transaction_unlinks_the_last_one(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    first, _ = joes(conn)
    client.post("/transactions/new", data={"date": "2026-09-02", "name": "Cash at the market", "amount": "8", "direction": "out",
                                           "account": "cash", "type": "regular"})
    cash = conn.execute("SELECT id FROM transactions WHERE name = 'Cash at the market'").fetchone()[0]
    with conn:
        links.link(conn, rows(conn, first, cash))
    response = client.post(f"/transactions/{cash}/delete", data={"back": "/transactions"})
    assert "Deleted “Cash at the market”." in unescape(response.text)
    assert rows(conn, first)[0]["link_id"] is None
    conn.close()


def test_linked_rows_dont_pair_with_amounts_that_cancel_them_out():
    row = lambda tid, amount, link=None: {"id": tid, "amount": amount, "account_id": 1, "type": "regular", "transfer_id": None,
                                          "link_id": link, "links": int(link is not None)}
    listed_rows = [row(1, -100.0, 7), row(2, -80.0), row(3, 100.0), row(4, -20.0, 7)]
    assert [t["id"] for t in offsetting_together(listed_rows)] == [1, 2, 3, 4]  # the 100 in stays below the 80


def test_one_line_joins_linked_rows_past_what_hangs_under_them():
    row = lambda tid, link=None, joined=False: {"id": tid, "link_id": link, "links": int(link is not None), "joined": joined}
    drawn = chained([row(1, 7), row(2, joined=True), row(3, 7), row(4)])  # 2 is a payback of 1
    assert [(t["chain_up"], t["chain_down"], t["chain_through"]) for t in drawn] == [
        (False, True, False), (False, False, True), (True, False, False), (False, False, False)]


def test_a_split_links_as_a_whole(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    _, order = joes(conn)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()[0]
    client.post(f"/transactions/{order}/split", data={"part_id": ["", ""], "part_name": ["Lamp", "Batteries"], "part_amount": ["50", ""],
                                                      "part_category": ["", ""], "back": "/transactions"})
    lamp, batteries = (p["id"] for p in splits.parts_of(conn, order))
    client.post("/transactions/new", data={"date": "2026-09-09", "name": "Warranty", "amount": "6", "direction": "out",
                                           "account": str(card), "type": "regular"})
    plan = conn.execute("SELECT id FROM transactions WHERE name = 'Warranty'").fetchone()[0]
    # Picking one of its parts links the split, the order as the bank charged it.
    response = client.post("/transactions/bulk", data={"ids": [str(lamp), str(plan)], "action": "link", "back": "/transactions?month=2026-09"})
    assert "Linked 2 transactions." in unescape(response.text)
    assert [r["link_id"] is not None for r in rows(conn, order, plan, lamp, batteries)] == [True, True, False, False]
    # The split's row and the plan are equals, a line joining them past the parts; the plan is listed on the order's day.
    [day] = [g for g in group_by_day(find_transactions(conn, month="2026-09")[0], date(2026, 10, 14)) if g["date"] == "2026-09-08"]
    drawn = [(t.get("kind"), t["id"], t["joined"], t["chain_up"], t["chain_down"], t["chain_through"]) for t in day["rows"]]
    assert drawn == [("split", order, False, False, True, False), (None, lamp, True, False, False, True),
                     (None, batteries, True, False, False, True), (None, plan, False, True, False, False)]
    listing = unescape(response.text)
    assert f'id="split{order}" data-link="{order} l' in listing and "on Sep 9" in listing
    # Unlinking from a part unlinks the split.
    response = client.post("/transactions/bulk", data={"ids": [str(batteries)], "action": "unlink", "back": "/transactions"})
    assert "Unlinked 1 transaction." in unescape(response.text)
    assert [r["link_id"] for r in rows(conn, order, plan)] == [None, None]
    conn.close()


def test_ungrouping_a_combined_line_unlinks_it(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Visa')").lastrowid
    a, b = (conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-09-10', ?, ?, 'copilot')",
                         (account, amount, name)).lastrowid for amount, name in ((-12, "Shop: Widget"), (-8, "Shop: Gadget")))
    other = add(conn, account, "2026-09-11", -5, "Shipping")
    group = splits.combine(conn, rows(conn, a)[0], rows(conn, b), "Shop")
    links.link(conn, rows(conn, group, other))
    splits.unsplit(conn, rows(conn, group)[0])
    assert rows(conn, other)[0]["link_id"] is None


def test_a_linked_split_is_listed_on_its_day_with_its_parts(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    order = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-09-06', -30, 'Shop', 'plaid')",
                         (account,)).lastrowid  # the bank's date, a day after the items in the history
    big, small = (conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, split_from_id) VALUES (?, '2026-09-05', ?, 'Shop', 'copilot', ?)",
                               (account, amount, order)).lastrowid for amount in (-20, -10))  # they take the bank's date
    plan = add(conn, account, "2026-09-05", -4, "Warranty")
    links.link(conn, rows(conn, order, plan))
    [day] = group_by_day(find_transactions(conn)[0], date(2026, 10, 14))
    assert (day["date"], [t["id"] for t in day["rows"]]) == ("2026-09-06", [order, big, small, plan])  # the split's row, its parts, the plan
    assert [t["id"] for t in day["rows"] if t.get("date", day["date"]) != day["date"]] == [plan]  # only the plan shows its own date
