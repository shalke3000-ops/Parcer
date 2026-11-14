from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from leadhub.config import load_settings
from leadhub.db import Database
from leadhub.main import create_app
from leadhub.scoring import load_keywords

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path):
    base = load_settings(require_password=False)
    return replace(
        base,
        password=PASSWORD,
        secret_key="test-secret-key",
        db_path=tmp_path / "test.sqlite3",
        tg_bot_token="",
        tg_notify_chat_id="",
        min_score=5,
        nominatim_url="https://nominatim.test/search",
        overpass_url="https://overpass.test/api/interpreter",
        overpass_mirrors=("https://overpass-mirror.test/api/interpreter",),
    )


@pytest.fixture
def db(settings) -> Database:
    return Database(settings.db_path)


@pytest.fixture
def keywords():
    return load_keywords(ROOT / "keywords.json")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def authed(client) -> TestClient:
    response = client.post("/api/login", json={"password": PASSWORD}, headers={"X-LeadHub": "1"})
    assert response.status_code == 200
    client.headers.update({"X-LeadHub": "1"})
    return client
