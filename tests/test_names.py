import json
import sqlite3

import pytest

from app import categorize, names
from app.db import connect

# Made-up names in the shapes banks send them, and what they become.
CLEANED = [
    ("Online transfer to savings Conf# 1A2B3C4D", "Online transfer to savings"),
    ("Online Banking transfer to SAV 4321 Confirmation# XYZAB98765", "Online Banking transfer to SAV"),
    ("Mobile Banking payment to CRD 8765 Confirmation# a1bc2defg", "Mobile Banking payment to CRD"),
    ("Payment from CHK 1111 CONF#q9w8e7r6t", "Payment from CHK"),
    ("Zelle payment from Jamie Doe Conf# AB1CdE23f", "Zelle payment from Jamie Doe"),
    ("Zelle payment to Jamie Doe for pizza night Conf# a1b22cd3e", "Zelle payment to Jamie Doe"),
    ("Zelle payment to Jamie Doe JPM99ABC123", "Zelle payment to Jamie Doe"),
    ("Online Transfer to XXXXXX5678 Ref #IB0ABC1234", "Online Transfer"),
    ("ACH Debit Acme Utility PPD ID: 1234567890", "ACH Debit Acme Utility"),
    ("Transaction ID: 98765 Corner Bakery", "Corner Bakery"),
    ("Acme Store debit card credit *4321", "Acme Store debit card credit"),
    ("Payment to card ending in 1234", "Payment to card"),
    ("Transfer 123456789012", "Transfer"),
    ("Online transfer to SAV 1234 5678 Conf# 1A2B3C4D", "Online transfer to SAV"),
    ("  Corner   Bakery ", "Corner Bakery"),
]
# Names with nothing to take off, among them some that look a little like codes.
KEPT = ["24/7 Fitness", "7-Eleven", "Studio 54", "Conference Center", "Reference Books", "Corner Bakery #552", "Rock On",
        "Holiday Inn", "Zelle payment to For Real LLC", "PAYPAL *JAMIEDOE1", "Conf# 123"]


@pytest.mark.parametrize(("bank", "shown"), CLEANED)
def test_codes_and_account_numbers_come_off(bank, shown):
    assert names.clean(bank) == shown
    assert names.clean(shown) == shown  # a name already cleaned stays as it is, so rules made for it keep matching


@pytest.mark.parametrize("bank", KEPT)
def test_names_without_codes_stay_as_they_are(bank):
    assert names.clean(bank) == bank  # "Conf# 123" would have nothing left, so it stays too


def add(conn, name, *, source="plaid", bank=None, merchant=None, edited=0, renamed=0, day="2026-10-01"):
    """Adds a transaction named `name`, with what the bank calls it (`bank`, `merchant`) in its details."""
    account = conn.execute("SELECT id FROM accounts").fetchone() or [conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid]
    details = json.dumps({k: v for k, v in {"bank_name": bank, "merchant": merchant}.items() if v}) if bank or merchant else None
    return conn.execute("""INSERT INTO transactions (account_id, date, amount, name, source, plaid_details, user_edited, name_edited)
                           VALUES (?, ?, -5, ?, ?, ?, ?, ?)""", (account[0], day, name, source, details, edited, renamed)).lastrowid


def name_of(conn, tid):
    return conn.execute("SELECT name FROM transactions WHERE id = ?", (tid,)).fetchone()[0]


def test_renaming_the_others_and_the_ones_to_come(conn):
    renamed = add(conn, "Card payment", bank="Mobile Banking payment to CRD 8765 Confirmation# a1b2c3d4", edited=1, renamed=1)
    others = [add(conn, "Mobile Banking payment to CRD"), add(conn, "mobile banking payment to crd", edited=1, renamed=1)]
    unrelated = add(conn, "Mobile Banking payment to CRD extra")
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (renamed,)).fetchone()
    assert names.rename_offer(conn, t, "Card payment") is None  # not renamed after all
    offer = names.rename_offer(conn, t, "Mobile Banking payment to CRD")
    assert offer == {"id": renamed, "old": "Mobile Banking payment to CRD", "new": "Card payment", "others": 2}

    # Every other one with the old name, renamed by hand or not, as the user asked for them all.
    assert names.rename_all(conn, "Mobile Banking payment to CRD", "Card payment") == 2
    assert [name_of(conn, i) for i in (*others, unrelated)] == ["Card payment", "Card payment", "Mobile Banking payment to CRD extra"]
    assert categorize.Categorizer(conn).rename("MOBILE BANKING PAYMENT TO CRD") == "Card payment"
    # With nothing left to rename and the rule in place, there's nothing to offer.
    assert names.rename_offer(conn, t, "Mobile Banking payment to CRD") is None


def test_names_from_before_the_cleanup_count_as_the_same_name(conn):
    renamed = add(conn, "Savings", edited=1, renamed=1)
    others = [add(conn, "Online transfer to savings Conf# 9Z8Y7X6W"), add(conn, "Online transfer to savings")]
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (renamed,)).fetchone()
    offer = names.rename_offer(conn, t, "Online transfer to savings Conf# 1A2B3C4D")
    assert (offer["old"], offer["others"]) == ("Online transfer to savings", 2)  # the rule is for the name new ones get
    assert names.rename_all(conn, offer["old"], "Savings") == 2
    assert [name_of(conn, i) for i in others] == ["Savings", "Savings"]
    assert names.rename_offer(conn, t, "Savings Conf# 1A2B3C4D") is None  # its codes taken off by hand: the cleanup's job


