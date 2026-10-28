from datetime import date, timedelta
from html import unescape

from app import recurring, reports, splits, sync, tags
from tests.conftest import FakePlaid, page, plaid_txn
from tests.test_routes import import_sample


def joes_and_categories(conn):
    joes, groceries = conn.execute("SELECT id, category_id FROM transactions WHERE name = 'Trader Joe''s' AND date = '2026-09-08'").fetchone()
    shopping = conn.execute("SELECT id FROM categories WHERE name = 'Shopping'").fetchone()[0]
    return joes, groceries, shopping


def groceries_spent(conn, month):
    return next(ch["spent"] for n in reports.category_spending(conn, month) for ch in n["children"] if ch["name"] == "Groceries")


def test_a_purchase_splits_into_parts_that_count_instead(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, groceries, shopping = joes_and_categories(conn)
    before = reports.month_totals(conn, "2026-09")
    page_text = unescape(client.get(f"/transactions/{joes}").text)
    assert "Split this transaction" in page_text and 'data-total="6110"' in page_text

    # One part without an amount gets what's left.
    response = client.post(f"/transactions/{joes}/split", data={
        "part_id": ["", ""], "part_name": ["Trader Joe's", "Flowers"], "part_amount": ["$41.10", ""],
        "part_category": [str(groceries), str(shopping)], "back": f"/transactions/{joes}"})
    assert "Split “Trader Joe's” into 2 parts." in unescape(response.text)
    parts = splits.parts_of(conn, joes)
    assert [(p["name"], p["amount"], p["category_id"], p["source"], p["date"], p["reviewed"]) for p in parts] == [
        ("Trader Joe's", -41.1, groceries, "split", "2026-09-08", 1), ("Flowers", -20.0, shopping, "split", "2026-09-08", 1)]
    assert tuple(conn.execute("SELECT reviewed, user_edited FROM transactions WHERE id = ?", (joes,)).fetchone()) == (1, 1)
    assert reports.month_totals(conn, "2026-09") == before  # the parts add up to the purchase
    assert groceries_spent(conn, "2026-09") == round(54.20 + 41.10, 2)

    # The list shows a row for the split, and its parts under it, side by side.
    september = unescape(client.get("/transactions?month=2026-09").text)
    head, first, second = (september.index(f'id="{i}"') for i in (f"split{joes}", f't{parts[0]["id"]}', f't{parts[1]["id"]}'))
    assert f'id="t{joes}"' not in september and head < first < second and "split into 2" in september
    rows = lambda i: september[september.rindex("<li", 0, i):i]
    assert "joined" in rows(first) and "joined" in rows(second) and "✂ Split</a>" not in september
    assert 'Tue, Sep 8</span><span>$61.10' in september
    # Filtering by a category shows only the part in it.
    flowers = unescape(client.get(f"/transactions?month=2026-09&category={shopping}").text)
    assert f'id="t{parts[1]["id"]}"' in flowers and f'id="split{joes}"' in flowers and f'id="t{parts[0]["id"]}"' not in flowers
    assert "Split into 2 parts" in unescape(client.get(f"/transactions/{joes}").text)
    part_page = unescape(client.get(f"/transactions/{parts[1]['id']}").text)
    assert "Part of a split" in part_page and "Split this transaction" not in part_page and "data-recurring-field" not in part_page

    # Changing the split: new amounts and a third part, and emptying a row removes its part.
    p1, p2 = (p["id"] for p in parts)
    client.post(f"/transactions/{joes}/split", data={
        "part_id": [str(p1), str(p2), ""], "part_name": ["Trader Joe's", "", "Wine"], "part_amount": ["50.00", "", "11.10"],
        "part_category": [str(groceries), "", ""], "back": f"/transactions/{joes}"})
    assert [(p["id"] == p1, p["name"], p["amount"]) for p in splits.parts_of(conn, joes)] == [(True, "Trader Joe's", -50.0), (False, "Wine", -11.1)]
    assert conn.execute("SELECT count(*) FROM transactions WHERE name = 'Flowers'").fetchone()[0] == 0  # made by splitting, so deleted

    response = client.post(f"/transactions/{joes}/unsplit", data={"back": f"/transactions/{joes}"})
    assert "counts as one transaction again" in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transactions WHERE source = 'split'").fetchone()[0] == 0
    assert reports.month_totals(conn, "2026-09") == before and groceries_spent(conn, "2026-09") == round(54.20 + 61.10, 2)
    assert f'id="t{joes}"' in client.get("/transactions?month=2026-09").text
    conn.close()


def test_a_split_has_to_add_up(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)

    def split(tid, names, amounts):
        return unescape(client.post(f"/transactions/{tid}/split", data={
            "part_id": [""] * len(names), "part_name": names, "part_amount": amounts, "part_category": [""] * len(names),
            "back": f"/transactions/{tid}"}).text)

    assert "The parts add up to $50.00, but the transaction is $61.10." in split(joes, ["A", "B"], ["25", "25"])
    assert "all the parts but one" in split(joes, ["A", "B"], ["", ""])
    assert "nothing is left" in split(joes, ["A", "B"], ["61.10", ""])
    assert "“-5” isn't an amount" in split(joes, ["A", "B"], ["-5", ""])
    assert "above zero" in split(joes, ["A", "B"], ["0", ""])
    assert "at least two parts" in split(joes, ["A", ""], ["61.10", ""])
    assert conn.execute("SELECT count(*) FROM transactions WHERE split_from_id IS NOT NULL").fetchone()[0] == 0

    # Moves between accounts and paybacks can't be split; a part can't be split again.
    payment = conn.execute("SELECT id FROM transactions WHERE name = 'Chase Payment'").fetchone()[0]
    assert "Split this transaction" not in unescape(client.get(f"/transactions/{payment}").text)
    assert "can't be split" in split(payment, ["A", "B"], ["400", ""])
    split(joes, ["A", "B"], ["30", ""])
    part = splits.parts_of(conn, joes)[0]["id"]
    assert "already part of a split" in split(part, ["A", "B"], ["10", ""])
    conn.close()


def test_a_split_transaction_links_to_nothing(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["A", "B"], "part_amount": ["30", ""],
                                                     "part_category": ["", ""]})
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-09-10", "name": "Zelle from Sam", "amount": "15", "direction": "in",
                                           "account": str(checking), "type": "regular"})
    sam = conn.execute("SELECT id FROM transactions WHERE name = 'Zelle from Sam'").fetchone()[0]
    offered = unescape(client.get(f"/transactions/{sam}").text)
    assert "Sep 8 · Trader Joe's · $61.10" not in offered and "Sep 8 · A · $30.00" in offered  # its parts are offered instead...
    response = client.post(f"/transactions/{sam}/pays-back", data={"purchase_id": str(joes)})
    assert "is split into parts. Link one of its parts instead." in unescape(response.text)  # ...nor linked
    part = splits.parts_of(conn, joes)[0]["id"]
    client.post(f"/transactions/{sam}/pays-back", data={"purchase_id": str(part)})
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (sam,)).fetchone()[0] == part
    conn.close()


