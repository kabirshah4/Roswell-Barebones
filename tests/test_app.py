from fastapi.testclient import TestClient

from backend.config import Config
from backend.main import create_app


def make_client(db_path, client=None):
    cfg = Config(db_path=db_path)
    app = create_app(cfg=cfg, client=client, start_poller=False)
    return TestClient(app)


def test_health_reports_poller_state(db_path):
    with make_client(db_path) as c:
        body = c.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["last_success_at"] is None
    assert body["consecutive_failures"] == 0
    assert "interval_seconds" in body


def test_app_creates_schema_on_startup(tmp_path):
    from backend.db import database

    db = tmp_path / "fresh.db"
    app = create_app(cfg=Config(db_path=db), start_poller=False)
    with TestClient(app):
        pass
    with database.get_conn(db) as conn:
        assert database.list_watchlist(conn) == []


def test_poller_does_not_start_when_disabled(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app):
        assert app.state.poller_task is None
