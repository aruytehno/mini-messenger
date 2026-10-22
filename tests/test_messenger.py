"""Behavior tests; set TEST_DATABASE_URL for a dedicated PostgreSQL test database.

The configured test database is cleared between tests. Never point it at real data.
"""
import os
from datetime import timedelta
from uuid import uuid4

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app import main
from app.db import Base, Message, Session, SessionLocal, now

ORIGIN = {"origin": "http://localhost:8000"}


@pytest.fixture
def clients(monkeypatch):
    url = os.environ["DATABASE_URL"]
    if url == "sqlite://":
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url)
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    main.attempts.clear()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    with TestClient(main.app, headers=ORIGIN) as alice, TestClient(main.app, headers=ORIGIN) as bob, TestClient(main.app, headers=ORIGIN) as eve:
        for client, login in ((alice, "alice"), (bob, "bob"), (eve, "eve")):
            result = client.post("/api/register", json={"login": login, "password": "test-password"})
            assert result.status_code == 201
        yield alice, bob, eve, factory
    engine.dispose()


def dialog(alice):
    return alice.post("/api/dialogs", json={"login": "bob"}).json()["id"]


def send(client, dialog_id, text="Привет", client_id=None):
    return client.post(f"/api/dialogs/{dialog_id}/messages", json={"text": text, "client_id": client_id or str(uuid4())})


def test_authentication_and_origin(clients):
    alice, bob, eve, factory = clients
    assert alice.get("/api/me").json()["login"] == "alice"
    assert alice.post("/api/register", json={"login": "ALICE", "password": "test-password"}).status_code == 409
    assert alice.post("/api/login", json={"login": "alice", "password": "wrong-password"}).status_code == 401
    assert alice.post("/api/logout", headers={"origin": "https://evil.example"}).status_code == 403
    assert alice.post("/api/logout").status_code == 200
    assert alice.get("/api/me").status_code == 401
    assert alice.post("/api/login", json={"login": "ALICE", "password": "test-password"}).status_code == 200


def test_private_history_and_idempotency(clients):
    alice, bob, eve, factory = clients
    ident = dialog(alice)
    assert dialog(alice) == ident
    key = str(uuid4())
    first = send(alice, ident, client_id=key)
    assert first.status_code == 200
    assert send(alice, ident, client_id=key).json()["id"] == first.json()["id"]
    assert send(alice, ident, text="Другой текст", client_id=key).status_code == 409
    assert bob.get(f"/api/dialogs/{ident}/messages").json()[0]["text"] == "Привет"
    assert eve.get(f"/api/dialogs/{ident}/messages").status_code == 404
    assert send(eve, ident).status_code == 404
    assert send(alice, ident, text="   ").status_code == 422


def test_expiration_and_cleanup(clients):
    alice, bob, eve, factory = clients
    ident = dialog(alice)
    payload = send(alice, ident).json()
    from datetime import datetime
    assert datetime.fromisoformat(payload["expires_at"]) - datetime.fromisoformat(payload["created_at"]) == timedelta(hours=24)
    with factory.begin() as db:
        db.get(Message, payload["id"]).expires_at = now() - timedelta(seconds=1)
    assert bob.get(f"/api/dialogs/{ident}/messages").json() == []
    main.cleanup()
    with factory() as db:
        assert db.get(Message, payload["id"]) is None


def test_password_revokes_sessions(clients):
    alice, bob, eve, factory = clients
    old_cookie = alice.cookies.get(main.COOKIE)
    assert alice.post("/api/password", json={"current_password": "wrong", "new_password": "new-password"}).status_code == 400
    assert alice.post("/api/password", json={"current_password": "test-password", "new_password": "new-password"}).status_code == 200
    assert alice.get("/api/me").status_code == 200
    assert eve.get("/api/me", headers={"cookie": f"{main.COOKIE}={old_cookie}"}).status_code == 401
    alice.post("/api/logout")
    assert alice.post("/api/login", json={"login": "alice", "password": "test-password"}).status_code == 401
    assert alice.post("/api/login", json={"login": "alice", "password": "new-password"}).status_code == 200


def test_websocket_delivery(clients):
    alice, bob, eve, factory = clients
    ident = dialog(alice)
    with bob.websocket_connect("/ws", headers=ORIGIN) as ws:
        payload = send(alice, ident).json()
        event = ws.receive_json()
        assert event["type"] == "message"
        assert event["message"]["id"] == payload["id"]


def test_pagination(clients):
    alice, bob, eve, factory = clients
    ident = dialog(alice)
    uid = alice.get("/api/me").json()["id"]
    with factory.begin() as db:
        for i in range(105):
            db.add(Message(dialog_id=ident, sender_id=uid, client_id=str(uuid4()), text=str(i),
                           created_at=now(), expires_at=now() + timedelta(hours=24)))
    recent = alice.get(f"/api/dialogs/{ident}/messages").json()
    assert len(recent) == 100
    older = alice.get(f"/api/dialogs/{ident}/messages?before={recent[0]['id']}").json()
    assert len(older) == 5
    assert {m["id"] for m in recent}.isdisjoint(m["id"] for m in older)


def test_websocket_rejects_wrong_origin_and_expired_session(clients):
    alice, bob, eve, factory = clients
    with pytest.raises(WebSocketDisconnect):
        with alice.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass
    token = alice.cookies.get(main.COOKIE)
    with factory.begin() as db:
        db.get(Session, main.digest(token)).expires_at = now() - timedelta(seconds=1)
    assert alice.get("/api/me").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with alice.websocket_connect("/ws", headers=ORIGIN):
            pass


def test_initial_migration(tmp_path):
    import subprocess
    import sys
    from sqlalchemy import inspect
    path = tmp_path / "migration.db"
    url = "sqlite:///" + path.as_posix()
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {"users", "dialogs", "messages", "sessions", "alembic_version"}
    assert any(index["column_names"] == ["expires_at"] for index in inspector.get_indexes("messages"))
    engine.dispose()