def test_a_split_follows_the_bank(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("dinner", 50.0, "2026-10-08", "DINNER"), plaid_txn("store", 30.0, "2026-10-09", "STORE")], "c1"),
        "c1": page([], "c2", modified=[plaid_txn("dinner", 60.0, "2026-10-08", "DINNER")], removed=["store"]),
    })
    sync.link_item(conn, fake, "public-sandbox-1", {"institution_id": "ins_1", "name": "Tartan"})
    sync.sync_item(conn, fake, "item-1")
    rows = {r["plaid_transaction_id"]: r for r in conn.execute("SELECT * FROM transactions")}
    with conn:
        for t in rows.values():
            parts, problem = splits.read_parts(["", ""], ["Mine", "Theirs"], ["", "10.00"], ["", ""], round(abs(t["amount"]) * 100), t["name"])
            assert problem is None
            splits.save(conn, t, parts)
        # A row from the history, two days before the bank's date, that became a part of the store charge (as an import
        # from Copilot can leave them): it's dated like the charge.
        kept = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-10-07', -1, 'Old', 'copilot')",
                            (rows["store"]["account_id"],)).lastrowid
        conn.execute("UPDATE transactions SET split_from_id = ? WHERE id = ?", (rows["store"]["id"], kept))
    assert conn.execute("SELECT date FROM transactions WHERE id = ?", (kept,)).fetchone()[0] == "2026-10-09"
    assert reports.month_totals(conn, "2026-10")["spent"] == 50 + 30 + 1

    sync.sync_item(conn, fake, "item-1")
    # The bank changed the dinner (a tip, say): its parts no longer add up, so it's back on the review list.
    dinner = conn.execute("SELECT * FROM transactions WHERE plaid_transaction_id = 'dinner'").fetchone()
    assert (dinner["amount"], dinner["reviewed"]) == (-60.0, 0)
    assert sum(p["amount"] for p in splits.parts_of(conn, dinner["id"])) == -50.0
    # The bank removed the store charge: the parts made by splitting it go too, the row from the history stays.
    assert conn.execute("SELECT count(*) FROM transactions WHERE plaid_transaction_id = 'store'").fetchone()[0] == 0
    assert [p["name"] for p in conn.execute("SELECT name FROM transactions WHERE source = 'split' ORDER BY id")] == ["Mine", "Theirs"]
    assert tuple(conn.execute("SELECT split_from_id, date FROM transactions WHERE id = ?", (kept,)).fetchone()) == (None, "2026-10-07")  # its own date back
    assert reports.month_totals(conn, "2026-10")["spent"] == 50 + 1


