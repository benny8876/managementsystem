import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.core.live import broker
from app.core.security import decode_token
from app.db.session import SessionLocal
from app.models.users import User

router = APIRouter(tags=["live"])

PING_SECONDS = 25  # keeps proxies / phone networks from dropping an idle socket
AUTH_TIMEOUT = 10


def _user_branch(token: str) -> tuple[bool, int | None]:
    payload = decode_token(token)
    if not payload or payload.get("typ") != "access":
        return False, None
    with SessionLocal() as db:
        user = db.get(User, int(payload["sub"]))
        if not user or not user.is_active:
            return False, None
        return True, user.branch_id


async def _pump(ws: WebSocket, branch_id: int | None, public: bool) -> None:
    q = broker.subscribe(branch_id, public)
    try:
        await ws.send_json({"type": "ready"})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                msg = {"type": "ping"}
            await ws.send_json(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        broker.unsubscribe(q)


@router.websocket("/ws")
async def staff_socket(ws: WebSocket, branch_id: int | None = None):
    """Logged-in counters. The access token is sent as the first message (not
    in the URL) so it never lands in access logs."""
    await ws.accept()
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT)
        token = json.loads(raw).get("token", "")
    except (asyncio.TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
        await ws.close(code=4401)
        return
    ok, user_branch = await run_in_threadpool(_user_branch, token)
    if not ok:
        await ws.close(code=4401)
        return
    # Branch-bound staff only ever hear about their own branch.
    await _pump(ws, user_branch or branch_id, public=False)


@router.websocket("/ws/public/{branch_id}")
async def public_socket(ws: WebSocket, branch_id: int):
    """Patient QR page and waiting-room TV: queue changes only, no login."""
    await ws.accept()
    await _pump(ws, branch_id, public=True)
