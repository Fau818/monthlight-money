import json

import httpx
import pytest

from app.plaid_client import PlaidClient, PlaidError


def recording_client(response=None, status=200):
    """Returns a PlaidClient whose requests are recorded in a list instead of sent, and that list."""
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(status, json=response or {"link_token": "link-sandbox-x", "added": [], "modified": [],
                                                         "removed": [], "next_cursor": "c1", "has_more": False})

    return PlaidClient("id", "secret", "sandbox", transport=httpx.MockTransport(handle)), sent


def test_new_connections_ask_consent_for_later_products():
    client, sent = recording_client()
    client.create_link_token()
    body = json.loads(sent[0].content)
    assert body["products"] == ["transactions"]
    assert body["additional_consented_products"] == ["investments", "liabilities"]
    assert body["transactions"] == {"days_requested": 730}
    assert sent[0].headers["Plaid-Version"] == "2020-09-14"


def test_update_mode_keeps_the_item_and_lets_accounts_change():
    client, sent = recording_client()
    client.create_link_token("access-sandbox-1")
    body = json.loads(sent[0].content)
    assert body["access_token"] == "access-sandbox-1"
    assert "products" not in body and "additional_consented_products" not in body
    assert body["update"] == {"account_selection_enabled": True}


def test_sync_asks_for_raw_descriptions_and_v2_categories():
    client, sent = recording_client()
    client.transactions_sync("access-sandbox-1", None)
    client.transactions_sync("access-sandbox-1", "c1")
    first, second = (json.loads(r.content) for r in sent)
    assert first["options"] == {"include_original_description": True, "personal_finance_category_version": "v2"}
    assert first["count"] == 500 and "cursor" not in first and second["cursor"] == "c1"


def test_errors_keep_plaids_details():
    client, _ = recording_client({"error_type": "ITEM_ERROR", "error_code": "ITEM_NOT_FOUND", "error_message": "not found",
                                  "display_message": None, "request_id": "req-123"}, status=400)
    with pytest.raises(PlaidError) as caught:
        client.get_accounts("access-sandbox-1")
    error = caught.value
    assert (error.code, error.error_type, error.request_id) == ("ITEM_NOT_FOUND", "ITEM_ERROR", "req-123")
    assert "req-123" in str(error)  # so every log line about it carries the id Plaid support asks for
    assert "connect the bank again" in error.friendly  # Plaid sent no display_message, so ours is used
    assert PlaidError("SOMETHING_NEW", "x").friendly == "Plaid returned an error (SOMETHING_NEW)."
    assert PlaidError("ITEM_LOGIN_REQUIRED", "x", "Log in to Tartan again.").friendly == "Log in to Tartan again."