def test_a_split_that_no_longer_adds_up_shows_on_the_review_list(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["A", "B"], "part_amount": ["30", ""],
                                                     "part_category": ["", ""]})
    with conn:
        conn.execute("UPDATE transactions SET amount = -70, reviewed = 0 WHERE id = ?", (joes,))  # as a bank update leaves it
    review = unescape(client.get("/transactions?review=1").text)
    assert f'id="t{joes}"' in review and '<span class="mark-text">2 parts</span>' in review and "Parts add up to $61.10" in review
    assert "but the parts add up to $61.10" in unescape(client.get(f"/transactions/{joes}").text)
    listing = unescape(client.get("/transactions").text)
    assert f'id="t{joes}"' not in listing and "Split doesn't add up" in listing
    conn.close()


def test_unsplitting_keeps_parts_from_the_history(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    original = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, reviewed) VALUES (?, '2026-05-02', -42, 'Store', 'plaid', 1)",
                            (account,)).lastrowid
    old = [conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, split_from_id) VALUES (?, '2026-05-02', ?, 'Store', 'copilot', ?)",
                        (account, amount, original)).lastrowid for amount in (-30, -12)]
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (original,)).fetchone()
    assert splits.unsplit(conn, t) == 2
    assert conn.execute(f"SELECT count(*) FROM transactions WHERE id IN ({old[0]}, {old[1]}) AND split_from_id IS NULL").fetchone()[0] == 2
    assert conn.execute("SELECT reviewed FROM transactions WHERE id = ?", (original,)).fetchone()[0] == 0  # it may count twice now


def test_a_split_keeps_the_tags_and_deletes_with_a_hand_made_transaction(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    client.post("/transactions/new", data={"date": "2026-10-12", "name": "Market", "amount": "40", "direction": "out",
                                           "account": "cash", "type": "regular", "notes": "with Alex"})
    market = conn.execute("SELECT id FROM transactions WHERE name = 'Market'").fetchone()[0]
    with conn:
        trip = tags.get_or_create(conn, "Trip")
        tags.add(conn, trip, [market])
    client.post(f"/transactions/{market}/split", data={"part_id": ["", ""], "part_name": ["Fruit", "Bread"],
                                                       "part_amount": ["25", ""], "part_category": ["", ""]})
    parts = splits.parts_of(conn, market)
    assert [p["notes"] for p in parts] == ["with Alex", None]  # the note goes to the first part
    assert all(conn.execute("SELECT 1 FROM transaction_tags WHERE transaction_id = ? AND tag_id = ?", (p["id"], trip)).fetchone() for p in parts)
    assert "empty the part's name and amount" in unescape(client.post(f"/transactions/{parts[0]['id']}/delete", data={"back": "/"}).text)
    client.post(f"/transactions/{market}/delete", data={"back": "/transactions"})
    assert conn.execute("SELECT count(*) FROM transactions WHERE source = 'split'").fetchone()[0] == 0
    conn.close()


def test_the_export_has_the_parts_not_the_split_transaction(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["Food", "Flowers"], "part_amount": ["41.10", ""],
                                                     "part_category": ["", ""]})
    lines = client.get("/export.csv").text.strip().splitlines()
    assert len(lines) == 1 + 10 - 1 + 2
    assert not any("61.10" in line for line in lines) and any(",Flowers,-20.00," in line for line in lines)
    conn.close()


