from app import categorize


def add_categories(conn, *names):
    return {n: conn.execute("INSERT INTO categories (name) VALUES (?)", (n,)).lastrowid for n in names}


def test_plaid_types_and_primaries():
    assert categorize.plaid_primary("FOOD_AND_DRINK_COFFEE") == "FOOD_AND_DRINK"
    assert categorize.plaid_primary("TRANSFER_IN_DEPOSIT") == "TRANSFER_IN"
    assert categorize.plaid_primary(None) is None
    assert categorize.plaid_type("INCOME_SALARY") == "income"
    assert categorize.plaid_type("TRANSFER_OUT_SAVINGS") == "transfer"
    assert categorize.plaid_type("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT") == "transfer"
    assert categorize.plaid_type("LOAN_PAYMENTS_CAR_PAYMENT") == "regular"
    assert categorize.plaid_type(None) == "regular"
    # Zelle, Venmo and the like count as spending, the way Copilot counted them.
    assert categorize.plaid_type("TRANSFER_OUT_TRANSFER_OUT_FROM_APPS") == "regular"
    assert categorize.plaid_type("TRANSFER_IN_TRANSFER_IN_FROM_APPS") == "regular"
    assert categorize.plaid_type("TRANSFER_OUT_ACCOUNT_TRANSFER") == "transfer"


def test_auto_map_matches_whole_words(conn):
    ids = add_categories(conn, "Groceries", "Dining Out", "Car", "Credit Card Fees", "Taxes")
    categorize.auto_map_plaid_categories(conn)
    mapping = dict(conn.execute("SELECT plaid_category, category_id FROM plaid_category_map").fetchall())
    assert mapping["FOOD_AND_DRINK_GROCERIES"] == ids["Groceries"]
    assert mapping["FOOD_AND_DRINK"] == ids["Dining Out"]
    assert mapping["TRANSPORTATION_GAS"] == ids["Car"]  # "car" doesn't match "Card"
    assert mapping["BANK_FEES"] == ids["Credit Card Fees"]
    assert mapping["GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT"] == ids["Taxes"]


def test_auto_map_leaves_existing_mappings_alone(conn):
    ids = add_categories(conn, "Groceries", "Food")
    conn.execute("INSERT INTO plaid_category_map VALUES ('FOOD_AND_DRINK_GROCERIES', ?)", (ids["Food"],))
    categorize.auto_map_plaid_categories(conn)
    assert conn.execute("SELECT category_id FROM plaid_category_map WHERE plaid_category = 'FOOD_AND_DRINK_GROCERIES'").fetchone()[0] == ids["Food"]


def test_categorizer_prefers_exact_rules_then_longer_patterns(conn):
    ids = add_categories(conn, "Groceries", "Shopping", "Subscriptions")
    categorize.add_rule(conn, "Amazon", "contains", ids["Shopping"], None)
    categorize.add_rule(conn, "amazon prime", "contains", ids["Subscriptions"], None)
    categorize.add_rule(conn, "Amazon Fresh", "equals", ids["Groceries"], None)
    c = categorize.Categorizer(conn)
    assert c.categorize("Amazon Fresh", None, None)[0] == ids["Groceries"]
    assert c.categorize("AMAZON PRIME VIDEO", None, None)[0] == ids["Subscriptions"]
    assert c.categorize("Amazon.com", None, "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")[0] == ids["Shopping"]
    # A rule can match the bank's raw description too.
    assert c.categorize("Unknown", "AMZN Mktp amazon", None)[0] == ids["Shopping"]


def test_transfers_have_no_category_unless_a_rule_says_so(conn):
    ids = add_categories(conn, "Loans")
    categorize.auto_map_plaid_categories(conn)
    c = categorize.Categorizer(conn)
    assert c.categorize("Payment", None, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "VERY_HIGH") == (None, "transfer")
    assert c.categorize("Car loan", None, "LOAN_PAYMENTS_CAR_PAYMENT", "HIGH") == (ids["Loans"], "regular")


def test_unsure_plaid_categories_are_left_for_review(conn):
    ids = add_categories(conn, "Groceries")
    categorize.auto_map_plaid_categories(conn)
    c = categorize.Categorizer(conn)
    assert c.categorize("Market", None, "FOOD_AND_DRINK_GROCERIES", "VERY_HIGH") == (ids["Groceries"], "regular")
    for unsure in ("MEDIUM", "LOW", "UNKNOWN", None):
        assert c.categorize("Market", None, "FOOD_AND_DRINK_GROCERIES", unsure) == (None, "regular")
    # The type still follows Plaid, so an unsure card payment isn't counted as spending.
    assert c.categorize("Payment", None, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "LOW") == (None, "transfer")
    # A rule still applies.
    categorize.add_rule(conn, "market", "equals", ids["Groceries"], None)
    assert categorize.Categorizer(conn).categorize("Market", None, "FOOD_AND_DRINK_GROCERIES", "LOW") == (ids["Groceries"], "regular")


