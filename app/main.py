import asyncio
import hashlib
import logging
import os
import re
import secrets
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from time import monotonic
from threading import Lock
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.db import Dialog, Message, Session, SessionLocal, User, now

ORIGINS = {s.strip() for s in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",") if s.strip()}
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE = "messenger_session"
passwords = PasswordHash.recommended()
DUMMY_HASH = passwords.hash("dummy-password-for-timing")
connections: dict[int, set[WebSocket]] = defaultdict(set)
attempts: dict[tuple, deque] = {}
attempts_lock = Lock()
STATIC = Path(__file__).parent / "static"


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def limited(key, maximum, period=60):
    with attempts_lock:
        _limited(key, maximum, period)


def _limited(key, maximum, period):
    stamp = monotonic()
    # Remove inactive buckets so arbitrary client addresses cannot grow this forever.
    for old in list(attempts):
        if not attempts[old] or attempts[old][-1] <= stamp - period:
            del attempts[old]
    bucket = attempts.setdefault(key, deque())
    while bucket and bucket[0] <= stamp - period:
        bucket.popleft()
    if len(bucket) >= maximum:
        raise HTTPException(429, "Слишком много запросов. Попробуйте позже.")
    bucket.append(stamp)


def cleanup():
    with SessionLocal.begin() as db:
        db.execute(delete(Message).where(Message.expires_at <= now()))
        db.execute(delete(Session).where(Session.expires_at <= now()))


async def janitor():
    while True:
        await asyncio.sleep(60)
        try:
            await run_in_threadpool(cleanup)
        except Exception:
            logging.exception("History cleanup failed")


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(cleanup)
    task = asyncio.create_task(janitor())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="mini-messenger", lifespan=lifespan)


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("origin") not in ORIGINS:
            return Response("Origin forbidden", status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


def authenticated(token):
    if not token or len(token) > 200:
        raise HTTPException(401, "Войдите в аккаунт.")
    with SessionLocal() as db:
        session = db.get(Session, digest(token))
        if not session or session.expires_at <= now():
            raise HTTPException(401, "Сессия истекла. Войдите снова.")
        user = db.get(User, session.user_id)
        return {"id": user.id, "login": user.login}


def current_user(request: Request):
    return authenticated(request.cookies.get(COOKIE))


def member(db, dialog_id, user_id):
    dialog = db.get(Dialog, dialog_id)
    if not dialog or user_id not in (dialog.first_id, dialog.second_id):
        raise HTTPException(404, "Диалог не найден.")
    return dialog


class Credentials(BaseModel):
    login: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class NewDialog(BaseModel):
    login: str = Field(min_length=3, max_length=32)


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_id: UUID


def login_value(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", value):
        raise HTTPException(422, "Логин: 3–32 символа, латинские буквы, цифры и _.")
    return value


def set_session(db, user_id, response):
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=digest(token), user_id=user_id, expires_at=now() + timedelta(days=7)))
    response.set_cookie(COOKIE, token, max_age=7 * 86400, httponly=True,
                        secure=COOKIE_SECURE, samesite="strict", path="/")


@app.post("/api/register", status_code=201)
def register(data: Credentials, request: Request, response: Response):
    limited(("auth", request.client.host), 10)
    login = login_value(data.login)
    with SessionLocal.begin() as db:
        user = User(login=login, password_hash=passwords.hash(data.password))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            raise HTTPException(409, "Этот логин уже занят.")
        set_session(db, user.id, response)
        return {"id": user.id, "login": user.login}


@app.post("/api/login")
def login(data: Credentials, request: Request, response: Response):
    limited(("auth", request.client.host), 10)
    value = login_value(data.login)
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.login == value))
        valid = passwords.verify(data.password, user.password_hash if user else DUMMY_HASH)
        if not user or not valid:
            raise HTTPException(401, "Неверный логин или пароль.")
        # Replace this browser's previous session when switching accounts.
        token = request.cookies.get(COOKIE)
        if token:
            db.execute(delete(Session).where(Session.token_hash == digest(token)))
        set_session(db, user.id, response)
        return {"id": user.id, "login": user.login}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


async def disconnect_user(user_id):
    for ws in list(connections.get(user_id, ())):
        with suppress(Exception):
            await ws.close(code=4401)


@app.post("/api/logout")
async def logout(request: Request, response: Response, user=Depends(current_user)):
    def revoke():
        with SessionLocal.begin() as db:
            db.execute(delete(Session).where(Session.token_hash == digest(request.cookies[COOKIE])))
    await run_in_threadpool(revoke)
    response.delete_cookie(COOKIE, path="/")
    await disconnect_user(user["id"])
    return {"ok": True}


