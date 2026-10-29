import importlib.util
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reports
from app.db import connect
from app.main import create_app

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_data.py"


def demo_data():
    spec = importlib.util.spec_from_file_location("demo_data", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_demo_data_builds_and_every_page_shows_it(settings):
    path = demo_data().build(settings.data_dir, today=date(2026, 10, 14))  # the day the tests fix as today
    conn = connect(path)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] > 100
    assert reports.month_totals(conn, "2026-10")["spent"] > 0
    conn.close()
    with TestClient(create_app(settings)) as client:
        for page in ("/", "/transactions", "/reports", "/reports?group=merchant&by=quarter", "/everyday", "/categories", "/recurring",
                     "/accounts", "/rules", "/export.csv"):
            assert client.get(page).status_code == 200, page


def test_the_demo_data_never_touches_an_existing_database(settings):
    demo = demo_data()
    demo.build(settings.data_dir, today=date(2026, 10, 14))
    with pytest.raises(SystemExit, match="already exists"):
        demo.build(settings.data_dir)
