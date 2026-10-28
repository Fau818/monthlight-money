"""Minimal Plaid API client covering the endpoints this app uses."""

import httpx

BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
# Pinned so a change to the Dashboard's default API version can't change response shapes under us.
API_VERSION = "2020-09-14"
# Plaid caps this at 730 days; banks often have less.
DAYS_REQUESTED = 730
# Permission asked for at bank login so these can be added later without logging in again.
# Plaid fetches nothing for them and bills nothing until their endpoints are called, which this app doesn't do yet.
CONSENT_FOR_LATER = ["investments", "liabilities"]
SANDBOX_INSTITUTION = "ins_109508"  # First Platypus Bank, a Plaid test institution

# What to tell the user when Plaid sends no display_message of its own.
FRIENDLY_MESSAGES = {
    "ITEM_LOGIN_REQUIRED": "The bank needs you to log in again.",
    "INSTITUTION_DOWN": "The bank isn't responding right now. The app will try again later.",
    "INSTITUTION_NOT_RESPONDING": "The bank isn't responding right now. The app will try again later.",
    "RATE_LIMIT_EXCEEDED": "Plaid asked the app to slow down. It will try again later.",
    "ITEM_NOT_FOUND": "This connection no longer exists at Plaid. Disconnect it on Accounts (its history is kept), then connect the bank again.",
    "NETWORK_ERROR": "Couldn't reach Plaid. Check the internet connection.",
}


class PlaidError(Exception):
    """An error returned by the Plaid API, or a failure to reach it."""

    def __init__(self, code: str, message: str, display_message: str | None = None,
                 error_type: str | None = None, request_id: str | None = None):
        # request_id is what Plaid support asks for, so it goes into every log line about this error.
        super().__init__(f"{code}: {message}" + (f" (request_id {request_id})" if request_id else ""))
        self.code = code
        self.message = message
        self.display_message = display_message
        self.error_type = error_type
        self.request_id = request_id

    @property
    def friendly(self) -> str:
        """Returns a message for the user: Plaid's own when it sends one, otherwise ours."""
        return self.display_message or FRIENDLY_MESSAGES.get(self.code) or f"Plaid returned an error ({self.code})."


class PlaidClient:
    def __init__(self, client_id: str, secret: str, env: str, transport: httpx.BaseTransport | None = None):
        self.env = env
        self._http = httpx.Client(
            base_url=BASE_URLS[env],
            headers={"PLAID-CLIENT-ID": client_id, "PLAID-SECRET": secret, "Plaid-Version": API_VERSION},
            timeout=60,
            transport=transport,
        )

    def _post(self, path: str, body: dict) -> dict:
        try:
            response = self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise PlaidError("NETWORK_ERROR", f"Couldn't reach Plaid ({exc.__class__.__name__})") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or data.get("error_code"):
            raise PlaidError(
                data.get("error_code") or f"HTTP_{response.status_code}",
                data.get("error_message") or response.text[:200],
                data.get("display_message"),
                data.get("error_type"),
                data.get("request_id"),
            )
        return data

    def create_link_token(self, access_token: str | None = None) -> str:
        """Returns a Link token for connecting a new bank, or, when `access_token` is given, for logging in again to
        an existing connection and changing which of its accounts are shared."""
        body: dict = {
            "client_name": "Monthlight",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": "local-user"},
        }
        if access_token:
            # Update mode keeps the same Item, so it doesn't use up a Trial plan slot.
            body["access_token"] = access_token
            body["update"] = {"account_selection_enabled": True}
        else:
            body["products"] = ["transactions"]
            body["additional_consented_products"] = CONSENT_FOR_LATER
            body["transactions"] = {"days_requested": DAYS_REQUESTED}
        return self._post("/link/token/create", body)["link_token"]

    def exchange_public_token(self, public_token: str) -> dict:
        """Returns {"access_token", "item_id"} for a public token from Link."""
        data = self._post("/item/public_token/exchange", {"public_token": public_token})
        return {"access_token": data["access_token"], "item_id": data["item_id"]}

    def get_accounts(self, access_token: str) -> dict:
        """Returns Plaid's /accounts/get response: the Item's accounts with balances, and the Item."""
        return self._post("/accounts/get", {"access_token": access_token})

    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500) -> dict:
        """Returns one page of transaction updates after `cursor` (all history when it's None)."""
        body: dict = {
            "access_token": access_token,
            "count": count,
            # The raw bank text helps rules match; v2 is the category list the app's mapping is written against.
            "options": {"include_original_description": True, "personal_finance_category_version": "v2"},
        }
        if cursor:
            body["cursor"] = cursor
        return self._post("/transactions/sync", body)

    def remove_item(self, access_token: str) -> None:
        """Disconnects the Item at Plaid. Plaid doesn't give back the Trial plan slot it used."""
        self._post("/item/remove", {"access_token": access_token})

    def sandbox_public_token(self, institution_id: str = SANDBOX_INSTITUTION, username: str | None = None) -> str:
        """Returns a public token for a fake Sandbox bank, skipping the Link screens. `username` picks one of
        Plaid's test users, such as user_transactions_dynamic; the default is user_good."""
        options: dict = {"transactions": {"days_requested": DAYS_REQUESTED}}
        if username:
            options.update(override_username=username, override_password="pass_good")
        data = self._post(
            "/sandbox/public_token/create",
            {"institution_id": institution_id, "initial_products": ["transactions"], "options": options},
        )
        return data["public_token"]
