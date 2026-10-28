import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import copilot_import, reports, sync
from app.db import connect
from app.plaid_client import PlaidError
from tests.conftest import COPILOT_CSV, FakePlaid, mutation_error, page, plaid_account, plaid_txn


def rows(conn):
    return {r["plaid_transaction_id"] or r["name"]: r for r in conn.execute("SELECT * FROM transactions")}


def link(conn, fake):
    return sync.link_item(conn, fake, "public-sandbox-1", {"institution_id": "ins_1", "name": "Chase"})


def test_new_bank_without_history_syncs_right_away(conn):
    fake = FakePlaid(pages={None: page([
        plaid_txn("t1", 54.2, "2026-10-08", "TRADER JOE S #123", merchant="Trader Joe's", pfc="FOOD_AND_DRINK_GROCERIES"),
        plaid_txn("t2", -2500, "2026-10-09", "ACME PAYROLL", pfc="INCOME_SALARY"),
        plaid_txn("t3", 300, "2026-10-10", "PAYMENT THANK YOU", pfc="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
    ], "c1")})
    item = link(conn, fake)
    assert item["status"] == "ok"
    result = sync.sync_item(conn, fake, "item-1")
    assert result.added == 3

    by_id = rows(conn)
    groceries = conn.execute("SELECT id FROM categories WHERE name = 'Groceries'").fetchone()["id"]
    assert by_id["t1"]["amount"] == -54.2 and by_id["t1"]["name"] == "Trader Joe's"
    assert by_id["t1"]["category_id"] == groceries
    assert by_id["t1"]["original_name"] == "TRADER JOE S #123"
    assert by_id["t2"]["type"] == "income" and by_id["t2"]["amount"] == 2500
    assert by_id["t3"]["type"] == "transfer" and by_id["t3"]["category_id"] is None

    saved = conn.execute("SELECT cursor, update_status FROM plaid_items").fetchone()
    assert tuple(saved) == ("c1", "HISTORICAL_UPDATE_COMPLETE")
    account = conn.execute("SELECT * FROM accounts").fetchone()
    assert (account["name"], account["mask"], account["type"], account["balance"]) == ("Sapphire Preferred", "1234", "credit", 100.0)
    assert conn.execute("SELECT count(*) FROM balance_history").fetchone()[0] == 1


def test_posted_transaction_replaces_pending_and_keeps_edits(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("p1", 12.0, "2026-10-08", "BLUE BOTTLE", pending=True)], "c1"),
        "c1": page([plaid_txn("t1", 14.5, "2026-10-09", "BLUE BOTTLE", pending_id="p1")], "c2", removed=["p1"]),
    })
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    coffee = conn.execute("INSERT INTO categories (name) VALUES ('Treats')").lastrowid
    conn.execute("UPDATE transactions SET notes = 'with Sam', category_id = ?, user_edited = 1", (coffee,))
    conn.commit()

    sync.sync_item(conn, fake, "item-1")
    [row] = conn.execute("SELECT * FROM transactions").fetchall()
    assert (row["plaid_transaction_id"], row["pending"], row["amount"]) == ("t1", 0, -14.5)
    assert (row["notes"], row["category_id"]) == ("with Sam", coffee)


def test_modified_and_removed_transactions(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("t1", 10, "2026-10-01", "A"), plaid_txn("t2", 20, "2026-10-02", "B")], "c1"),
        "c1": page([], "c2", modified=[plaid_txn("t1", 11, "2026-10-01", "A")], removed=["t2"]),
    })
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    result = sync.sync_item(conn, fake, "item-1")
    assert (result.updated, result.removed) == (1, 1)
    assert [(r["plaid_transaction_id"], r["amount"]) for r in conn.execute("SELECT * FROM transactions")] == [("t1", -11.0)]


