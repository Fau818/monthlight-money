import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import main, recurring, reports, sync
from app.config import Settings
from app.db import connect
from app.main import create_app
from app.plaid_client import PlaidError

COPILOT_CSV = """date,name,amount,status,category,parent category,excluded,tags,type,account,account mask,note,recurring
2026-09-01,Trader Joe's,54.20,posted,Groceries,Food,false,,regular,Chase Sapphire,1234,,
2026-09-08,Trader Joe's,61.10,posted,Groceries,Food,false,,regular,Chase Sapphire,1234,,
2026-07-03,Netflix,15.49,posted,Subscriptions,,false,,regular,Chase Sapphire,1234,,Netflix
2026-08-03,Netflix,15.49,posted,Subscriptions,,false,,regular,Chase Sapphire,1234,,Netflix
2026-09-03,Netflix,15.49,posted,Subscriptions,,false,,regular,Chase Sapphire,1234,,Netflix
2026-09-15,Acme Payroll,-2500.00,posted,Paycheck,,false,,income,Acme Checking,9876,,
2026-09-20,Chase Payment,500.00,posted,,,false,,internal transfer,Acme Checking,9876,,
2026-09-20,Payment Thank You,-500.00,posted,,,false,,internal transfer,Chase Sapphire,1234,,
2026-09-21,Amazon,-20.00,posted,Shopping,,false,,regular,Chase Sapphire,1234,Returned shoes,
2026-10-03,Shell,40.00,posted,Gas,,false,,regular,Chase Sapphire,1234,,
2026-10-05,Blue Bottle,6.50,pending,Coffee,Food,false,,regular,Chase Sapphire,1234,,
"""


def plaid_txn(tid, amount, day, name, *, merchant=None, pfc=None, confidence="VERY_HIGH", pending=False, pending_id=None,
              account="acc-card"):
    """Returns a transaction shaped like Plaid's /transactions/sync output."""
    return {
        "transaction_id": tid, "account_id": account, "amount": amount, "date": day, "authorized_date": None,
        "name": name, "merchant_name": merchant, "pending": pending, "pending_transaction_id": pending_id,
        "personal_finance_category": {"primary": pfc.split("_")[0], "detailed": pfc, "confidence_level": confidence} if pfc else None,
        "logo_url": None,
    }


def plaid_account(account_id, name, mask, type_="credit", subtype="credit card", current=100.0):
    return {"account_id": account_id, "name": name, "mask": mask, "type": type_, "subtype": subtype,
            "balances": {"current": current, "available": None, "limit": 5000, "iso_currency_code": "USD"}}


class FakePlaid:
    """Stands in for PlaidClient; `pages` maps a cursor to the /transactions/sync page returned for it."""

    env = "sandbox"

    def __init__(self, accounts=None, pages=None):
        self.accounts = accounts or [plaid_account("acc-card", "Sapphire Preferred", "1234")]
        self.pages = pages or {None: page([], "c1")}
        self.cursors = []
        self.errors = {}  # cursor, "accounts" or "exchange" -> PlaidError raised once
        self.removed = False
        self.exchanges = 0

    def exchange_public_token(self, public_token):
        if "exchange" in self.errors:
            raise self.errors.pop("exchange")
        self.exchanges += 1
        return {"access_token": f"access-sandbox-{self.exchanges}", "item_id": f"item-{self.exchanges}"}

    def get_accounts(self, access_token):
        if "accounts" in self.errors:
            raise self.errors.pop("accounts")
        return {"accounts": self.accounts, "item": {"institution_id": "ins_1", "consent_expiration_time": None}}

    def transactions_sync(self, access_token, cursor, count=500):
        self.cursors.append(cursor)
        if cursor in self.errors:
            raise self.errors.pop(cursor)
        return self.pages[cursor]

    def remove_item(self, access_token):
        self.removed = True

    def create_link_token(self, access_token=None):
        return "link-sandbox-token"


def page(added, next_cursor, modified=(), removed=(), has_more=False, status="HISTORICAL_UPDATE_COMPLETE"):
    return {"added": list(added), "modified": list(modified), "removed": [{"transaction_id": r} for r in removed],
            "next_cursor": next_cursor, "has_more": has_more, "transactions_update_status": status}


def mutation_error():
    return PlaidError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "changed during pagination")


class FixedDate(date):
    """A date whose today() is always the same day, so the dates pages show ("Tue, Sep 8", with the year only when it
    isn't this year's) don't depend on when the tests run."""

    @classmethod
    def today(cls):
        return cls(2026, 10, 14)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    for module in (main, recurring, reports, sync):
        monkeypatch.setattr(module, "date", FixedDate)


@pytest.fixture
def pacific(monkeypatch):
    """Runs a test with the app's time zone set to US Pacific time, the way TZ sets it, and puts the zone back after."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data", import_dir=tmp_path / "import", backup_dir=tmp_path / "backups",
        plaid_client_id="id", plaid_secret="secret",
        plaid_env="sandbox", sync_interval_hours=0, allowed_hosts=["testserver"],
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c
