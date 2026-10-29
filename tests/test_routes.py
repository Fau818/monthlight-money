import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape

from fastapi.testclient import TestClient

from app import reports, sync
from app.main import bank_details, create_app, find_transactions, month_chunk
from app.plaid_client import PlaidError
from tests.conftest import COPILOT_CSV, FakePlaid, page, plaid_account, plaid_txn

PAGES = ["/", "/transactions", "/budgets", "/recurring", "/accounts", "/categories", "/import", "/settings", "/names"]


def test_every_page_loads_when_empty(client):
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert '<nav aria-label="Main">' in response.text


def import_sample(client, settings):
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    (settings.import_dir / "copilot.csv").write_text(COPILOT_CSV)
    preview = client.get("/import/preview", params={"file": "copilot.csv"})
    assert preview.status_code == 200 and "Import 10 transactions" in preview.text
    return client.post("/import/copilot", data={"file": "copilot.csv"})


def test_import_then_every_page_loads(client, settings):
    response = import_sample(client, settings)
    assert "Imported 10 transactions" in response.text
    for path in PAGES + ["/?month=2026-09", "/budgets?month=2026-09", "/transactions?month=2026-09&q=netflix&review=1&type=regular"]:
        assert client.get(path).status_code == 200, path
    assert "Netflix" in client.get("/transactions?q=netflix").text
    assert "Food" in client.get("/?month=2026-09").text


def test_upload_goes_to_preview(client, settings):
    response = client.post("/import/upload", files={"upload": ("export.csv", COPILOT_CSV, "text/csv")})
    assert response.status_code == 200 and "Preview: export.csv" in response.text
    assert (settings.import_dir / "export.csv").exists()


def test_import_rejects_files_outside_the_folder(client):
    assert client.get("/import/preview", params={"file": "../data/money.db"}).status_code == 404