def test_rules_beat_plaid_categories(conn):
    fake = FakePlaid(pages={None: page([plaid_txn("t1", 9, "2026-10-01", "VENMO", pfc="TRANSFER_OUT_FROM_APPS")], "c1")})
    link(conn, fake)
    dining = conn.execute("SELECT id FROM categories WHERE name = 'Restaurants'").fetchone()["id"]
    conn.execute("INSERT INTO rules (pattern, match, category_id, type) VALUES ('venmo', 'contains', ?, 'regular')", (dining,))
    conn.commit()
    sync.sync_item(conn, fake, "item-1")
    t = conn.execute("SELECT * FROM transactions").fetchone()
    assert (t["category_id"], t["type"]) == (dining, "regular")


def test_copilot_history_merges_without_duplicates(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    fake = FakePlaid(
        accounts=[plaid_account("acc-card", "Sapphire Preferred", "1234"),
                  plaid_account("acc-check", "Checking", "9876", "depository", "checking", 2000.0)],
        pages={None: page([
            plaid_txn("deep", 54.2, "2026-09-01", "Trader Joe's"),          # well inside the imported history
            plaid_txn("edge-same", 40.0, "2026-10-02", "SHELL OIL"),         # the imported Shell charge, a day apart
            plaid_txn("edge-new", 6.5, "2026-10-04", "BLUE BOTTLE"),         # was pending in Copilot, so not imported
            plaid_txn("after", 25.0, "2026-10-10", "CHIPOTLE"),              # after Copilot stopped syncing
            plaid_txn("older", 80.0, "2026-06-01", "IKEA"),                  # before the imported history starts
            plaid_txn("check-1", -2500, "2026-10-12", "ACME PAYROLL", account="acc-check", pfc="INCOME_SALARY"),
        ], "c1")},
    )
    item = link(conn, fake)
    assert item["status"] == "needs_match"

    pairs = sync.suggest_matches(conn, item)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()["id"]
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()["id"]
    assert {p["plaid"]["account_id"]: p["suggested"] for p in pairs} == {"acc-card": card, "acc-check": checking}

    sync.apply_matches(conn, "item-1", {"acc-card": card, "acc-check": checking})
    result = sync.sync_item(conn, fake, "item-1")
    assert (result.skipped, result.matched, result.added) == (1, 1, 4)

    # Rows added to an imported account weren't in the import, so they wait for review, however old.
    assert {r["plaid_transaction_id"]: r["reviewed"] for r in conn.execute(
        "SELECT plaid_transaction_id, reviewed FROM transactions WHERE source = 'plaid'")} == {
        "edge-new": 0, "after": 0, "older": 0, "check-1": 0}
    shell = conn.execute("SELECT * FROM transactions WHERE name = 'Shell'").fetchone()
    assert shell["plaid_transaction_id"] == "edge-same" and shell["source"] == "copilot"
    assert conn.execute("SELECT count(*) FROM transactions WHERE account_id = ?", (card,)).fetchone()[0] == 8 + 3
    account = conn.execute("SELECT * FROM accounts WHERE id = ?", (card,)).fetchone()
    assert (account["name"], account["plaid_account_id"], account["history_cutoff"]) == ("Chase Sapphire", "acc-card", "2026-10-03")
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 2  # no duplicate accounts


def test_matching_an_account_twice_is_refused(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    fake = FakePlaid(accounts=[plaid_account("a", "One", "1111"), plaid_account("b", "Two", "2222")])
    link(conn, fake)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()["id"]
    with pytest.raises(ValueError):
        sync.apply_matches(conn, "item-1", {"a": card, "b": card})


def test_pagination_restarts_after_mutation_error(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("t1", 1, "2026-10-01", "A")], "c1", has_more=True),
        "c1": page([plaid_txn("t2", 2, "2026-10-02", "B")], "c2"),
    })
    fake.errors["c1"] = mutation_error()
    link(conn, fake)
    result = sync.sync_item(conn, fake, "item-1")
    assert fake.cursors == [None, "c1", None, "c1"]
    assert result.added == 2  # nothing doubled from the abandoned first pass