def test_recurring_charges_go_by_the_charge_not_its_parts(conn):
    today = date(2026, 10, 14)
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    for months_ago in (3, 2, 1):
        day = (today - timedelta(days=30 * months_ago)).isoformat()
        tid = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, ?, -50, 'Gym Club', 'plaid')",
                           (account, day)).lastrowid
        t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
        splits.save(conn, t, splits.read_parts(["", ""], ["Gym Club", "Towels"], ["45", ""], ["", ""], 5000, "Gym Club")[0])
    [candidate] = recurring.find_candidates(conn, today)
    assert (candidate["pattern"], candidate["amount"], candidate["count"]) == ("gym club", -50.0, 3)
    _, linked = recurring.track(conn, "Gym", "gym club", "monthly", -50.0, None)
    assert linked == 3  # the charges, not their parts


def history_split(conn):
    """Returns (bank original, [its two parts from the history]) on a new account: 42.00 split as 30.00 and 12.00."""
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    original = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, reviewed, user_edited) "
                            "VALUES (?, '2026-09-12', -42, 'Store', 'plaid', 1, 1)", (account,)).lastrowid
    parts = [conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, reviewed, split_from_id) "
                          "VALUES (?, '2026-09-12', ?, 'Store', 'copilot', 1, ?)", (account, amount, original)).lastrowid for amount in (-30, -12)]
    conn.commit()
    return original, parts


def test_a_history_part_taken_out_of_a_split_goes_to_review(client, settings):
    conn = sync.connect(settings.db_path)
    original, (thirty, twelve) = history_split(conn)
    response = client.post(f"/transactions/{original}/split", data={
        "part_id": [str(thirty), str(twelve), ""], "part_name": ["", "Store", "Other"], "part_amount": ["", "12.00", "30.00"],
        "part_category": ["", "", ""], "back": f"/transactions/{original}"})
    assert "1 part from your history is on its own again, on the review list" in unescape(response.text)
    row = conn.execute("SELECT split_from_id, reviewed FROM transactions WHERE id = ?", (thirty,)).fetchone()
    assert tuple(row) == (None, 0)  # it counts again, so the user gets to check it
    conn.close()


def test_a_part_cant_be_deleted_on_its_own(client, settings):
    conn = sync.connect(settings.db_path)
    _original, (thirty, _) = history_split(conn)
    with conn:
        conn.execute("UPDATE transactions SET source = 'manual' WHERE id = ?", (thirty,))  # a row added by hand
    assert "Delete this transaction" not in client.get(f"/transactions/{thirty}").text
    response = client.post(f"/transactions/{thirty}/delete", data={"back": "/transactions"})
    assert "empty the part's name and amount" in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transactions WHERE id = ?", (thirty,)).fetchone()[0] == 1
    conn.close()