def test_recategorizing_offers_a_rule(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    tid = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    shopping = conn.execute("SELECT id FROM categories WHERE name = 'Shopping'").fetchone()[0]

    response = client.post(f"/transactions/{tid}/category", data={"category_id": shopping, "back": "/transactions"})
    assert "is now Shopping" in response.text and f"/rules/from-transaction/{tid}" in response.text
    assert client.get("/transactions").text.count("is now Shopping") == 0  # the message shows once

    response = client.post(f"/rules/from-transaction/{tid}", data={"back": "/transactions"})
    assert "Rule saved" in response.text
    assert tuple(conn.execute("SELECT pattern, category_id FROM rules WHERE pattern = 'shell'").fetchone()) == ("shell", shopping)
    conn.close()


def test_renaming_a_transaction_offers_to_rename_the_others(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM transactions WHERE name = 'Netflix' ORDER BY id")]
    category = conn.execute("SELECT category_id FROM transactions WHERE id = ?", (ids[0],)).fetchone()[0]
    form = {"name": "Netflix family plan", "category_id": category, "type": "regular", "quiet": "1", "back": "/transactions"}
    text = unescape(client.post(f"/transactions/{ids[0]}", data=form).text)
    assert "Renamed it “Netflix family plan”." in text and "Rename the 2 others named “Netflix” too, and new ones" in text
    assert tuple(conn.execute("SELECT user_edited, name_edited FROM transactions WHERE id = ?", (ids[0],)).fetchone()) == (1, 1)

    text = unescape(client.post(f"/rules/rename-from-transaction/{ids[0]}", data={"old": "Netflix", "back": "/transactions"}).text)
    assert "Renamed 2 others. New ones named “Netflix” will come named “Netflix family plan”." in text
    assert {r[0] for r in conn.execute("SELECT name FROM transactions WHERE id IN (?, ?, ?)", ids)} == {"Netflix family plan"}
    # The rule the import made for the name keeps its category, and renames now too.
    rule = conn.execute("SELECT category_id, rename_to FROM rules WHERE pattern = 'netflix'").fetchone()
    assert tuple(rule) == (category, "Netflix family plan")
    assert "Netflix family plan" in client.get("/categories").text
    conn.close()


def test_renaming_a_split_offers_the_same(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    tid, day = conn.execute("SELECT id, date FROM transactions WHERE name = 'Amazon'").fetchone()
    client.post(f"/transactions/{tid}/split", data={"part_name": ["Shoes", "Socks"], "part_amount": ["15", ""], "part_category": ["", ""]})
    text = unescape(client.post(f"/transactions/{tid}/whole", data={"name": "Amazon order", "date": day, "quiet": "1"}).text)
    assert "Renamed it “Amazon order”." in text and "Rename new ones named “Amazon” too" in text
    assert tuple(conn.execute("SELECT name, name_edited FROM transactions WHERE id = ?", (tid,)).fetchone()) == ("Amazon order", 1)
    conn.execute("UPDATE transactions SET name = 'Amazon Conf# 1A2B3C4D' WHERE id = ?", (tid,))
    conn.commit()
    text = unescape(client.post(f"/transactions/{tid}/whole", data={"name": "Amazon Conf# 1A2B3C4D", "date": "2026-09-20"}).text)
    assert "Saved. It's dated" in text and "Rename" not in text  # only its date changed: nothing to offer
    conn.close()


def test_rules_can_give_a_name(client, settings):
    conn = sync.connect(settings.db_path)
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    for renamed in (0, 1):
        conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, name_edited) VALUES (?, '2026-10-01', -5, ?, 'plaid', ?)",
                     (account, "SQ *CORNER BAKERY", renamed))
    conn.commit()
    assert "Pick what the rule does" in unescape(client.post("/rules", data={"pattern": "corner bakery", "match": "contains"}).text)
    text = unescape(client.post("/rules", data={"pattern": "corner bakery", "match": "contains", "rename_to": " Bakery ", "apply": "1"}).text)
    assert "Rule saved. Updated 1 transactions." in text and "<td>Bakery</td>" in text
    assert [r[0] for r in conn.execute("SELECT name FROM transactions ORDER BY id")] == ["Bakery", "SQ *CORNER BAKERY"]
    conn.close()


def test_the_names_page_learns_from_earlier_names_and_cleans_up(client, settings):
    conn = sync.connect(settings.db_path)
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid
    bank = "Online transfer to savings Conf# 1A2B3C4D"
    for name, source, day in ((bank, "plaid", "2026-10-01"), ("To savings", "copilot", "2026-08-01"), ("To savings", "copilot", "2026-09-01")):
        conn.execute("INSERT INTO transactions (account_id, date, amount, name, source, plaid_details) VALUES (?, ?, -50, ?, ?, ?)",
                     (account, day, name, source, json.dumps({"bank_name": bank})))
    conn.commit()
    assert "1 earlier name can be cleaned up. 1 rule could name new ones like your earlier ones." in unescape(client.get("/settings").text)
    text = unescape(client.get("/names").text)
    assert all(part in text for part in ("Online transfer to savings", "To savings", "2 earlier ones", "Make the rules", "Rename it"))

    text = unescape(client.post("/names/rules", data={"patterns": ["online transfer to savings", "something else"]}).text)
    assert "Made 1 rule: new transactions come named like your earlier ones." in text
    assert "Nothing to suggest" in text  # the rule names them now
    text = unescape(client.post("/names/clean").text)
    assert "Renamed 1 transaction." in text and "Nothing to clean up" in text
    assert conn.execute("SELECT name FROM transactions WHERE source = 'plaid'").fetchone()[0] == "To savings"  # the rule's name
    assert "Review names" not in client.get("/settings").text
    conn.close()


def test_edit_transaction_and_budgets(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    tid = conn.execute("SELECT id FROM transactions WHERE name = 'Amazon'").fetchone()[0]
    assert client.get(f"/transactions/{tid}").status_code == 200
    client.post(f"/transactions/{tid}", data={"name": "Amazon return", "type": "regular", "notes": "shoes", "reviewed": "1"})
    row = conn.execute("SELECT name, notes, user_edited FROM transactions WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("Amazon return", "shoes", 1)

    food = conn.execute("SELECT id FROM categories WHERE name = 'Food'").fetchone()[0]
    response = client.post("/budgets", data={f"budget_{food}": "$1,200", "back": "/budgets?month=2026-09"})
    assert "Budgets saved" in response.text
    assert conn.execute("SELECT budget FROM categories WHERE id = ?", (food,)).fetchone()[0] == 1200.0
    assert "“lots” isn't a number" in unescape(client.post("/budgets", data={f"budget_{food}": "lots"}).text)
    conn.close()


def test_connect_flow_with_matching(client, settings, monkeypatch):
    import_sample(client, settings)
    fake = FakePlaid(pages={
        None: page([plaid_txn("n1", 12, "2026-10-12", "CHIPOTLE", pfc="FOOD_AND_DRINK_FAST_FOOD")], "c1"),
        "c1": page([], "c1"),
    })
    monkeypatch.setattr(sync, "make_client", lambda s: fake)
    monkeypatch.setattr("app.main.sync_in_background", lambda s, ids=None: sync.run_sync(s, ids))

    assert client.post("/plaid/link-token", json={}).json() == {"link_token": "link-sandbox-token"}
    result = client.post("/plaid/exchange", json={"public_token": "public-1", "institution": {"name": "Chase", "institution_id": "ins_3"}}).json()
    assert result == {"redirect": "/plaid/match/item-1"}
    assert "still need matching" in client.get("/").text
    match_page = client.get("/plaid/match/item-1")
    assert "Sapphire Preferred" in match_page.text and "Chase Sapphire" in match_page.text

    conn = sync.connect(settings.db_path)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()[0]
    response = client.post("/plaid/match/item-1", data={"account_acc-card": str(card)})
    assert "Accounts matched" in response.text
    assert conn.execute("SELECT count(*) FROM transactions WHERE plaid_transaction_id = 'n1' AND account_id = ?", (card,)).fetchone()[0] == 1
    assert "Chase" in client.get("/accounts").text
    assert "Chase: 0 new, 0 updated, 0 removed" in client.post("/sync", data={"back": "/"}).text
    conn.close()


def test_cross_site_posts_are_blocked(client):
    response = client.post("/categories", data={"name": "Evil"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/categories", data={"name": "Fine"}, headers={"Origin": "http://testserver"}).status_code == 200


def test_unknown_host_names_are_refused(client):
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_redirects_stay_on_site(client):
    response = client.post("/sync", data={"back": "https://evil.example/"}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_export_csv(client, settings):
    import_sample(client, settings)
    response = client.get("/export.csv")
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("date,name,amount") and len(lines) == 11


def fake_plaid(monkeypatch, fake):
    monkeypatch.setattr(sync, "make_client", lambda s: fake)
    monkeypatch.setattr("app.main.sync_in_background", lambda s, ids=None: None)
    return fake


BANK_LOGIN = {"public_token": "public-1", "institution": {"name": "Tartan", "institution_id": "ins_2"},
             "accounts": [{"id": "x", "mask": "1234", "subtype": "credit card"}], "link_session_id": "sess-1"}


def test_second_login_to_a_connected_bank_is_caught_before_exchange(client, monkeypatch):
    fake = fake_plaid(monkeypatch, FakePlaid())
    assert client.post("/plaid/exchange", json=BANK_LOGIN).json() == {"redirect": "/accounts"}
    again = client.post("/plaid/exchange", json={**BANK_LOGIN, "public_token": "public-2"})
    assert again.status_code == 409 and again.json()["duplicate"] is True
    assert "Tartan is already connected" in again.json()["error"]
    assert fake.exchanges == 1  # never exchanged, so Plaid doesn't count a second connection

    fake.accounts = [plaid_account("acc-card-2", "Sapphire Preferred", "1234")]  # a new Item gets new account ids
    forced = client.post("/plaid/exchange", json={**BANK_LOGIN, "public_token": "public-2", "allow_duplicate": True})
    assert forced.json() == {"redirect": "/accounts"} and fake.exchanges == 2


def test_connection_whose_accounts_did_not_load_can_be_retried(client, monkeypatch):
    fake = fake_plaid(monkeypatch, FakePlaid())
    fake.errors["accounts"] = PlaidError("INSTITUTION_DOWN", "down")
    assert client.post("/plaid/exchange", json=BANK_LOGIN).json() == {"redirect": "/accounts"}
    page_text = unescape(client.get("/accounts").text)
    assert "Connected Tartan, but Plaid couldn't list its accounts yet" in page_text
    assert "Syncing Tartan failed: The bank isn't responding right now." in page_text
    assert "Tartan: 0 new, 0 updated, 0 removed" in client.post("/sync", data={"back": "/accounts"}).text


def test_bank_access_about_to_expire_asks_for_a_new_login(client, settings, monkeypatch):
    fake_plaid(monkeypatch, FakePlaid())
    client.post("/plaid/exchange", json=BANK_LOGIN)
    conn = sync.connect(settings.db_path)
    with conn:
        conn.execute("UPDATE plaid_items SET consent_expires_at = ?", ((datetime.now(UTC) + timedelta(days=5)).isoformat(),))
    page_text = client.get("/accounts").text
    assert "Tartan stops sharing data with this app on" in page_text and ">Log in again</button>" in page_text
    with conn:
        conn.execute("UPDATE plaid_items SET consent_expires_at = ?", ((datetime.now(UTC) + timedelta(days=200)).isoformat(),))
    page_text = client.get("/accounts").text
    assert "stops sharing" not in page_text and ">Add or remove accounts</button>" in page_text
    conn.close()


def test_link_token_for_an_existing_connection_uses_update_mode(client, monkeypatch):
    fake = fake_plaid(monkeypatch, FakePlaid())
    tokens = []
    monkeypatch.setattr(fake, "create_link_token", lambda access_token=None: tokens.append(access_token) or "link-update")
    client.post("/plaid/exchange", json=BANK_LOGIN)
    assert client.post("/plaid/link-token", json={"item_id": "item-1"}).json() == {"link_token": "link-update"}
    assert tokens == ["access-sandbox-1"]
    assert client.post("/plaid/link-token", json={"item_id": "gone"}).status_code == 404
    response = client.post("/plaid/items/item-1/repaired", json={"link_session_id": "sess-2"})
    assert response.json() == {"redirect": "/accounts"} and "Connection updated" in client.get("/accounts").text


def test_link_exits_are_logged_with_the_session_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="monthlight"):
        response = client.post("/plaid/link-exit", json={
            "error": {"error_type": "ITEM_ERROR", "error_code": "INVALID_CREDENTIALS", "error_message": "x", "display_message": None},
            "institution": {"name": "Tartan", "institution_id": "ins_2"}, "status": "requires_credentials",
            "link_session_id": "sess-42", "request_id": "req-7",
        })
        assert response.json() == {"ok": True}
        assert client.post("/plaid/link-exit", json={"error": None, "status": "institution_search", "link_session_id": "sess-43"}).status_code == 200
    assert "INVALID_CREDENTIALS at Tartan (link_session_id sess-42, request_id req-7)" in caplog.text
    assert "Link closed without connecting at no bank chosen, step institution_search (link_session_id sess-43)" in caplog.text


def test_scripts_and_styles_are_versioned(client):
    from app.main import STATIC_VERSION
    page_text = client.get("/").text
    assert f"/static/app.js?v={STATIC_VERSION}" in page_text and f"/static/style.css?v={STATIC_VERSION}" in page_text
    assert client.get(f"/static/app.js?v={STATIC_VERSION}").status_code == 200


def test_paired_transfers_are_hidden_unless_asked_for(client, settings):
    import_sample(client, settings)  # has a card payment on both sides: two transfers, not linked yet
    listing = client.get("/transactions").text
    assert "Chase Payment" in listing and "Payment Thank You" in listing  # without their other side found, they show
    conn = sync.connect(settings.db_path)
    with conn:
        assert sync.link_transfers(conn) == 1
    conn.close()
    listing = client.get("/transactions").text
    assert "Chase Payment" not in listing and "Payment Thank You" not in listing
    assert "2 paired transfers between your accounts hidden" in listing and "transfers=1" in listing
    shown = client.get("/transactions?transfers=1").text
    assert "Chase Payment" in shown and "Hide transfers" in shown
    assert "Chase Payment" in client.get("/transactions?type=transfer").text
    assert "Chase Payment" not in client.get("/").text  # nor on the dashboard's recent list


def test_income_and_transfers_take_no_category(client, settings):
    import_sample(client, settings)
    shown = client.get("/transactions?transfers=1").text
    # 10 rows: 1 income and 2 transfers show their type; only the 7 regular ones get a category menu.
    assert shown.count('name="category_id"') == 7 and ">Income</span>" in shown and ">Transfer</span>" in shown
    # Uncategorized means regular transactions without one; the transfers have none but don't count.
    assert "No transactions match." in client.get("/transactions?category=none&transfers=1").text
    conn = sync.connect(settings.db_path)
    payroll = conn.execute("SELECT id FROM transactions WHERE type = 'income'").fetchone()[0]
    groceries = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    conn.close()
    assert 'data-category-field hidden' in client.get(f"/transactions/{payroll}").text
    assert 'data-category-field>' in client.get(f"/transactions/{groceries}").text


def test_saving_a_transfer_clears_its_category(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    tid, shopping = conn.execute("SELECT t.id, t.category_id FROM transactions t WHERE name = 'Amazon'").fetchone()
    client.post(f"/transactions/{tid}", data={"name": "Amazon", "type": "transfer", "category_id": str(shopping), "reviewed": "1"})
    assert tuple(conn.execute("SELECT type, category_id, user_edited FROM transactions WHERE id = ?", (tid,)).fetchone()) == ("transfer", None, 1)
    conn.close()


def test_add_and_delete_a_transaction_by_hand(client, settings):
    import_sample(client, settings)
    form = client.get("/transactions/new")
    assert form.status_code == 200 and "Cash (a new account)" in form.text  # not mistaken for transaction id "new"
    response = client.post("/transactions/new", data={"date": "2026-10-12", "name": "Farmers market", "amount": "$1,012.50",
                                                      "direction": "out", "account": "cash", "type": "regular"})
    assert "Added “Farmers market”" in unescape(response.text) and "Farmers market" in response.text
    conn = sync.connect(settings.db_path)
    t = conn.execute("""SELECT t.*, a.name AS account, a.source AS account_source FROM transactions t
                        JOIN accounts a ON a.id = t.account_id WHERE t.source = 'manual'""").fetchone()
    assert (t["amount"], t["account"], t["account_source"], t["user_edited"], t["date"]) == (-1012.5, "Cash", "manual", 1, "2026-10-12")
    assert reports.month_totals(conn, "2026-10")["spent"] == 40.0 + 1012.5  # Shell, then the market
    # A second cash transaction goes to the same Cash account; money in to another account works too.
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-10-13", "name": "Cash back", "amount": "20", "direction": "in",
                                           "account": "cash", "type": "regular"})
    client.post("/transactions/new", data={"date": "2026-10-13", "name": "Side gig", "amount": "300", "direction": "in",
                                           "account": str(checking), "type": "income", "category_id": "1"})
    assert conn.execute("SELECT count(*) FROM accounts WHERE name = 'Cash'").fetchone()[0] == 1
    gig = conn.execute("SELECT amount, type, category_id FROM transactions WHERE name = 'Side gig'").fetchone()
    assert tuple(gig) == (300.0, "income", None)  # income takes no category

    # Only hand-added transactions can be deleted.
    market = t["id"]
    assert "Delete this transaction" in client.get(f"/transactions/{market}").text
    response = client.post(f"/transactions/{market}/delete", data={"back": "/transactions"})
    assert "Deleted “Farmers market”" in unescape(response.text)
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    assert "Delete this transaction" not in client.get(f"/transactions/{shell}").text
    assert "Only transactions you added yourself" in client.post(f"/transactions/{shell}/delete", data={"back": "/transactions"}).text
    assert conn.execute("SELECT count(*) FROM transactions WHERE id IN (?, ?)", (market, shell)).fetchone()[0] == 1
    conn.close()


def test_adding_a_transaction_keeps_what_was_typed_when_something_is_wrong(client):
    response = client.post("/transactions/new", data={"date": "2026-10-12", "name": "Lunch", "amount": "twelve", "account": "cash"})
    assert response.status_code == 400 and "Check the date and the amount" in response.text and 'value="Lunch"' in response.text
    response = client.post("/transactions/new", data={"date": "2026-10-12", "name": "Lunch", "amount": "0", "account": "cash"})
    assert response.status_code == 400 and "an amount above zero" in response.text
    response = client.post("/transactions/new", data={"date": "2026-10-12", "name": "Lunch", "amount": "5", "account": "999"})
    assert response.status_code == 400 and "Pick an account" in response.text


def test_other_devices_can_use_the_app_but_not_plaid_link(settings):
    settings = replace(settings, remote_hosts=["*.sgponte"])
    with TestClient(create_app(settings), base_url="http://mymac.sgponte:8080") as phone:
        assert phone.get("/transactions").status_code == 200
        response = phone.post("/plaid/link-token", json={}, headers={"Origin": "http://mymac.sgponte:8080"})
        assert response.status_code == 400 and "on the computer running the app" in response.json()["error"]
    with TestClient(create_app(settings), base_url="http://elsewhere.example") as stranger:
        assert stranger.get("/transactions").status_code == 400


def test_day_labels():
    from datetime import date

    from app.main import day_label
    today = date(2026, 10, 14)
    assert [day_label(d, today) for d in ("2026-10-14", "2026-10-13", "2026-09-21", "2025-12-31")] == [
        "Today", "Yesterday", "Mon, Sep 21", "Wed, Dec 31, 2025"]


def test_transactions_are_listed_by_day_with_emoji_categories(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    with conn:
        sync.link_transfers(conn)  # the card payment's two sides, so they're hidden as a pair
    conn.close()
    listing = unescape(client.get("/transactions?month=2026-09").text)
    assert "Mon, Sep 21" in listing and "Tue, Sep 8" in listing
    assert listing.index("Mon, Sep 21") < listing.index("Tue, Sep 8")  # newest first
    assert "🛒 Food › Groceries" in listing  # the category menu shows each category's emoji
    assert re.search(r'<span class="pill tint-\w+">Groceries</span>', listing)  # and its pill, the category's own name
    assert '<span class="cur">$</span>54.20' in listing  # amounts with a small dollar sign
    assert "$130.79 out · +$2,520.00 in" in listing  # totals of what's listed, paired transfers left out


def test_months_are_listed_whole_a_few_at_a_time(monkeypatch):
    monkeypatch.setattr("app.main.PAGE_SIZE", 10)
    months = [("2026-10", 4), ("2026-09", 7), ("2026-08", 30), ("2026-07", 2)]
    assert month_chunk(months, "2026-10") == ["2026-10", "2026-09"]  # enough to list 10 or more
    assert month_chunk(months, "2026-08") == ["2026-08"]  # a month is never cut, however many it holds
    assert month_chunk(months, "2026-10", "2026-08") == ["2026-10", "2026-09", "2026-08"]  # through the oldest asked for


def test_the_list_goes_by_month_and_loads_earlier_ones(client, settings, monkeypatch):
    import_sample(client, settings)
    monkeypatch.setattr("app.main.PAGE_SIZE", 3)
    listing = unescape(client.get("/transactions").text)
    assert 'id="m2026-10"' in listing and 'id="m2026-09"' in listing and 'id="m2026-08"' not in listing
    assert '<a class="button" href="/transactions?oldest=2026-07#m2026-08" data-older>August 2026 and earlier</a>' in listing
    earlier = unescape(client.get("/transactions?oldest=2026-07").text)
    assert all(f'id="m2026-{m}"' in earlier for m in ("10", "09", "08", "07")) and "data-older" not in earlier
    # Each month starts with its totals, as on the dashboard; a search lists its months without them.
    october = listing[listing.index('id="m2026-10"'):listing.index('id="m2026-09"')]
    assert "<dt>Spent</dt><dd>$40.00</dd>" in october and 'href="/?month=2026-10">Open October' in october
    found = unescape(client.get("/transactions?q=netflix").text)
    assert 'id="m2026-09"' in found and "month-totals" not in found


def test_dashboard_review_list_and_spending_chart(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    with conn:
        conn.execute("UPDATE transactions SET reviewed = 0 WHERE name = 'Shell'")
    page = unescape(client.get("/?month=2026-10").text)
    assert 'aria-label="Mark Shell as reviewed"' in page and 'class="count">1<' in page
    assert "data-plot" in page and "Spent in October" in page
    response = client.post("/transactions/review", data={"ids": [conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]],
                                                         "back": "/"})
    assert "Marked 1 transaction as reviewed." in response.text and "All caught up" in response.text
    conn.close()


def test_categories_take_an_emoji_and_a_color(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    gas = conn.execute("SELECT id FROM categories WHERE name = 'Gas'").fetchone()[0]
    client.post(f"/categories/{gas}", data={"name": "Gas", "emoji": " ⛽️ ", "color": "aqua"})
    assert tuple(conn.execute("SELECT emoji, color FROM categories WHERE id = ?", (gas,)).fetchone()) == ("⛽️", "aqua")
    assert "⛽️" in client.get("/categories").text
    client.post(f"/categories/{gas}", data={"name": "Gas", "emoji": "", "color": "plaid"})  # back to automatic
    assert tuple(conn.execute("SELECT emoji, color FROM categories WHERE id = ?", (gas,)).fetchone()) == (None, None)
    conn.close()


def test_budgets_are_edited_on_request(client, settings):
    import_sample(client, settings)
    assert 'name="budget_' not in client.get("/budgets?month=2026-09").text
    editing = client.get("/budgets?month=2026-09&edit=1").text
    assert 'name="budget_' in editing and 'name="back" value="/budgets?month=2026-09"' in editing


def test_a_transactions_date_can_be_changed(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    tid, gas = conn.execute("SELECT id, category_id FROM transactions WHERE name = 'Shell'").fetchone()
    assert 'name="date" value="2026-10-03"' in client.get(f"/transactions/{tid}").text
    assert reports.month_totals(conn, "2026-10")["spent"] == 40.0
    response = client.post(f"/transactions/{tid}", data={"name": "Shell", "type": "regular", "category_id": str(gas),
                                                         "date": "2026-09-28", "reviewed": "1"})
    assert "It's now dated Sep 28." in unescape(response.text)
    row = conn.execute("SELECT date, date_edited, user_edited FROM transactions WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("2026-09-28", 1, 0)  # only the date is pinned; rules may still recategorize it
    assert reports.month_totals(conn, "2026-10")["spent"] == 0.0  # it counts in September now
    assert "updates from your bank won't change it" in unescape(client.get(f"/transactions/{tid}").text)

    response = client.post(f"/transactions/{tid}", data={"name": "Shell", "type": "regular", "date": "not a date"})
    assert "Check the date" in response.text
    assert conn.execute("SELECT date FROM transactions WHERE id = ?", (tid,)).fetchone()[0] == "2026-09-28"
    conn.close()


def test_amounts_that_cancel_out_sit_together_within_a_day():
    from datetime import date

    from app.main import group_by_day
    row = lambda tid, amount, account, type_="transfer": {"id": tid, "date": "2026-03-02", "shown_date": "2026-03-02", "amount": amount,
                                                          "account_id": account, "type": type_, "excluded": 0, "category_excluded": 0,
                                                          "account_hidden": 0, "reimburses_id": None, "transfer_id": None}
    rows = [row(1, 1200.0, 1), row(2, 5000.0, 2, "income"), row(3, -20.0, 2, "regular"), row(4, -1200.0, 2), row(5, 20.0, 2, "regular")]
    [day] = group_by_day(rows, date(2026, 10, 14))
    assert [t["id"] for t in day["rows"]] == [1, 4, 2, 3, 5]
    assert day["spent"] == 0.0  # the order doesn't change the day's total


def test_a_day_lists_the_biggest_amounts_first(conn):
    from datetime import date

    from app.main import find_transactions, group_by_day
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Checking')").lastrowid

    def add(name, amount, type_="regular", split_from=None):
        return conn.execute("INSERT INTO transactions (account_id, date, amount, name, type, source, split_from_id) "
                            "VALUES (?, '2026-09-12', ?, ?, ?, 'manual', ?)", (account, amount, name, type_, split_from)).lastrowid

    coffee, bakery, apple, rent, pay = add("Coffee", -4.5), add("Bakery", -12), add("apple", -12), add("Rent", -1500), add("Paycheck", 2000, "income")
    store = add("Store", -100)
    gift, kept = add("Gift", -30, split_from=store), add("Store", -70, split_from=store)  # the smaller part made first
    tools, zebra = add("Hardware", -80), add("Zebra", -100)
    listed = lambda **criteria: [(t.get("kind"), t["id"], t["joined"])
                                 for t in group_by_day(find_transactions(conn, **criteria)[0], date(2026, 10, 14))[0]["rows"]]
    # Money in and out alike, and ties by name, whatever the capitals. A split goes by its total and name, its parts by amount.
    assert listed() == [(None, pay, False), (None, rent, False), ("split", store, False), (None, kept, True), (None, gift, True),
                        (None, zebra, False), (None, tools, False), (None, apple, False), (None, bakery, False), (None, coffee, False)]
    # On the review list, the split's own row heads its parts.
    assert listed(review=True) == [(None, pay, False), (None, rent, False), (None, store, False), (None, kept, True), (None, gift, True),
                                   (None, zebra, False), (None, tools, False), (None, apple, False), (None, bakery, False), (None, coffee, False)]


def test_income_is_never_excluded(client, settings):
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    (settings.import_dir / "copilot.csv").write_text(COPILOT_CSV.replace("Acme Payroll,-2500.00,posted,Paycheck,,false", "Acme Payroll,-2500.00,posted,Paycheck,,true"))
    client.post("/import/copilot", data={"file": "copilot.csv"})
    conn = sync.connect(settings.db_path)
    payroll = conn.execute("SELECT id, excluded FROM transactions WHERE type = 'income'").fetchone()
    assert payroll["excluded"] == 0  # imported as not excluded
    assert reports.month_totals(conn, "2026-09")["income"] == 2500.0
    assert "data-category-field hidden" in client.get(f"/transactions/{payroll['id']}").text  # where Excluded is a choice
    client.post(f"/transactions/{payroll['id']}", data={"name": "Acme Payroll", "type": "income", "excluded": "1", "date": "2026-09-15"})
    assert conn.execute("SELECT excluded FROM transactions WHERE id = ?", (payroll["id"],)).fetchone()[0] == 0
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    page_text = client.get(f"/transactions/{shell}").text
    assert "data-category-field>" in page_text and '<option value="excluded">' in page_text  # regular ones can still be excluded
    # Excluded is a choice beside the categories: picking it clears the category, and picking a category counts it again.
    shopping = conn.execute("SELECT id FROM categories WHERE name = 'Shopping'").fetchone()[0]
    state = lambda: tuple(conn.execute("SELECT excluded, category_id FROM transactions WHERE id = ?", (shell,)).fetchone())
    assert "is excluded from spending" in unescape(client.post(f"/transactions/{shell}/category", data={"category_id": "excluded"}).text)
    assert state() == (1, None)
    assert '<option value="excluded" selected>' in client.get("/transactions?month=2026-10").text
    assert shell not in [t["id"] for t in find_transactions(conn, category="none")[0]]  # it isn't waiting for a category
    client.post(f"/transactions/{shell}/category", data={"category_id": str(shopping)})
    assert state() == (0, shopping)
    client.post(f"/transactions/{shell}", data={"name": "Shell", "type": "regular", "category_id": "excluded", "date": "2026-10-03"})
    assert state() == (1, None)
    with conn:  # however it's excluded, it loses its category
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (shopping, shell))
    assert state() == (1, None)
    conn.close()


def test_a_friends_payback_links_to_the_purchase(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-10-02", "name": "Zelle from Sam", "amount": "25", "direction": "in",
                                           "account": str(checking), "type": "regular"})
    payback = conn.execute("SELECT id FROM transactions WHERE name = 'Zelle from Sam'").fetchone()[0]
    joes = conn.execute("SELECT id FROM transactions WHERE name = 'Trader Joe''s' AND date = '2026-09-08'").fetchone()[0]
    assert reports.month_totals(conn, "2026-10")["spent"] == 15.0  # Shell's 40, less the 25 that came in

    page_text = unescape(client.get(f"/transactions/{payback}").text)
    assert "Link the purchase it pays back" in page_text and f'value="{joes}">Sep 8 · Trader Joe\'s · $61.10' in page_text
    response = client.post(f"/transactions/{payback}/pays-back", data={"purchase_id": str(joes), "back": f"/transactions/{payback}"})
    assert "pays back “Trader Joe's”: it counts in Groceries on Sep 8." in unescape(response.text)
    row = conn.execute("SELECT reimburses_id, type, user_edited, reviewed FROM transactions WHERE id = ?", (payback,)).fetchone()
    assert tuple(row) == (joes, "regular", 1, 1)
    # It takes the purchase's category, follows it when that changes, and keeps it when given another one.
    category = lambda tid: conn.execute("SELECT category_id FROM transactions WHERE id = ?", (tid,)).fetchone()[0]
    groceries, shopping = category(joes), conn.execute("SELECT id FROM categories WHERE name = 'Shopping'").fetchone()[0]
    assert category(payback) == groceries
    with conn:
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (shopping, joes))
    assert category(payback) == shopping
    with conn:
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (groceries, payback))
    assert category(payback) == shopping
    with conn:
        conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (groceries, joes))
    assert reports.month_totals(conn, "2026-10")["spent"] == 40.0  # it moved to September, with the purchase
    groceries = next(ch for n in reports.category_spending(conn, "2026-09") for ch in n["children"] if ch["name"] == "Groceries")
    assert groceries["spent"] == round(54.20 + 61.10 - 25, 2)

    september = unescape(client.get("/transactions?month=2026-09").text)
    assert september.index("Trader Joe's", september.index("Tue, Sep 8")) < september.index("Zelle from Sam")  # right under it
    assert "pays back Trader Joe's" in september and "received Oct 2" in september and "your share $36.10" in september
    assert "Zelle from Sam" not in client.get("/transactions?month=2026-10").text
    purchase_page = unescape(client.get(f"/transactions/{joes}").text)
    assert "Paid back" in purchase_page and "$36.10" in purchase_page

    # Unlinking and linking again from the purchase's page.
    client.post(f"/transactions/{payback}/pays-back", data={"purchase_id": "", "back": "/"})
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (payback,)).fetchone()[0] is None
    client.post(f"/transactions/{joes}/paybacks", data={"payback_id": str(payback), "back": "/"})
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (payback,)).fetchone()[0] == joes
    # Only money in pays back, only a purchase gets paid back, and a payback that becomes a transfer stops being one.
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    assert "Only money coming in" in unescape(client.post(f"/transactions/{shell}/pays-back", data={"purchase_id": str(joes)}).text)
    client.post(f"/transactions/{payback}", data={"name": "Zelle from Sam", "type": "transfer", "date": "2026-10-02"})
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (payback,)).fetchone()[0] is None
    conn.close()