def test_run_sync_records_login_errors(settings, monkeypatch):
    conn = connect(settings.db_path)
    fake = FakePlaid()
    link(conn, fake)
    fake.errors["accounts"] = PlaidError("ITEM_LOGIN_REQUIRED", "login required", "Please log in to Chase again.")
    monkeypatch.setattr(sync, "make_client", lambda s: fake)
    results = sync.run_sync(settings)
    assert results == [("Chase", "Please log in to Chase again.")]
    item = conn.execute("SELECT status, error FROM plaid_items").fetchone()
    assert tuple(item) == ("login_required", "Please log in to Chase again.")
    assert sync.due_items(conn, settings) == []  # waits for the user instead of retrying
    conn.close()


def test_due_items_checks_new_banks_often(settings):
    conn = connect(settings.db_path)
    link(conn, FakePlaid())
    now = datetime.now(UTC)
    # Never synced: due.
    assert sync.due_items(conn, settings, now) == ["item-1"]
    conn.execute("UPDATE plaid_items SET last_synced_at = ?, update_status = 'INITIAL_UPDATE_COMPLETE'", ((now - timedelta(minutes=3)).isoformat(),))
    assert sync.due_items(conn, replace(settings, sync_interval_hours=6), now) == ["item-1"]
    conn.execute("UPDATE plaid_items SET update_status = 'HISTORICAL_UPDATE_COMPLETE'")
    assert sync.due_items(conn, replace(settings, sync_interval_hours=6), now) == []
    conn.close()