def test_removing_parts_keeps_their_links(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["Mine", "Theirs"], "part_amount": ["41.10", ""],
                                                     "part_category": ["", ""]})
    mine, theirs = (p["id"] for p in splits.parts_of(conn, joes))
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-09-10", "name": "Zelle from Sam", "amount": "20", "direction": "in",
                                           "account": str(checking), "type": "regular"})
    sam = conn.execute("SELECT id FROM transactions WHERE name = 'Zelle from Sam'").fetchone()[0]
    client.post(f"/transactions/{sam}/pays-back", data={"purchase_id": str(theirs)})
    before = reports.month_totals(conn, "2026-09")

    response = unescape(client.post(f"/transactions/{joes}/unsplit", data={"back": f"/transactions/{joes}"}).text)
    assert "“Theirs” is linked to a payback. Unlink that first" in response
    response = unescape(client.post(f"/transactions/{joes}/split", data={
        "part_id": [str(mine), str(theirs), ""], "part_name": ["Mine", "", "Other"], "part_amount": ["41.10", "", "20.00"],
        "part_category": ["", "", ""], "back": f"/transactions/{joes}"}).text)
    assert "Theirs” is linked to a payback" in response
    assert len(splits.parts_of(conn, joes)) == 2 and reports.month_totals(conn, "2026-09") == before
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (sam,)).fetchone()[0] == theirs
    conn.close()


def test_a_split_that_doesnt_add_up_stays_flagged(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["A", "B"], "part_amount": ["30", ""],
                                                     "part_category": ["", ""]})
    with conn:
        conn.execute("UPDATE transactions SET amount = -70, reviewed = 1 WHERE id = ?", (joes,))  # marked reviewed anyway
    review = unescape(client.get("/transactions?review=1").text)
    assert f'id="t{joes}"' in review and 'aria-label="Mark Trader Joe\'s as reviewed"' not in review
    assert 'class="count">1<' in review  # still counted as waiting for review
    listing = unescape(client.get("/transactions?month=2026-09").text)
    assert listing.count("Split doesn't add up") == 1  # on the split's row above its parts
    part = splits.parts_of(conn, joes)[0]["id"]
    assert "The parts add up to $61.10, but the transaction is $70.00." in unescape(client.get(f"/transactions/{part}").text)
    conn.close()


def test_rows_split_in_copilot_combine_into_one_line_and_ungroup(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()[0]
    shopping = conn.execute("SELECT id FROM categories WHERE name = 'Shopping'").fetchone()[0]
    with conn:
        add = lambda name, amount, category=None: conn.execute(
            "INSERT INTO transactions (account_id, date, amount, name, category_id, reviewed, source) VALUES (?, '2026-09-18', ?, ?, ?, 1, 'copilot')",
            (card, amount, name, category)).lastrowid
        mug, pens, tape = add("Store: Mug", -4.0), add("Store: Pens", -6.0), add("Other: Tape", -3.0, shopping)
        bank = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, plaid_transaction_id) "
                            "VALUES (?, '2026-09-18', -3, 'Bank row', 'plaid', 'bank-1')", (card,)).lastrowid
    before = (reports.month_totals(conn, "2026-09"), reports.category_spending(conn, "2026-09"))
    page_text = unescape(client.get(f"/transactions/{mug}").text)
    assert "Combine with others into one" in page_text and 'name="name" value="Store"' in page_text
    assert f'value="{pens}" checked>' in page_text and f'value="{bank}"' not in page_text  # bank rows have their own record

    response = client.post(f"/transactions/{mug}/combine", data={"ids": [str(pens), str(tape)], "name": "Store order", "back": "/transactions"})
    assert "Combined 3 transactions into one line" in unescape(response.text) and "Combined from 3 transactions" in unescape(response.text)
    combined = conn.execute("SELECT * FROM transactions WHERE source = 'group'").fetchone()
    assert (combined["name"], combined["amount"], combined["date"]) == ("Store order", -13.0, "2026-09-18")
    assert sorted(p["id"] for p in splits.parts_of(conn, combined["id"])) == sorted([mug, pens, tape])
    assert (reports.month_totals(conn, "2026-09"), reports.category_spending(conn, "2026-09")) == before  # nothing's total changes
    listing = unescape(client.get("/transactions?month=2026-09").text)
    assert all(f'id="t{i}"' in listing for i in (mug, pens, tape)) and f'id="t{combined["id"]}"' not in listing
    assert f'id="split{combined["id"]}"' in listing and "split into 3" in listing  # the combined row heads its items
    assert "Bank row" in listing

    # A bank row can't be combined; Ungroup puts the rows back as they were.
    assert "Pick at least one more" in unescape(client.post(f"/transactions/{mug}/combine", data={"ids": [str(bank)]}).text)
    response = client.post(f"/transactions/{combined['id']}/unsplit", data={"back": "/transactions?month=2026-09"})
    assert "Ungrouped" in unescape(response.text)
    assert conn.execute("SELECT count(*) FROM transactions WHERE source = 'group'").fetchone()[0] == 0
    assert all(tuple(r) == (None, 1) for r in conn.execute(f"SELECT split_from_id, reviewed FROM transactions WHERE id IN ({mug}, {pens}, {tape})"))
    assert (reports.month_totals(conn, "2026-09"), reports.category_spending(conn, "2026-09")) == before
    conn.close()



