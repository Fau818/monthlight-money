"""Runtime settings, read from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAID_ENVS = ("sandbox", "production")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    import_dir: Path = ROOT / "import"
    backup_dir: Path = ROOT / "backups"  # daily copies of the database
    plaid_client_id: str = ""
    plaid_secret: str = ""  # secret for plaid_env only
    plaid_env: str = "sandbox"
    sync_interval_hours: float = 6.0  # 0 turns off background syncing
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    # Names your other devices reach this computer by, through a private network (docs/remote-access.md): one ending in
    # .ts.net, say, or *.sgponte. Plaid Link can't start there.
    remote_hosts: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "money.db"

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)


def load_settings() -> Settings:
    """Returns settings built from the environment; exits with a message if PLAID_ENV is invalid."""
    env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    if env not in PLAID_ENVS:
        raise SystemExit(f"PLAID_ENV must be 'sandbox' or 'production', not {env!r}")
    secret_var = "PLAID_PRODUCTION_SECRET" if env == "production" else "PLAID_SANDBOX_SECRET"
    hosts = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1")
    remote = os.environ.get("REMOTE_HOSTS", "")
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", ROOT / "data")),
        import_dir=Path(os.environ.get("IMPORT_DIR", ROOT / "import")),
        backup_dir=Path(os.environ.get("BACKUP_DIR", ROOT / "backups")),
        plaid_client_id=os.environ.get("PLAID_CLIENT_ID", "").strip(),
        plaid_secret=os.environ.get(secret_var, "").strip(),
        plaid_env=env,
        sync_interval_hours=float(os.environ.get("SYNC_INTERVAL_HOURS", "6")),
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        remote_hosts=[h.strip() for h in remote.split(",") if h.strip()],
    )
