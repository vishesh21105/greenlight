import pytest

from app import config, db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite file and workspace folder."""
    monkeypatch.setenv("GREENLIGHT_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("GREENLIGHT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("START_WORKER", "0")
    monkeypatch.setenv("FLAKY_RERUNS", "3")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    for key in ("LLM_API_KEY", "LLM_MODEL", "DASHBOARD_PASSWORD", "ADMIN_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    config.reload_settings()
    db.reset_engine()
    db.init_db()
    yield
    db.reset_engine()