def test_a_part_of_a_split_doesnt_pair_with_an_amount_that_cancels_it_out():
    from app.main import offsetting_together
    row = lambda tid, amount, split=None: {"id": tid, "amount": amount, "account_id": 1, "type": "regular", "transfer_id": None,
                                           "split_from_id": split}
    rows = [row(1, 70.0, 9), row(2, 30.0, 9), row(3, -80.0), row(4, -70.0)]  # as listed: the split's total of 100 first
    assert [t["id"] for t in offsetting_together(rows)] == [1, 2, 3, 4]  # the 70 out stays below the 80


def test_a_part_that_pays_back_a_purchase_sits_under_it_without_its_split():
    from app.main import with_split_heads
    part = lambda tid, amount, split, pays_back=None: {
        "id": tid, "amount": amount, "split_from_id": split, "reimburses_id": pays_back, "split_name": f"Split {split}",
        "split_total": 0.0, "split_count": 2, "split_siblings_sum": 0.0, "split_pending": 0, "account_name": "Checking",
        "account_mask": None, "account_id": 1, "type": "regular"}
    # A purchase that's a part, the money paying it back (a part of another split), then the purchase's other part.
    rows = [part(1, -19.0, 8), part(2, 12.0, 9, pays_back=1), part(3, -10.0, 8)]
    assert [(t.get("kind"), t["id"], t.get("under_head")) for t in with_split_heads(rows)] == [
        ("split", 8, None), (None, 1, True), (None, 2, None), (None, 3, True)]
    # Without the purchase there, it's a part like any other on its own day; on the purchase's day, still a payback.
    assert [(t.get("kind"), t["id"]) for t in with_split_heads([part(2, 12.0, 9, pays_back=1)])] == [("split", 9), (None, 2)]
    moved = dict(part(2, 12.0, 9, pays_back=1), date="2026-08-02", shown_date="2026-06-05")
    assert [(t.get("kind"), t["id"]) for t in with_split_heads([moved])] == [(None, 2)]


def test_the_money_going_out_heads_a_move_between_accounts():
    from datetime import date

    from app.main import group_by_day
    row = lambda tid, amount, account, transfer: {"id": tid, "date": "2026-03-02", "shown_date": "2026-03-02", "amount": amount, "account_id": account,
                                                  "type": "transfer", "excluded": 0, "category_excluded": 0, "account_hidden": 0,
                                                  "reimburses_id": None, "transfer_id": transfer}
    [day] = group_by_day([row(9, 300.0, 2, 4), row(4, -300.0, 1, 9)], date(2026, 10, 14))  # newest first: the money in came first
    assert [(t["id"], t["joined"]) for t in day["rows"]] == [(4, False), (9, True)]


def test_money_in_from_a_move_sits_under_the_money_out_even_days_apart(client, settings):
    import_sample(client, settings)  # the card payment: out of checking and into the card, both on Sep 20, linked
    conn = sync.connect(settings.db_path)
    out, came_in = (conn.execute("SELECT id FROM transactions WHERE name = ?", (n,)).fetchone()[0] for n in ("Chase Payment", "Payment Thank You"))
    with conn:
        conn.execute("UPDATE transactions SET date = '2026-09-23' WHERE id = ?", (came_in,))  # the card got it three days later
        conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (came_in, out))
        conn.execute("UPDATE transactions SET transfer_id = ? WHERE id = ?", (out, came_in))
    listing = unescape(client.get("/transactions?transfers=1&month=2026-09").text)
    day = listing[listing.index("Sun, Sep 20"):]
    assert day.index(f'id="t{out}"') < day.index(f'id="t{came_in}"') < day.index("</section>")  # both on the day it went out
    assert "received Sep 23" in day[day.index(f'id="t{came_in}"'):] and "Wed, Sep 23" not in listing
    assert 'value="2026-09-23"' in client.get(f"/transactions/{came_in}").text  # its own date, unchanged
    conn.close()


