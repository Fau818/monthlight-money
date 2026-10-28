from datetime import date

import pytest

from app import copilot_import
from tests.conftest import COPILOT_CSV


def test_parse_reads_rows_and_detects_spending_as_positive():
    parsed = copilot_import.parse(COPILOT_CSV)
    assert len(parsed.rows) == 11
    assert parsed.sign == -1  # income is negative in the file, so spending is positive
    assert parsed.rows[0].mask == "1234"
    assert parsed.rows[6].type == "transfer"
    assert parsed.rows[-1].pending


def test_parse_accepts_other_header_spellings_and_formats():
    text = "Date,Name,Amount,Account_Name,Category\n09/01/2026,Cafe,\"$1,234.50\",Card,Food\n2026-09-02,Shop,(12.00),Card,\n"
    parsed = copilot_import.parse("﻿" + text)
    assert [r.date for r in parsed.rows] == [date(2026, 9, 1), date(2026, 9, 2)]
    assert [r.amount for r in parsed.rows] == [1234.5, -12.0]
    assert parsed.rows[0].account == "Card"


def test_parse_reports_missing_columns():
    with pytest.raises(copilot_import.CopilotImportError, match="amount"):
        copilot_import.parse("date,name\n2026-01-01,x\n")


def test_unreadable_lines_are_counted_not_fatal():
    parsed = copilot_import.parse("date,name,amount\nnot-a-date,x,1\n2026-01-01,y,2\n")
    assert len(parsed.rows) == 1 and parsed.unreadable == 1


def test_import_creates_accounts_categories_rules_and_recurring(conn):
    result = copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    assert (result.added, result.pending_skipped, result.accounts_created) == (10, 1, 2)
    assert result.categories_created == 6  # Food, Groceries, Subscriptions, Paycheck, Shopping, Gas
    groceries = conn.execute(
        "SELECT p.name FROM categories c JOIN categories p ON p.id = c.parent_id WHERE c.name = 'Groceries'"
    ).fetchone()
    assert groceries["name"] == "Food"

    amounts = dict(conn.execute("SELECT name, amount FROM transactions WHERE name IN ('Acme Payroll', 'Amazon', 'Shell')").fetchall())
    assert amounts == {"Acme Payroll": 2500.0, "Amazon": 20.0, "Shell": -40.0}
    assert conn.execute("SELECT notes FROM transactions WHERE name = 'Amazon'").fetchone()[0] == "Returned shoes"
    assert conn.execute("SELECT type FROM transactions WHERE name = 'Chase Payment'").fetchone()[0] == "transfer"

    rules = {r["pattern"] for r in conn.execute("SELECT pattern FROM rules")}
    assert rules == {"trader joe's", "netflix"}  # merchants seen at least twice with one category

    netflix = conn.execute("SELECT * FROM recurring WHERE name = 'Netflix'").fetchone()
    assert (netflix["frequency"], netflix["amount"], netflix["pattern"]) == ("monthly", -15.49, "netflix")
    assert conn.execute("SELECT count(*) FROM transactions WHERE recurring_id = ?", (netflix["id"],)).fetchone()[0] == 3

    # Plaid categories get mapped onto the imported categories by name.
    mapped = dict(conn.execute("SELECT plaid_category, c.name FROM plaid_category_map m JOIN categories c ON c.id = m.category_id").fetchall())
    assert mapped["FOOD_AND_DRINK_GROCERIES"] == "Groceries"
    assert mapped["TRANSPORTATION_GAS"] == "Gas"


def test_reimporting_the_same_file_adds_nothing(conn):
    copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    again = copilot_import.import_parsed(conn, copilot_import.parse(COPILOT_CSV))
    assert (again.added, again.duplicates, again.accounts_created, again.rules_created, again.recurring_created) == (0, 10, 0, 0, 0)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 10


def test_identical_rows_in_one_file_are_kept_separately(conn):
    text = "date,name,amount,account\n2026-09-01,Coffee,4.00,Card\n2026-09-01,Coffee,4.00,Card\n2026-09-02,Lunch,-4.00,Card\n"
    result = copilot_import.import_parsed(conn, copilot_import.parse(text))
    assert result.added == 3


def test_summarize_describes_the_file():
    s = copilot_import.summarize(copilot_import.parse(COPILOT_CSV))
    assert (s["posted"], s["pending"], s["start"], s["end"]) == (10, 1, date(2026, 7, 3), date(2026, 10, 5))
    assert s["spending_positive"] is True
    assert {a["name"] for a in s["accounts"]} == {"Chase Sapphire", "Acme Checking"}


def test_importing_again_after_renaming_brings_back_no_account_or_category(client, settings):
    from app import sync
    from tests.test_routes import import_sample
    import_sample(client, settings)
    conn = sync.connect(settings.db_path)
    with conn:
        conn.execute("UPDATE accounts SET name = 'My card' WHERE name = 'Chase Sapphire'")
        conn.execute("UPDATE categories SET name = 'Food shopping' WHERE name = 'Groceries'")
    counts = lambda: tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("accounts", "categories", "transactions"))
    before = counts()
    response = client.post("/import/copilot", data={"file": "copilot.csv"})
    assert response.status_code == 200 and counts() == before
    conn.close()