def test_a_payback_bigger_than_its_purchase_pays_it_back_in_full(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    client.post("/transactions/new", data={"date": "2026-10-04", "name": "Zelle from Robin", "amount": "45", "direction": "in",
                                           "account": str(checking), "type": "regular"})
    robin = conn.execute("SELECT id FROM transactions WHERE name = 'Zelle from Robin'").fetchone()[0]
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]  # 40.00 on Oct 3
    # Each side offers the other, in a group after the ones that fit.
    offered = unescape(client.get(f"/transactions/{robin}").text)
    assert offered.index('label="$45.00 or more"') < offered.index('label="Less than $45.00"') < offered.index(f'value="{shell}">Oct 3 · Shell · $40.00')
    offered = unescape(client.get(f"/transactions/{shell}").text)
    assert f'<optgroup label="More than $40.00"><option value="{robin}">Oct 4 · Zelle from Robin · +$45.00' in offered
    before = reports.month_totals(conn, "2026-10")
    client.post(f"/transactions/{shell}/paybacks", data={"payback_id": str(robin)})
    assert conn.execute("SELECT reimburses_id FROM transactions WHERE id = ?", (robin,)).fetchone()[0] == shell
    # It's paid back in full, and the 5.00 over counts like a refund, so no total changes.
    assert reports.month_totals(conn, "2026-10") == before
    listing = unescape(client.get("/transactions?month=2026-10").text)
    assert "paid back in full, $5.00 over" in listing and "your share -$5.00" not in listing
    purchase_page = unescape(client.get(f"/transactions/{shell}").text)
    assert "$0.00</strong> · paid back in full" in purchase_page and "More than it cost" in purchase_page
    conn.close()