def test_a_part_is_dated_like_its_split(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["A", "B"], "part_amount": ["30", ""],
                                                     "part_category": ["", ""], "back": "/transactions"})
    first = splits.parts_of(conn, joes)[0]["id"]
    page_text = unescape(client.get(f"/transactions/{first}").text)
    assert 'type="date"' not in page_text and "transaction it's split from</a>. Change it there." in page_text
    # A date sent for a part (from an older page, say) doesn't move it.
    client.post(f"/transactions/{first}", data={"name": "A", "type": "regular", "date": "2026-09-20"})
    assert [p["date"] for p in splits.parts_of(conn, joes)] == ["2026-09-08", "2026-09-08"]
    response = client.post(f"/transactions/{first}/whole", data={"name": "A", "date": "2026-09-20", "back": "/transactions"})
    assert "A part is dated like the transaction it's split from." in unescape(response.text)
    # The split's page renames it or changes the date of all of it; the bank's updates keep both, and the parts keep their names.
    assert f'action="/transactions/{joes}/whole"' in client.get(f"/transactions/{joes}").text
    response = client.post(f"/transactions/{joes}/whole", data={"name": "Market run", "date": "2026-09-10", "back": f"/transactions/{joes}"})
    assert "Saved. It's dated Sep 10, and so are its parts." in unescape(response.text)
    row = conn.execute("SELECT name, date, date_edited, user_edited FROM transactions WHERE id = ?", (joes,)).fetchone()
    assert tuple(row) == ("Market run", "2026-09-10", 1, 1)
    assert [(p["name"], p["date"]) for p in splits.parts_of(conn, joes)] == [("A", "2026-09-10"), ("B", "2026-09-10")]
    assert f'id="split{joes}"' in (listing := unescape(client.get("/transactions?month=2026-09").text)) and "Market run" in listing
    conn.close()


def test_parts_move_with_their_split_when_the_bank_moves_it(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("dinner", 50.0, "2026-10-08", "DINNER")], "c1"),
        "c1": page([], "c2", modified=[plaid_txn("dinner", 50.0, "2026-10-06", "DINNER")]),
    })
    sync.link_item(conn, fake, "public-sandbox-1", {"institution_id": "ins_1", "name": "Tartan"})
    sync.sync_item(conn, fake, "item-1")
    dinner = conn.execute("SELECT * FROM transactions WHERE plaid_transaction_id = 'dinner'").fetchone()
    with conn:
        parts, _ = splits.read_parts(["", ""], ["Mine", "Theirs"], ["", "10.00"], ["", ""], 5000, "DINNER")
        splits.save(conn, dinner, parts)
        # A row from the history made a part (dated a day before the bank's) takes the split's date.
        old = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-10-07', -1, 'Old', 'copilot')",
                           (dinner["account_id"],)).lastrowid
        conn.execute("UPDATE transactions SET split_from_id = ? WHERE id = ?", (dinner["id"], old))
    assert {p["date"] for p in splits.parts_of(conn, dinner["id"])} == {"2026-10-08"}
    sync.sync_item(conn, fake, "item-1")  # the bank dates it two days earlier
    assert {p["date"] for p in splits.parts_of(conn, dinner["id"])} == {"2026-10-06"}