@app.post("/api/password")
async def change_password(data: PasswordChange, response: Response, user=Depends(current_user)):
    limited(("password", user["id"]), 5)
    def change():
        with SessionLocal.begin() as db:
            row = db.scalar(select(User).where(User.id == user["id"]).with_for_update())
            if not passwords.verify(data.current_password, row.password_hash):
                raise HTTPException(400, "Текущий пароль неверен.")
            row.password_hash = passwords.hash(data.new_password)
            db.execute(delete(Session).where(Session.user_id == row.id))
            set_session(db, row.id, response)
    await run_in_threadpool(change)
    await disconnect_user(user["id"])
    return {"ok": True}


@app.get("/api/dialogs")
def dialogs(user=Depends(current_user)):
    with SessionLocal() as db:
        rows = db.scalars(select(Dialog).where(or_(
            Dialog.first_id == user["id"], Dialog.second_id == user["id"]
        )).order_by(Dialog.id.desc())).all()
        return [{"id": d.id, "login": db.get(User, d.second_id if d.first_id == user["id"] else d.first_id).login}
                for d in rows]


@app.post("/api/dialogs")
def create_dialog(data: NewDialog, user=Depends(current_user)):
    limited(("dialogs", user["id"]), 30)
    with SessionLocal() as db:
        other = db.scalar(select(User).where(User.login == login_value(data.login)))
        if not other:
            raise HTTPException(404, "Пользователь не найден.")
        if other.id == user["id"]:
            raise HTTPException(400, "Укажите логин другого пользователя.")
        first, second = sorted((user["id"], other.id))
        query = select(Dialog).where(Dialog.first_id == first, Dialog.second_id == second)
        dialog = db.scalar(query)
        if not dialog:
            dialog = Dialog(first_id=first, second_id=second)
            db.add(dialog)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                dialog = db.scalar(query)
        return {"id": dialog.id, "login": other.login}


def serialize(message):
    return {"id": message.id, "dialog_id": message.dialog_id, "sender_id": message.sender_id,
            "text": message.text, "client_id": message.client_id,
            "created_at": message.created_at.isoformat(), "expires_at": message.expires_at.isoformat()}


@app.get("/api/dialogs/{dialog_id}/messages")
def history(dialog_id: int, before: int | None = None, user=Depends(current_user)):
    with SessionLocal() as db:
        member(db, dialog_id, user["id"])
        query = select(Message).where(Message.dialog_id == dialog_id, Message.expires_at > now())
        if before is not None:
            query = query.where(Message.id < before)
        rows = db.scalars(query.order_by(Message.id.desc()).limit(100)).all()
        return [serialize(row) for row in reversed(rows)]


async def notify(user_ids, payload):
    async def send(ws):
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=3)
        except Exception:
            with suppress(Exception):
                await ws.close()
    await asyncio.gather(*(send(ws) for uid in user_ids for ws in list(connections.get(uid, ()))))


@app.post("/api/dialogs/{dialog_id}/messages")
async def send_message(dialog_id: int, data: NewMessage, user=Depends(current_user)):
    limited(("messages", user["id"]), 60)
    text = data.text.strip()
    if not text:
        raise HTTPException(422, "Сообщение не может быть пустым.")
    def save():
        with SessionLocal() as db:
            dialog = member(db, dialog_id, user["id"])
            query = select(Message).where(Message.sender_id == user["id"], Message.client_id == str(data.client_id))
            message = db.scalar(query)
            if not message:
                stamp = now()
                message = Message(dialog_id=dialog_id, sender_id=user["id"], text=text,
                                  client_id=str(data.client_id), created_at=stamp,
                                  expires_at=stamp + timedelta(hours=24))
                db.add(message)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    message = db.scalar(query)
            if message.dialog_id != dialog_id or message.text != text:
                raise HTTPException(409, "Идентификатор сообщения уже использован.")
            if message.expires_at <= now():
                raise HTTPException(409, "Срок хранения сообщения уже истёк.")
            return serialize(message), (dialog.first_id, dialog.second_id)
    payload, recipients = await run_in_threadpool(save)
    await notify(recipients, {"type": "message", "message": payload})
    return payload


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    if ws.headers.get("origin") not in ORIGINS:
        await ws.close(code=4403)
        return
    token = ws.cookies.get(COOKIE)
    try:
        user = await run_in_threadpool(authenticated, token)
    except HTTPException:
        await ws.close(code=4401)
        return
    if len(connections[user["id"]]) >= 5:
        await ws.close(code=4429)
        return
    await ws.accept()
    connections[user["id"]].add(ws)
    try:
        while True:
            await asyncio.sleep(20)
            await run_in_threadpool(authenticated, token)
            await asyncio.wait_for(ws.send_json({"type": "ping"}), timeout=3)
    except (WebSocketDisconnect, HTTPException, RuntimeError, OSError, asyncio.TimeoutError):
        with suppress(Exception):
            await ws.close(code=4401)
    finally:
        connections[user["id"]].discard(ws)
        if not connections[user["id"]]:
            del connections[user["id"]]


@app.get("/health")
def health():
    with SessionLocal() as db:
        db.execute(select(1))
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