def test_the_other_side_of_a_move_can_be_linked_by_hand(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    checking, card = (conn.execute("SELECT id FROM accounts WHERE name = ?", (n,)).fetchone()[0] for n in ("Acme Checking", "Chase Sapphire"))
    for name, amount, direction, account in (("To my card", "300", "out", checking), ("From my checking", "300", "in", card)):
        client.post("/transactions/new", data={"date": "2026-10-04", "name": name, "amount": amount, "direction": direction,
                                               "account": str(account), "type": "regular"})
    out, came_in = (conn.execute("SELECT id FROM transactions WHERE name = ?", (n,)).fetchone()[0] for n in ("To my card", "From my checking"))
    conn.execute("UPDATE transactions SET excluded = 1 WHERE id = ?", (out,))
    conn.commit()
    page_text = unescape(client.get(f"/transactions/{out}").text)
    assert "Link the other side of a move" in page_text and f'value="{came_in}">Oct 4 · From my checking' in page_text

    response = client.post(f"/transactions/{out}/other-side", data={"other_id": str(came_in), "back": f"/transactions/{out}"})
    assert "as one move between your accounts" in unescape(response.text)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM transactions WHERE id IN (?, ?)", (out, came_in))}
    assert (rows[out]["transfer_id"], rows[came_in]["transfer_id"]) == (came_in, out)
    assert all(r["type"] == "transfer" and r["excluded"] == 0 and r["reviewed"] == 1 for r in rows.values())
    listing = unescape(client.get("/transactions?transfers=1").text)
    assert "to Chase Sapphire" in listing and "from Acme Checking" in listing
    transfer_page = unescape(client.get(f"/transactions/{out}").text)
    assert "Other side" in transfer_page and "data-recurring-field hidden" in transfer_page and "data-category-field hidden" in transfer_page

    # Only opposite amounts on different accounts pair; unlinking keeps both transfers; a type change unlinks.
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    assert "One side has to be money out" in client.post(f"/transactions/{shell}/other-side", data={"other_id": str(out)}).text
    client.post(f"/transactions/{out}/other-side", data={"other_id": ""})
    assert conn.execute("SELECT count(*) FROM transactions WHERE transfer_id IS NOT NULL").fetchone()[0] == 0
    client.post(f"/transactions/{out}/other-side", data={"other_id": str(came_in)})
    client.post(f"/transactions/{came_in}", data={"name": "From my checking", "type": "regular", "date": "2026-10-04"})
    assert conn.execute("SELECT count(*) FROM transactions WHERE transfer_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_linked_rows_are_drawn_as_a_group():
    from app.main import joined_to_their_link
    row = lambda tid, reimburses=None, transfer=None: {"id": tid, "reimburses_id": reimburses, "transfer_id": transfer}
    rows = [row(1), row(2, reimburses=1), row(3, reimburses=1), row(4), row(5, transfer=6), row(6, transfer=5)]
    drawn = joined_to_their_link(rows)
    assert [t["joined"] for t in drawn] == [False, True, True, False, False, True]
    assert [t["more"] for t in drawn] == [False, True, False, False, False, False]


def test_amounts_are_read_as_one_or_a_range():
    from app.main import amount_range
    assert amount_range("12.50") == (12.5, 12.5) and amount_range("-$1,200") == (1200.0, 1200.0)
    assert amount_range("10-20") == (10.0, 20.0) and amount_range("20 – 10") == (10.0, 20.0) and amount_range("10..20") == (10.0, 20.0)
    assert amount_range(">100") == (100.0, None) and amount_range("<= 50") == (None, 50.0)
    assert amount_range("lunch") is None and amount_range("nan") is None and amount_range("10-") is None


def test_transactions_can_be_found_by_amount(client, settings):
    import_sample(client, settings)
    listed = lambda amount: unescape(client.get("/transactions", params={"amount": amount}).text)
    one = listed("61.10")
    assert "1 transaction ·" in one and "$61.10" in one and "$54.20" not in one
    range_ = listed("15-20")
    assert "4 transactions ·" in range_ and "Netflix" in range_ and "Amazon" in range_  # money out and in alike
    moves = listed("500")
    assert "Chase Payment" in moves and "Payment Thank You" in moves  # transfers show when looking for an amount
    assert "Acme Payroll" in listed(">1000") and "1 transaction ·" in listed(">1000")
    assert "Couldn't read “cheap” as an amount" in listed("cheap")
    conn = sync.connect(settings.db_path)
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    conn.close()
    assert 'href="/transactions?amount=40.00">Find others with this amount' in client.get(f"/transactions/{shell}").text


def test_selected_transactions_change_together(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    ids = lambda *names: [conn.execute("SELECT id FROM transactions WHERE name = ?", (n,)).fetchone()[0] for n in names]
    shell, amazon, payment = ids("Shell", "Amazon", "Chase Payment")
    page_text = unescape(client.get("/transactions?select=1&transfers=1").text)
    assert f'name="ids" value="{shell}" form="bulk"' in page_text and 'id="bulk"' in page_text and "Done selecting" in page_text
    assert 'form="bulk"' not in client.get("/transactions").text  # checkboxes only while selecting

    bulk = lambda action, picked, **form: unescape(client.post("/transactions/bulk", data={"ids": [str(i) for i in picked], "action": action,
                                                                                         "back": "/transactions?select=1", **form}).text)
    assert "Select some transactions first" in bulk("review", [])
    from app import tags
    with conn:
        todo = tags.get_or_create(conn, "TODO: check")
    assert "Pick a tag first" in bulk("tag", [shell])
    assert "Tagged 2 transactions “TODO: check”" in bulk("tag", [shell, amazon], tag_id=str(todo))
    assert "Took “TODO: check” off 1 transaction" in bulk("untag", [shell], tag_id=str(todo))

    gas = conn.execute("SELECT id FROM categories WHERE name = 'Gas'").fetchone()[0]
    assert "Pick a category first" in bulk("category", [shell])
    message = bulk("category", [shell, amazon, payment], category_id=str(gas))
    assert "2 transactions now in Gas" in message and "1 transaction left as they were" in message
    assert {r[0] for r in conn.execute(f"SELECT category_id FROM transactions WHERE id IN ({shell}, {amazon})")} == {gas}
    assert conn.execute("SELECT category_id FROM transactions WHERE id = ?", (payment,)).fetchone()[0] is None  # a transfer takes none
    assert "2 transactions excluded from spending" in bulk("exclude", [shell, amazon])
    assert reports.month_totals(conn, "2026-10")["spent"] == 0.0
    assert "counted in spending again" in bulk("include", [shell, amazon])
    with conn:
        conn.execute(f"UPDATE transactions SET reviewed = 0 WHERE id IN ({shell}, {amazon})")
    assert "Marked 2 transactions as reviewed" in bulk("review", [shell, amazon])

    # Combining needs one account, rows without a bank record, a few days apart at most.
    joes = conn.execute("SELECT id FROM transactions WHERE name = 'Trader Joe''s' AND date = '2026-09-08'").fetchone()[0]
    assert "can be combined" in bulk("combine", [shell, joes])  # a month apart
    first_joes = conn.execute("SELECT id FROM transactions WHERE name = 'Trader Joe''s' AND date = '2026-09-01'").fetchone()[0]
    netflix = conn.execute("SELECT id FROM transactions WHERE name = 'Netflix' AND date = '2026-09-03'").fetchone()[0]
    before = reports.month_totals(conn, "2026-09")
    assert "Combined 2 transactions into “Groceries run”" in bulk("combine", [first_joes, netflix], name="Groceries run")
    combined = conn.execute("SELECT id, amount FROM transactions WHERE source = 'group'").fetchone()
    assert combined["amount"] == round(-54.20 - 15.49, 2) and reports.month_totals(conn, "2026-09") == before
    conn.close()


def test_a_transactions_page_shows_what_the_bank_says(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    shell = conn.execute("SELECT id FROM transactions WHERE name = 'Shell'").fetchone()[0]
    with conn:
        conn.execute("""UPDATE transactions SET plaid_transaction_id = 'bank-123', original_name = 'SHELL OIL 5741', plaid_category = 'TRANSPORTATION_GAS',
                        plaid_details = ? WHERE id = ?""",
                     (json.dumps({"posted": "2026-10-05", "made": "2026-10-03", "channel": "in store", "code": "purchase", "where": "Lakeside, ZZ",
                                  "counterparties": [{"name": "Shell", "type": "merchant"}], "confidence": "HIGH"}), shell))
    page_text = unescape(client.get(f"/transactions/{shell}").text)
    section = page_text[page_text.index("From your bank"):]
    for shown in ("SHELL OIL 5741", "Posted", "Mon, Oct 5", "In store", "Purchase", "Lakeside, ZZ", "Shell · merchant",
                  "Transportation › Gas · high confidence", "bank-123"):
        assert shown in section, shown
    joes = conn.execute("SELECT id FROM transactions WHERE name = 'Trader Joe''s' AND date = '2026-09-08'").fetchone()[0]
    assert "From your bank" not in client.get(f"/transactions/{joes}").text  # nothing from a bank for this one
    conn.close()


def test_the_bank_card_shows_the_purchase_time_in_the_app_time_zone(pacific):
    card = lambda details: bank_details({"plaid_details": json.dumps(details)})
    evening = card({"made": "2026-10-09", "time": "2026-10-09T01:30:00Z", "posted": "2026-10-10"})
    assert (evening["made"], evening["time"], evening["posted"]) == ("2026-10-08", "6:30 PM", "2026-10-10")
    assert card({"made": "2026-10-09", "time": "2026-10-09T00:00:00Z"}) == {"made": "2026-10-09"}  # a bank's default midnight


def test_accounts_can_be_added_archived_or_hidden_and_the_picker_follows(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    checking, card = (conn.execute("SELECT id FROM accounts WHERE name = ?", (n,)).fetchone()[0] for n in ("Acme Checking", "Chase Sapphire"))
    response = client.post("/accounts/new", data={"name": "Gift card"})
    assert "Added “Gift card”." in unescape(response.text)
    gift = conn.execute("SELECT id, type, source, hidden, archived FROM accounts WHERE name = 'Gift card'").fetchone()
    assert tuple(gift)[1:] == ("other", "manual", 0, 0)
    # The account picked last is picked again next time.
    client.post("/transactions/new", data={"date": "2026-09-30", "name": "Store credit", "amount": "5", "direction": "out",
                                           "account": str(gift["id"]), "type": "regular"})
    assert f'<option value="{gift["id"]}" selected>Gift card' in client.get("/transactions/new").text
    # It shows what's left on it after its transactions.
    row = unescape(client.get("/accounts").text).split("<strong>Gift card</strong>")[1].split("</summary>")[0]
    assert "what's left after its transactions" in row and '<span class="num">-$5.00</span>' in row
    # Archived, an account's history still counts, but it isn't offered any more; hidden, neither.
    before = reports.month_totals(conn, "2026-09")
    client.post(f"/accounts/{checking}", data={"name": "Acme Checking", "archived": "1"})
    client.post(f"/accounts/{card}", data={"name": "Chase Sapphire", "hidden": "1"})
    page_text = client.get("/transactions/new").text
    assert "Acme Checking" not in page_text and "Chase Sapphire" not in page_text and "Gift card" in page_text
    assert reports.month_totals(conn, "2026-09")["income"] == before["income"]  # the archived account's paycheck still counts
    accounts_page = unescape(client.get("/accounts").text)
    assert "Archived (1)" in accounts_page and accounts_page.index("Archived (1)") < accounts_page.index("Acme Checking", accounts_page.index("Archived (1)"))
    conn.close()


def test_a_transactions_fields_save_quietly_as_they_change(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    shell = conn.execute("SELECT * FROM transactions WHERE name = 'Shell'").fetchone()
    gas, shopping = (conn.execute("SELECT id FROM categories WHERE name = ?", (n,)).fetchone()[0] for n in ("Gas", "Shopping"))
    here = f"/transactions/{shell['id']}?back=%2Ftransactions"
    form = {"name": "Shell", "date": shell["date"], "type": "regular", "category_id": str(gas), "reviewed": "1", "notes": "Road trip",
            "tags_shown": "1", "new_tags": "Trip, Work", "back": here, "quiet": "1"}
    # Saved as it changes (app.js sends quiet), with no message: the pane shows what changed.
    page_text = unescape(client.post(f"/transactions/{shell['id']}", data=form).text)
    assert "data-toast" not in page_text and ">Road trip</textarea>" in page_text and "Regular transaction" in page_text
    trip, work = (conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()[0] for n in ("Trip", "Work"))
    assert page_text.count('<label class="tag-chip ') == 2
    # Taking a tag off leaves it to add again, among the others; a new category offers a rule for the name.
    page_text = unescape(client.post(f"/transactions/{shell['id']}", data={**form, "new_tags": "", "tag_ids": [str(trip)], "category_id": str(shopping)}).text)
    assert page_text.count('<label class="tag-chip ') == 1 and '<option value="Work">' in page_text
    assert "“Shell” is now Shopping." in page_text and "Always put “Shell” in Shopping" in page_text
    assert work and "Make a rule for this name" in page_text
    conn.close()


def test_the_sidebar_lists_the_accounts_in_use_by_group(client, settings):
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    with conn:
        conn.execute("UPDATE accounts SET balance = 2000 WHERE name = 'Acme Checking'")
        conn.execute("UPDATE accounts SET balance = 120.5 WHERE name = 'Chase Sapphire'")
        other = conn.execute("INSERT INTO accounts (name, type, mask, source, balance) VALUES ('Chase Sapphire', 'credit', '5555', 'copilot', 30)").lastrowid
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()[0]
    sidebar = lambda path="/": unescape(client.get(path).text).split('<aside class="sidebar">')[1].split("</aside>")[0]
    shown = sidebar()
    # Each group shows what its accounts add up to, as banks show balances; a name two of them share gets their digits.
    assert shown.index("Cash") < shown.index("$2,000.00") < shown.index("Credit cards") < shown.index("$150.50")
    assert "Chase Sapphire ••1234" in shown and "Chase Sapphire ••5555" in shown
    # The account open in the list is lit up, and searching from there keeps the list's filters.
    listing = sidebar(f"/transactions?account={checking}&month=2026-09")
    assert f'<a href="/transactions?account={checking}" class="active" aria-current="page"' in listing
    assert f'<input type="hidden" name="month" value="2026-09"><input type="hidden" name="account" value="{checking}">' in listing
    assert '<input type="hidden"' not in sidebar("/budgets").split('class="side-search"')[1].split("</form>")[0]  # elsewhere it searches everything
    # A group stays folded from page to page: app.js keeps the folded ones in a cookie.
    client.cookies.set("sidebar_folded", "credit")
    assert '<details data-group="credit">' in sidebar() and '<details data-group="depository" open>' in sidebar()
    # Archived and hidden accounts aren't listed, nor a group left with none.
    client.post(f"/accounts/{checking}", data={"name": "Acme Checking", "archived": "1"})
    client.post(f"/accounts/{other}", data={"name": "Chase Sapphire", "hidden": "1"})
    shown = sidebar()
    assert 'data-group="depository"' not in shown and '<span>Chase Sapphire</span><span class="num">$120.50</span>' in shown
    conn.close()


def test_browsers_and_phones_find_the_app_icon(client):
    page_text = client.get("/").text
    assert '<link rel="icon" href="/favicon.ico" sizes="32x32">' in page_text and '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page_text
    for path, kind in (("/favicon.ico", "image/"), ("/apple-touch-icon.png", "image/png"), ("/static/icon.svg", "image/svg+xml")):
        response = client.get(path)
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), path


def test_odd_values_typed_into_a_url_are_ignored(client):
    for query in ("category=abc", "account=abc", "page=99999999999999999999", "category=1%20OR%201", "account=-1"):
        assert client.get(f"/transactions?{query}").status_code == 200, query


def test_an_amount_has_to_be_a_finite_number(client):
    for typed in ("inf", "nan", "-inf", "1e999"):
        response = client.post("/transactions/new", data={"date": "2026-10-01", "name": "Odd", "amount": typed, "direction": "out",
                                                          "account": "cash", "type": "regular"})
        assert response.status_code == 400 and "Check the date and the amount" in unescape(response.text), typed
    assert client.get("/").status_code == 200
    response = client.post("/recurring/track", data={"name": "Gym", "pattern": "gym", "frequency": "monthly", "amount": "nan"})
    assert "isn't an amount" in unescape(response.text)


def test_the_export_keeps_formulas_as_text(client):
    client.post("/transactions/new", data={"date": "2026-10-01", "name": "=HYPERLINK(\"x\")", "amount": "5", "direction": "out",
                                           "account": "cash", "type": "regular", "notes": "+1 note"})
    lines = client.get("/export.csv").text.splitlines()
    assert any(line.startswith('2026-10-01,"\'=HYPERLINK(""x"")",-5.00,') for line in lines), lines
    assert any(",'+1 note," in line for line in lines)
    assert 'filename="monthlight-export-2026-10-14.csv"' in client.get("/export.csv").headers["content-disposition"]


def test_other_websites_cant_frame_the_pages(client):
    headers = client.get("/").headers
    assert headers["x-frame-options"] == "DENY" and headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["x-content-type-options"] == "nosniff"


def test_odd_ids_typed_into_a_form_are_ignored(client, settings):
    response = client.post("/transactions/new", data={"date": "2026-10-01", "name": "Odd id", "amount": "5", "direction": "out",
                                                      "account": "cash", "type": "regular", "category_id": "abc"})
    assert response.status_code == 200
    conn = sync.connect(settings.db_path)
    assert conn.execute("SELECT category_id FROM transactions WHERE name = 'Odd id'").fetchone()[0] is None
    conn.close()