def test_a_transaction_gets_its_own_date_back_when_it_leaves(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    add = lambda day, amount: conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, ?, ?, 'Shop', 'copilot')",
                                           (account, day, amount)).lastrowid
    row = lambda tid: conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    dates = lambda *ids: [row(i)["date"] for i in ids]
    # Combined, they're dated like the first; ungrouped, each has its own date again.
    towel, soap = add("2026-09-18", -5.0), add("2026-09-20", -8.0)
    group = splits.combine(conn, row(towel), [row(soap)], "Shop")
    assert dates(towel, soap) == ["2026-09-18", "2026-09-18"]
    splits.unsplit(conn, row(group))
    assert dates(towel, soap) == ["2026-09-18", "2026-09-20"] and row(soap)["own_date"] is None
    # Made parts of a bank's charge (as an import from Copilot can leave them), they take its date; taken out of the split, or
    # unsplit, they get their own back.
    bank = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-09-22', -13, 'Shop', 'plaid')",
                        (account,)).lastrowid
    conn.execute("UPDATE transactions SET split_from_id = ? WHERE id IN (?, ?)", (bank, towel, soap))
    assert dates(towel, soap) == ["2026-09-22", "2026-09-22"] and row(soap)["own_date"] == "2026-09-20"
    splits.release(conn, row(towel))
    assert dates(towel, soap) == ["2026-09-18", "2026-09-22"]
    splits.unsplit(conn, row(bank))
    assert dates(towel, soap) == ["2026-09-18", "2026-09-20"]


def test_a_parts_page_says_which_date_it_gets_back(client, settings):
    conn = sync.connect(settings.db_path)
    _original, (thirty, twelve) = history_split(conn)  # its parts are dated like it, Sep 12
    with conn:
        conn.execute("UPDATE transactions SET own_date = '2026-09-10' WHERE id = ?", (thirty,))
    assert "Taken out, it gets its own date back: Thu, Sep 10" in unescape(client.get(f"/transactions/{thirty}").text)
    assert "own date back" not in client.get(f"/transactions/{twelve}").text
    conn.close()


def test_only_the_parts_of_a_split_are_excluded(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    joes, _, _ = joes_and_categories(conn)
    with conn:
        conn.execute("UPDATE transactions SET excluded = 1 WHERE id = ?", (joes,))
    before = reports.month_totals(conn, "2026-09")
    client.post(f"/transactions/{joes}/split", data={"part_id": ["", ""], "part_name": ["A", "B"], "part_amount": ["30", ""],
                                                     "part_category": ["", ""], "back": "/transactions"})
    first, second = splits.parts_of(conn, joes)
    # The parts take its exclusion and the split keeps none of its own; it shows its parts'.
    assert (first["excluded"], second["excluded"]) == (1, 1)
    assert conn.execute("SELECT excluded FROM transactions WHERE id = ?", (joes,)).fetchone()[0] == 0
    assert reports.month_totals(conn, "2026-09") == before
    def head():
        text = unescape(client.get("/transactions?month=2026-09").text)
        start = text.index(f'id="split{joes}"')
        return text[text.rindex("<li", 0, start):text.index("</li>", start)]
    assert "split-head excluded" in head() and ">Excluded</span>" in head()
    assert "Every part is excluded, so none of it counts." in unescape(client.get(f"/transactions/{joes}").text)
    # A part counted again counts, whatever the split was, and the split says how many parts are still excluded.
    client.post(f"/transactions/{first['id']}", data={"name": "A", "type": "regular", "date": "2026-09-08"})
    assert reports.month_totals(conn, "2026-09")["spent"] == round(before["spent"] + 30, 2)
    assert ">1 excluded</span>" in head()
    assert "1 part excluded ($31.10); the others count." in unescape(client.get(f"/transactions/{joes}").text)
    conn.close()


def test_unsplitting_counts_it_whole_unless_every_part_was_excluded(conn):
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    row = lambda tid: conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    for excluded_parts, expected in (((1, 1), 1), ((1, 0), 0)):
        tid = conn.execute("INSERT INTO transactions (account_id, date, amount, name, source) VALUES (?, '2026-09-10', -40, 'Store', 'plaid')",
                           (account,)).lastrowid
        parts, _ = splits.read_parts(["", ""], ["A", "B"], ["30", ""], ["", ""], 4000, "Store")
        splits.save(conn, row(tid), parts)
        conn.executemany("UPDATE transactions SET excluded = ? WHERE id = ?", [(x, p["id"]) for p, x in zip(splits.parts_of(conn, tid), excluded_parts)])
        splits.unsplit(conn, row(tid))
        assert row(tid)["excluded"] == expected