def test_a_new_name_takes_over_the_rules_that_gave_the_old_one(conn):
    shopping = conn.execute("INSERT INTO categories (name) VALUES ('Shopping')").lastrowid
    categorize.add_rule(conn, "card payment", "equals", shopping, None)
    categorize.add_rename(conn, "Mobile Banking payment to CRD", "Card payment")
    categorize.add_rename(conn, "Card payment", "Credit card")
    c = categorize.Categorizer(conn)
    assert c.rename("Mobile Banking payment to CRD") == c.rename("Card payment") == "Credit card"
    # Renaming leaves what the rule already did to the category, and vice versa.
    assert c.categorize("Card payment", None, None)[0] == shopping
    categorize.add_rule(conn, "card payment", "equals", None, "transfer")
    assert categorize.Categorizer(conn).rename("Card payment") == "Credit card"


def test_earlier_names_suggest_rules_for_new_ones(conn):
    bank = "Mobile Banking payment to CRD 8765 Confirmation# "
    for code in ("a1b2c3d4", "e5f6g7h8", "i9j0k1l2"):
        add(conn, "Card payment", bank=bank + code, edited=1, renamed=1)
    add(conn, "Card payment", source="copilot", bank=bank + "m3n4o5p6")
    add(conn, "Visa bill", source="copilot", bank=bank + "q7r8s9t0")  # 4 in 5 is enough
    add(conn, "Rent", bank="Online transfer Conf# 1A2B3C4D", edited=1, renamed=1)  # just once: no rule
    add(conn, "Corner Bakery", source="copilot", merchant="Corner Bakery")  # already named the way it comes
    add(conn, "Corner Bakery", source="copilot", merchant="Corner Bakery")
    add(conn, "Mobile Banking payment to CRD", bank=bank + "u1v2w3x4")  # not renamed: it says nothing about the user's names
    assert names.learned(conn) == [{"pattern": "mobile banking payment to crd", "shown": "Mobile Banking payment to CRD",
                                    "name": "Card payment", "count": 4}]
    categorize.add_rename(conn, "Mobile Banking payment to CRD", "Card payment")
    assert names.learned(conn) == []  # a rule names them already


def test_cleaning_up_the_names_from_before(conn):
    categorize.add_rename(conn, "Corner Bakery", "Bakery")
    old = add(conn, "Online transfer to savings Conf# 1A2B3C4D", bank="Online transfer to savings Conf# 1A2B3C4D", day="2026-10-02")
    edited = add(conn, "Online transfer Conf# 1A2B3C4D", bank="Online transfer Conf# 1A2B3C4D", edited=1)  # only its category changed
    renamed = add(conn, "Savings", bank="Online transfer Conf# 9Z8Y7X6W", edited=1, renamed=1)
    merchant = add(conn, "CORNER BAKERY 123", bank="CORNER BAKERY 123", merchant="Corner Bakery")
    history = add(conn, "Rent Conf# 1A2B3C4D", source="copilot", bank="Rent Conf# 1A2B3C4D")  # Copilot's names stay
    clean = add(conn, "Coffee", bank="Coffee")
    assert names.cleanup(conn) == [
        {"id": old, "date": "2026-10-02", "name": "Online transfer to savings Conf# 1A2B3C4D", "new": "Online transfer to savings"},
        {"id": merchant, "date": "2026-10-01", "name": "CORNER BAKERY 123", "new": "Bakery"},  # the merchant's name, as a rule renames it
        {"id": edited, "date": "2026-10-01", "name": "Online transfer Conf# 1A2B3C4D", "new": "Online transfer"},
    ]
    assert names.clean_up(conn) == 3
    assert [name_of(conn, i) for i in (old, edited, renamed, merchant, history, clean)] == [
        "Online transfer to savings", "Online transfer", "Savings", "Bakery", "Rent Conf# 1A2B3C4D", "Coffee"]
    assert names.cleanup(conn) == []


def test_older_databases_learn_which_names_were_changed_by_hand(tmp_path):
    path = tmp_path / "old.db"
    connect(path).close()
    old = sqlite3.connect(path)
    old.execute("ALTER TABLE transactions DROP COLUMN name_edited")
    old.execute("ALTER TABLE rules DROP COLUMN rename_to")
    old.execute("INSERT INTO accounts (name) VALUES ('Card')")
    rows = [("Online transfer Conf# 1A2B3C4D", {"bank_name": "Online transfer Conf# 1A2B3C4D"}, 1),  # only the category changed
            ("Savings", {"bank_name": "Online transfer Conf# 9Z8Y7X6W"}, 1),
            ("Corner Bakery", {"bank_name": "CORNER BAKERY 123", "merchant": "Corner Bakery"}, 1),
            ("Groceries run", None, 1),  # nothing from the bank to tell
            ("Coffee", {"bank_name": "COFFEE"}, 0)]  # not edited, though not the bank's name: a rule's
    for name, details, edited in rows:
        old.execute("INSERT INTO transactions (account_id, date, amount, name, source, plaid_details, user_edited) "
                    "VALUES (1, '2026-10-01', -5, ?, 'plaid', ?, ?)", (name, json.dumps(details) if details else None, edited))
    old.commit()
    old.close()
    connect(path).close()  # opened and closed without a commit, as a page does
    conn = sqlite3.connect(path)
    assert [r[0] for r in conn.execute("SELECT name_edited FROM transactions ORDER BY id")] == [0, 1, 0, 1, 0]
    assert "rename_to" in {r[1] for r in conn.execute("PRAGMA table_info(rules)")}
    conn.close()