def test_apply_rule_skips_hand_edited_transactions(conn):
    ids = add_categories(conn, "Coffee", "Treats")
    account = conn.execute("INSERT INTO accounts (name) VALUES ('Card')").lastrowid
    for name, edited in (("Blue Bottle", 0), ("blue bottle", 0), ("Blue Bottle", 1), ("Blue Bottle Beans", 0)):
        conn.execute(
            "INSERT INTO transactions (account_id, date, amount, name, category_id, user_edited, source) VALUES (?, '2026-10-01', -5, ?, ?, ?, 'plaid')",
            (account, name, ids["Treats"] if edited else None, edited),
        )
    rule = categorize.add_rule(conn, "Blue Bottle", "equals", ids["Coffee"], None)
    assert categorize.apply_rule(conn, rule) == 2
    # Saving the same rule again updates it in place.
    assert categorize.add_rule(conn, "blue bottle", "equals", ids["Treats"], None) == rule
    assert conn.execute("SELECT count(*) FROM rules").fetchone()[0] == 1


def test_default_categories_only_when_empty(conn):
    assert categorize.ensure_default_categories(conn) is True
    assert categorize.ensure_default_categories(conn) is False
    assert conn.execute("SELECT count(*) FROM categories").fetchone()[0] == len(categorize.DEFAULT_CATEGORIES)
    # Every default should be reachable from at least one Plaid category, except ones set by rules.
    mapped = {r[0] for r in conn.execute("SELECT DISTINCT c.name FROM plaid_category_map m JOIN categories c ON c.id = m.category_id")}
    assert set(categorize.DEFAULT_CATEGORIES) - mapped == {"Subscriptions"}


def test_loan_accounts_hold_only_transfers(conn):
    ids = add_categories(conn, "Loans")
    categorize.auto_map_plaid_categories(conn)
    categorize.add_rule(conn, "monthly payment", "equals", ids["Loans"], "regular")
    c = categorize.Categorizer(conn)
    # The bank account side of a mortgage payment is spending; the loan account side never is, whatever the rules say.
    assert c.categorize("Monthly payment", None, "LOAN_PAYMENTS_MORTGAGE_PAYMENT", "HIGH", "depository") == (ids["Loans"], "regular")
    assert c.categorize("Monthly payment", None, "LOAN_PAYMENTS_MORTGAGE_PAYMENT", "HIGH", "loan") == (None, "transfer")
    assert c.categorize("Interest", None, "BANK_FEES_INTEREST_CHARGE", "HIGH", "loan") == (None, "transfer")

    checking = conn.execute("INSERT INTO accounts (name, type) VALUES ('Checking', 'depository')").lastrowid
    mortgage = conn.execute("INSERT INTO accounts (name, type) VALUES ('Mortgage', 'loan')").lastrowid
    for account, amount in ((checking, -549), (mortgage, 549)):
        conn.execute("INSERT INTO transactions (account_id, date, amount, name, type, source) VALUES (?, '2026-10-01', ?, 'Monthly payment', 'transfer', 'plaid')",
                     (account, amount))
    rule = conn.execute("SELECT id FROM rules").fetchone()[0]
    assert categorize.apply_rule(conn, rule) == 1
    assert conn.execute("SELECT type FROM transactions WHERE account_id = ?", (mortgage,)).fetchone()[0] == "transfer"


def test_income_and_transfers_get_no_category_and_app_transfers_only_from_rules(conn):
    ids = add_categories(conn, "Income", "Supplies", "Rent")
    categorize.auto_map_plaid_categories(conn)
    conn.execute("INSERT INTO plaid_category_map VALUES ('TRANSFER_OUT_TRANSFER_OUT_FROM_APPS', ?)", (ids["Supplies"],))
    categorize.add_rule(conn, "payroll", "contains", ids["Income"], "income")
    categorize.add_rule(conn, "zelle to landlord", "contains", ids["Rent"], None)
    c = categorize.Categorizer(conn)
    assert c.categorize("ACME PAYROLL", None, "INCOME_SALARY", "VERY_HIGH") == (None, "income")
    assert c.categorize("Payment", None, "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "VERY_HIGH") == (None, "transfer")
    # Zelle and the like count as spending, but Plaid's category map doesn't apply to them; a rule does.
    assert c.categorize("ZELLE TO SAM", None, "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "VERY_HIGH") == (None, "regular")
    assert c.categorize("ZELLE TO LANDLORD", None, "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "VERY_HIGH") == (ids["Rent"], "regular")


def test_categories_for_transfers_and_income_are_not_learned(conn):
    from app.main import learn_plaid_mapping
    ids = add_categories(conn, "Supplies")
    for code in ("TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "TRANSFER_IN_ACCOUNT_TRANSFER", "INCOME_SALARY", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"):
        assert learn_plaid_mapping(conn, code, ids["Supplies"]) is False
    assert learn_plaid_mapping(conn, "GENERAL_MERCHANDISE_OFFICE_SUPPLIES", ids["Supplies"]) is True
    assert [r[0] for r in conn.execute("SELECT plaid_category FROM plaid_category_map")] == ["GENERAL_MERCHANDISE_OFFICE_SUPPLIES"]