def test_disconnect_keeps_copilot_history_even_when_deleting(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    fake = FakePlaid(
        accounts=[plaid_account("acc-card", "Sapphire", "1234"), plaid_account("acc-new", "Savings", "5555", "depository", "savings")],
        pages={None: page([plaid_txn("n1", 5, "2026-10-12", "X"), plaid_txn("s1", -1, "2026-10-12", "Interest", account="acc-new")], "c1")},
    )
    link(conn, fake)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()["id"]
    sync.apply_matches(conn, "item-1", {"acc-card": card, "acc-new": None})
    sync.sync_item(conn, fake, "item-1")

    sync.disconnect_item(conn, fake, "item-1", delete_data=True)
    assert fake.removed
    assert conn.execute("SELECT count(*) FROM plaid_items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM transactions WHERE source = 'plaid'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM transactions WHERE source = 'copilot'").fetchone()[0] == 10
    names = {r["name"] for r in conn.execute("SELECT name FROM accounts")}
    assert names == {"Chase Sapphire", "Acme Checking"}  # the Plaid-only Savings account went away
    assert conn.execute("SELECT count(*) FROM accounts WHERE plaid_account_id IS NOT NULL").fetchone()[0] == 0


def test_connection_is_kept_when_its_accounts_fail_to_load(settings, monkeypatch):
    conn = connect(settings.db_path)
    fake = FakePlaid(pages={None: page([plaid_txn("t1", 5, "2026-10-12", "X")], "c1")})
    fake.errors["accounts"] = PlaidError("INSTITUTION_NOT_RESPONDING", "not responding")
    item = link(conn, fake)
    # The access token is saved anyway, so the Trial plan slot this login used isn't wasted.
    assert (item["status"], item["access_token"]) == ("error", "access-sandbox-1")
    assert item["error"] == "The bank isn't responding right now. The app will try again later."
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
    assert sync.due_items(conn, settings) == ["item-1"]

    monkeypatch.setattr(sync, "make_client", lambda s: fake)
    assert sync.run_sync(settings) == [("Chase", "1 new, 0 updated, 0 removed")]
    assert tuple(conn.execute("SELECT status, error FROM plaid_items").fetchone()) == ("ok", None)
    assert conn.execute("SELECT count(*) FROM accounts WHERE plaid_item_id = 'item-1'").fetchone()[0] == 1
    conn.close()


def test_exchange_is_tried_again_only_after_a_network_error(conn):
    fake = FakePlaid()
    fake.errors["exchange"] = PlaidError("NETWORK_ERROR", "Couldn't reach Plaid")
    assert link(conn, fake)["status"] == "ok"
    fake.errors["exchange"] = PlaidError("INVALID_PUBLIC_TOKEN", "already used")
    with pytest.raises(PlaidError):
        link(conn, fake)
    assert fake.exchanges == 1


def test_new_accounts_found_later_are_matched_before_syncing(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    card_txns = [plaid_txn("after", 25.0, "2026-10-10", "CHIPOTLE")]
    fake = FakePlaid(pages={None: page(card_txns, "c1"), "c1": page([], "c1")})
    link(conn, fake)
    card = conn.execute("SELECT id FROM accounts WHERE name = 'Chase Sapphire'").fetchone()["id"]
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Acme Checking'").fetchone()["id"]
    sync.apply_matches(conn, "item-1", {"acc-card": card})
    sync.sync_item(conn, fake, "item-1")

    # Later the user shares their checking account too, through Add or remove accounts.
    fake.accounts.append(plaid_account("acc-check", "Checking", "9876", "depository", "checking", 2000.0))
    fake.pages[None] = page(card_txns + [
        plaid_txn("pay", -2500, "2026-09-15", "ACME PAYROLL", account="acc-check", pfc="INCOME_SALARY"),  # imported already
        plaid_txn("chk-new", 30, "2026-10-12", "GROCER", account="acc-check"),
    ], "c2")
    result = sync.sync_item(conn, fake, "item-1")
    assert result.needs_match and result.summary() == "new accounts need matching"
    item = conn.execute("SELECT * FROM plaid_items").fetchone()
    assert (item["status"], item["cursor"]) == ("needs_match", None)
    assert [a["account_id"] for a in json.loads(item["pending_accounts"])] == ["acc-check"]
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 2  # not added as a third account
    assert [p["suggested"] for p in sync.suggest_matches(conn, item)] == [checking]

    sync.apply_matches(conn, "item-1", {"acc-check": checking})
    result = sync.sync_item(conn, fake, "item-1")
    # Everything is fetched again from the start, and nothing is doubled.
    assert (result.updated, result.matched, result.added) == (1, 1, 1)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 10 + 2
    assert conn.execute("SELECT plaid_transaction_id FROM transactions WHERE name = 'Acme Payroll'").fetchone()[0] == "pay"


def test_finding_a_second_login_to_the_same_bank(conn):
    link(conn, FakePlaid())  # Chase, with a credit card ending 1234
    chase = {"institution_id": "ins_1", "name": "Chase"}
    card = [{"mask": "1234", "subtype": "credit card"}]
    assert sync.find_duplicate(conn, "sandbox", chase, card)["item_id"] == "item-1"
    assert sync.find_duplicate(conn, "sandbox", chase, [{"mask": "5555", "subtype": "checking"}]) is None  # another login there
    assert sync.find_duplicate(conn, "sandbox", chase, []) is not None  # can't tell, so assume the worst
    assert sync.find_duplicate(conn, "production", chase, card) is None
    assert sync.find_duplicate(conn, "sandbox", {"institution_id": "ins_2"}, card) is None
    assert sync.find_duplicate(conn, "sandbox", None, card) is None


def test_a_login_waiting_for_matching_counts_as_connected(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    assert link(conn, FakePlaid())["status"] == "needs_match"
    assert sync.find_duplicate(conn, "sandbox", {"institution_id": "ins_1"}, [{"mask": "1234", "subtype": "credit card"}]) is not None


def test_failing_new_banks_are_retried_less_often(settings):
    conn = connect(settings.db_path)
    link(conn, FakePlaid())
    now = datetime.now(UTC)
    six_hours = replace(settings, sync_interval_hours=6)
    conn.execute("UPDATE plaid_items SET status = 'error', last_synced_at = ?", ((now - timedelta(minutes=3)).isoformat(),))
    assert sync.due_items(conn, six_hours, now) == []
    conn.execute("UPDATE plaid_items SET last_synced_at = ?", ((now - timedelta(minutes=16)).isoformat(),))
    assert sync.due_items(conn, six_hours, now) == ["item-1"]
    conn.close()


def test_loan_payments_count_once(conn):
    fake = FakePlaid(
        accounts=[plaid_account("acc-check", "Checking", "1111", "depository", "checking", 2000.0),
                  plaid_account("acc-home", "Mortgage", "2222", "loan", "mortgage", 250000.0)],
        pages={None: page([
            plaid_txn("out", 549, "2026-10-01", "MORTGAGE PAYMENT", account="acc-check", pfc="LOAN_PAYMENTS_MORTGAGE_PAYMENT"),
            plaid_txn("in", -549, "2026-10-01", "Monthly payment", account="acc-home", pfc="LOAN_PAYMENTS_MORTGAGE_PAYMENT"),
            plaid_txn("interest", 300, "2026-10-01", "Interest", account="acc-home", pfc="BANK_FEES_INTEREST_CHARGE"),
            plaid_txn("lunch", 20, "2026-10-02", "Lunch", account="acc-check", pfc="FOOD_AND_DRINK_RESTAURANT"),
        ], "c1")},
    )
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    types = {r["plaid_transaction_id"]: r["type"] for r in conn.execute("SELECT plaid_transaction_id, type FROM transactions")}
    assert types == {"out": "regular", "in": "transfer", "interest": "transfer", "lunch": "regular"}
    assert reports.month_totals(conn, "2026-10")["spent"] == 569.0  # the payment once, plus lunch


def test_money_moved_between_own_accounts_through_an_app_is_a_transfer(conn):
    fake = FakePlaid(
        accounts=[plaid_account("acc-tartan", "Tartan Checking", "1111", "depository", "checking"),
                  plaid_account("acc-acme", "Acme Checking", "2222", "depository", "checking")],
        pages={None: page([
            plaid_txn("out", 3000, "2026-10-01", "ZELLE TO ME", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("in", -3000, "2026-10-02", "ZELLE FROM ME", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("odd", 500, "2026-10-03", "ONLINE TRANSFER", account="acc-tartan", pfc="OTHER_OTHER"),
            plaid_txn("odd-in", -500, "2026-10-03", "VENMO CASHOUT", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("dinner", 40, "2026-10-04", "VENMO TO SAM", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("payback", -26, "2026-10-04", "ZELLE FROM ALEX", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("far", 70, "2026-10-01", "ZELLE", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("far-in", -70, "2026-10-09", "ZELLE", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("lunch", 15, "2026-10-05", "LUNCH", account="acc-tartan", pfc="FOOD_AND_DRINK_RESTAURANT"),
            plaid_txn("refund", -15, "2026-10-05", "LUNCH REFUND", account="acc-acme", pfc="FOOD_AND_DRINK_RESTAURANT"),
        ], "c1")},
    )
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    types = {r["plaid_transaction_id"]: r["type"] for r in conn.execute("SELECT plaid_transaction_id, type FROM transactions")}
    assert types == {"out": "transfer", "in": "transfer", "odd": "transfer", "odd-in": "transfer",
                     "dinner": "regular", "payback": "regular", "far": "regular", "far-in": "regular",
                     "lunch": "regular", "refund": "regular"}
    assert conn.execute("SELECT count(*) FROM transactions WHERE type = 'transfer' AND category_id IS NOT NULL").fetchone()[0] == 0
    assert reports.month_totals(conn, "2026-10")["spent"] == 40 - 26 + 70 - 70 + 15 - 15


def test_marking_one_side_as_a_transfer_makes_the_other_follow(conn):
    fake = FakePlaid(
        accounts=[plaid_account("acc-tartan", "Tartan Checking", "1111", "depository", "checking"),
                  plaid_account("acc-acme", "Acme Checking", "2222", "depository", "checking")],
        pages={None: page([
            plaid_txn("out", 3000, "2026-10-01", "ZELLE TO ME", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("in", -3000, "2026-10-01", "ZELLE FROM ME", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("kept", 200, "2026-10-03", "ZELLE TO SAM", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("kept-in", -200, "2026-10-03", "ZELLE FROM SAM", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
        ], "c1")},
    )
    link(conn, fake)
    # After the first sync's pairing, the user turned one side of each move back into a regular transaction by hand
    # (which, in the app, also unlinks the two sides).
    sync.sync_item(conn, fake, "item-1")
    conn.execute("UPDATE transactions SET type = 'regular', user_edited = 1, transfer_id = NULL WHERE plaid_transaction_id IN ('out', 'kept')")
    conn.execute("UPDATE transactions SET type = 'regular', transfer_id = NULL WHERE plaid_transaction_id IN ('in', 'kept-in')")
    # Marking "out" a transfer by hand makes its other side follow; "kept" stays regular, as the user left it.
    conn.execute("UPDATE transactions SET type = 'transfer' WHERE plaid_transaction_id = 'out'")
    assert sync.pair_own_transfers(conn) == 1
    types = {r["plaid_transaction_id"]: r["type"] for r in conn.execute("SELECT plaid_transaction_id, type FROM transactions")}
    assert types == {"out": "transfer", "in": "transfer", "kept": "regular", "kept-in": "regular"}
    assert sync.pair_own_transfers(conn) == 0  # nothing left to change, and a used pair isn't reused


def test_a_date_set_by_hand_survives_bank_updates(conn):
    fake = FakePlaid(pages={
        None: page([plaid_txn("p1", 12.0, "2026-10-08", "BLUE BOTTLE", pending=True), plaid_txn("t2", 20, "2026-10-02", "B")], "c1"),
        "c1": page([plaid_txn("t1", 14.5, "2026-10-09", "BLUE BOTTLE", pending_id="p1")], "c2", removed=["p1"],
                   modified=[plaid_txn("t2", 21, "2026-10-01", "B")]),
    })
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    conn.execute("UPDATE transactions SET date = '2026-09-30', date_edited = 1 WHERE plaid_transaction_id = 'p1'")
    conn.commit()

    sync.sync_item(conn, fake, "item-1")
    by_id = rows(conn)
    assert (by_id["t1"]["date"], by_id["t1"]["amount"], by_id["t1"]["pending"]) == ("2026-09-30", -14.5, 0)  # posted, still the user's date
    assert (by_id["t2"]["date"], by_id["t2"]["amount"]) == ("2026-10-01", -21.0)  # not edited, so the bank's change applies


def test_a_sync_never_moves_a_date_later(conn):
    later_purchase = plaid_txn("t3", 30.0, "2026-10-12", "SHOP", pending_id="p3")
    later_purchase["authorized_date"] = "2026-10-10"
    fake = FakePlaid(pages={
        None: page([plaid_txn("p1", 12.0, "2026-10-08", "BLUE BOTTLE", pending=True),
                    plaid_txn("p2", 9.0, "2026-10-08", "CORNER STORE", pending=True),
                    plaid_txn("p3", 30.0, "2026-10-08", "SHOP", pending=True)], "c1"),
        "c1": page([plaid_txn("t1", 12.0, "2026-10-11", "BLUE BOTTLE", pending_id="p1"), later_purchase], "c2",
                   removed=["p1", "p3"], modified=[plaid_txn("p2", 9.0, "2026-10-09", "CORNER STORE", pending=True)]),
        "c2": page([], "c3", modified=[plaid_txn("t1", 12.5, "2026-10-11", "BLUE BOTTLE")]),
    })
    link(conn, fake)
    for _ in range(3):
        sync.sync_item(conn, fake, "item-1")
    by_id = rows(conn)
    # The bank's update after the posting changes the amount, not the day.
    assert (by_id["t1"]["date"], by_id["t1"]["amount"]) == ("2026-10-08", -12.5)
    assert by_id["p2"]["date"] == "2026-10-08"  # a pending row the bank dates a day later
    assert by_id["t3"]["date"] == "2026-10-08"  # a purchase date after the pending day


def test_the_two_sides_of_a_move_are_linked(conn):
    fake = FakePlaid(
        accounts=[plaid_account("acc-tartan", "Tartan Checking", "1111", "depository", "checking"),
                  plaid_account("acc-acme", "Acme Checking", "2222", "depository", "checking")],
        pages={None: page([
            plaid_txn("out", 1200, "2026-03-02", "ZELLE TO ME", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("in", -1200, "2026-03-02", "ZELLE FROM ME", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
            plaid_txn("out2", 1200, "2026-03-04", "ZELLE TO ME", account="acc-tartan", pfc="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
            plaid_txn("in2", -1200, "2026-03-04", "ZELLE FROM ME", account="acc-acme", pfc="TRANSFER_IN_TRANSFER_IN_FROM_APPS"),
        ], "c1")},
    )
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    by_id = rows(conn)
    ids = {k: r["id"] for k, r in by_id.items()}
    # Same amounts two days apart: each side links to the one on its own day, and both count as reviewed.
    assert (by_id["out"]["transfer_id"], by_id["in"]["transfer_id"]) == (ids["in"], ids["out"])
    assert (by_id["out2"]["transfer_id"], by_id["in2"]["transfer_id"]) == (ids["in2"], ids["out2"])
    assert all(r["type"] == "transfer" and r["reviewed"] == 1 for r in by_id.values())

    # Transfers that came in another way (an import, or by hand) link too, on different accounts only.
    tartan, acme = (conn.execute("SELECT id FROM accounts WHERE name = ?", (n,)).fetchone()[0] for n in ("Tartan Checking", "Acme Checking"))
    add = lambda account, day, amount: conn.execute(
        "INSERT INTO transactions (account_id, date, amount, name, type, source) VALUES (?, ?, ?, 'Move', 'transfer', 'copilot')",
        (account, day, amount)).lastrowid
    a, b, same = add(tartan, "2026-07-01", -500), add(acme, "2026-07-02", 500), add(tartan, "2026-07-01", 500)
    assert sync.link_transfers(conn) == 1
    linked = dict(conn.execute("SELECT id, transfer_id FROM transactions WHERE id IN (?, ?, ?)", (a, b, same)).fetchall())
    assert linked == {a: b, b: a, same: None}


def test_a_posted_transaction_keeps_the_day_it_was_made(conn):
    purchase_dated = plaid_txn("t2", 30.0, "2026-10-12", "SHOP", pending_id="p2")
    purchase_dated["authorized_date"] = "2026-10-08"
    fake = FakePlaid(pages={
        None: page([plaid_txn("p1", 12.0, "2026-10-08", "BLUE BOTTLE", pending=True),
                    plaid_txn("p2", 30.0, "2026-10-09", "SHOP", pending=True),
                    plaid_txn("p3", 50.0, "2026-10-10", "TRANSFER OUT", pending=True),
                    plaid_txn("p4", 25.0, "2026-10-10", "ZELLE PAYMENT", pending=True)], "c1"),
        "c1": page([plaid_txn("t1", 12.0, "2026-10-11", "BLUE BOTTLE", pending_id="p1"), purchase_dated,
                    plaid_txn("t3", 50.0, "2026-10-09", "TRANSFER OUT", pending_id="p3"),
                    plaid_txn("t4", 25.0, "2026-10-09", "ZELLE PAYMENT", pending_id="p4")], "c2", removed=["p1", "p2", "p3", "p4"]),
    })
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    with conn:
        conn.execute("UPDATE transactions SET name = 'Rent share', user_edited = 1 WHERE plaid_transaction_id = 'p4'")
    sync.sync_item(conn, fake, "item-1")
    by_id = rows(conn)
    assert (by_id["t1"]["date"], by_id["t1"]["pending"]) == ("2026-10-08", 0)  # no purchase date from the bank: the pending day stays
    assert by_id["t2"]["date"] == "2026-10-08"  # the bank's purchase date wins over both
    # Some banks date a transfer's posting a day before it showed as pending: the earlier day wins, edited or not.
    assert by_id["t3"]["date"] == "2026-10-09"
    assert (by_id["t4"]["date"], by_id["t4"]["name"]) == ("2026-10-09", "Rent share")


def test_a_purchase_time_dates_a_transaction_in_the_app_time_zone(conn, pacific):
    evening = plaid_txn("t1", 20.0, "2026-10-10", "DINNER")
    evening.update(authorized_date="2026-10-09", authorized_datetime="2026-10-09T01:30:00Z")  # 6:30 pm on Oct 8, Pacific time
    midnight = plaid_txn("t2", 5.0, "2026-10-10", "PARKING")
    midnight.update(authorized_date="2026-10-09", authorized_datetime="2026-10-09T00:00:00Z")  # a bank's default time
    fake = FakePlaid(pages={None: page([evening, midnight], "c1")})
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    by_id = rows(conn)
    assert (by_id["t1"]["date"], by_id["t2"]["date"]) == ("2026-10-08", "2026-10-09")


def test_what_else_the_bank_says_is_kept_and_kept_current(conn):
    detailed = plaid_txn("t1", 23.45, "2026-10-09", "SQ *CORNER BAKERY 123", merchant="Corner Bakery", pfc="FOOD_AND_DRINK_RESTAURANT")
    detailed.update(authorized_date="2026-10-07", authorized_datetime="2026-10-07T18:22:00Z", payment_channel="in store",
                    transaction_code="purchase", website="cornerbakery.example", payment_meta={"payment_processor": "Square"},
                    location={"address": "1 Main St", "city": "Lakeside", "region": "ZZ", "postal_code": "00000", "store_number": "42"},
                    counterparties=[{"name": "Corner Bakery", "type": "merchant"}, {"name": "Square", "type": "payment_terminal"}])
    changed = dict(detailed, transaction_code="adjustment")
    fake = FakePlaid(pages={None: page([detailed, plaid_txn("t2", 5, "2026-10-09", "FEE")], "c1"), "c1": page([], "c2", modified=[changed])})
    link(conn, fake)
    sync.sync_item(conn, fake, "item-1")
    details = json.loads(rows(conn)["t1"]["plaid_details"])
    assert details == {"posted": "2026-10-09", "made": "2026-10-07", "time": "2026-10-07T18:22:00Z", "bank_name": "SQ *CORNER BAKERY 123",
                       "merchant": "Corner Bakery", "website": "cornerbakery.example", "channel": "in store", "code": "purchase",
                       "where": "1 Main St, Lakeside, ZZ 00000", "store": "42", "processor": "Square", "confidence": "VERY_HIGH",
                       "counterparties": [{"name": "Corner Bakery", "type": "merchant"}, {"name": "Square", "type": "payment_terminal"}]}
    assert json.loads(rows(conn)["t2"]["plaid_details"]) == {"posted": "2026-10-09", "bank_name": "FEE"}  # only what the bank filled in
    sync.sync_item(conn, fake, "item-1")
    assert json.loads(rows(conn)["t1"]["plaid_details"])["code"] == "adjustment"  # the bank's later word replaces it
