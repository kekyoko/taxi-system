"""WebSocket notification gateway. Two independent instances; one Redis consumer group each."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker import NOTIFICATION_STREAM, client, ensure_group  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import transaction  # noqa: E402

GROUP = os.environ.get("GATEWAY_GROUP", "gateway-a")
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_S", "20"))
ALLOWED_ORIGINS = {settings.client_origin, settings.extra_client_origin}


@dataclass(eq=False)  # identity semantics: WebSocket objects are unhashable
class Connection:
    socket: WebSocket
    token_hash: str
    seen: set[str] = field(default_factory=set)


connections: dict[str, set[Connection]] = {}


def _add(user_id: str, connection: Connection) -> None:
    connections.setdefault(user_id, set()).add(connection)


def _remove(user_id: str, connection: Connection) -> None:
    bucket = connections.get(user_id)
    if bucket:
        bucket.discard(connection)
        if not bucket:
            connections.pop(user_id, None)


async def _send(connection: Connection, message: dict) -> bool:
    try:
        await connection.socket.send_json(message)
        return True
    except Exception:  # noqa: BLE001 - a dead socket is removed from the registry
        return False


async def _deliver(fields: dict[str, str]) -> None:
    payload = json.loads(fields.get("payload", "{}"))
    event_type = fields["event_type"]
    if event_type == "session.logout":
        target = payload.get("token_hash")
        for user_id, bucket in list(connections.items()):
            for connection in list(bucket):
                if connection.token_hash == target:
                    await _close(connection, 1008, "Session ended")
                    _remove(user_id, connection)
        return

    user_id = payload.get("user_id")
    notification_id = payload.get("notification_id")
    if not user_id or not notification_id:
        return
    message = {
        "id": notification_id,
        # Stable monotonic position from the notification row; lets the client
        # resume catch-up after a disconnect without duplicates or gaps.
        "seq": int(payload.get("seq") or 0),
        "type": event_type,
        "payload": payload.get("body", {}),
    }
    for connection in list(connections.get(user_id, set())):
        if notification_id in connection.seen:
            continue
        if await _send(connection, message):
            connection.seen.add(notification_id)


async def _close(connection: Connection, code: int, reason: str) -> None:
    if connection.socket.client_state == WebSocketState.CONNECTED:
        try:
            await connection.socket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


async def consume() -> None:
    loop = asyncio.get_running_loop()
    broker = client()
    await loop.run_in_executor(None, ensure_group, NOTIFICATION_STREAM, GROUP)
    consumer = f"{GROUP}-{os.getpid()}"
    print(json.dumps({"event": "gateway.consumer", "group": GROUP, "consumer": consumer}), flush=True)
    while True:
        try:
            messages = await loop.run_in_executor(
                None,
                lambda: broker.xreadgroup(
                    GROUP, consumer, {NOTIFICATION_STREAM: ">"}, count=20, block=1000
                ),
            )
            for _, items in messages:
                for message_id, fields in items:
                    await _deliver(fields)
                    await loop.run_in_executor(
                        None, lambda mid=message_id: broker.xack(NOTIFICATION_STREAM, GROUP, mid)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            print(json.dumps({"event": "gateway.error", "error": str(error)}), flush=True)
            await asyncio.sleep(1)


async def heartbeat(connection: Connection) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if not await _send(connection, {"type": "heartbeat"}):
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.consumer_task = asyncio.create_task(consume())
    yield
    app.state.consumer_task.cancel()
    try:
        await app.state.consumer_task
    except asyncio.CancelledError:
        pass
    for bucket in list(connections.values()):
        for connection in list(bucket):
            await _close(connection, 1012, "Server shutting down")


app = FastAPI(title="C14 WebSocket gateway", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "gateway": GROUP, "connections": sum(len(b) for b in connections.values())}


@app.websocket("/v1/ws")
async def websocket_endpoint(websocket: WebSocket, ticket: str = ""):
    origin = websocket.headers.get("origin")
    if not origin or origin not in ALLOWED_ORIGINS:
        # Reject before accept(): no authenticated socket for a foreign origin.
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    with transaction() as conn:
        row = conn.execute(
            """
            WITH claimed AS (
                UPDATE ws_tickets SET used_at=now()
                WHERE ticket=%s AND used_at IS NULL AND expires_at > now()
                RETURNING user_id, token_hash
            )
            SELECT claimed.user_id, claimed.token_hash
            FROM claimed
            JOIN sessions s ON s.token_hash = claimed.token_hash
                          AND s.user_id = claimed.user_id
                          AND s.expires_at > now()
            """,
            (ticket,),
        ).fetchone()
    if row is None:
        await websocket.close(code=1008, reason="Invalid or expired ticket")
        return

    await websocket.accept()
    connection = Connection(socket=websocket, token_hash=row["token_hash"])
    user_id = row["user_id"]
    _add(user_id, connection)
    await _send(connection, {"type": "connected", "user_id": user_id})
    beat = asyncio.create_task(heartbeat(connection))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        beat.cancel()
        _remove(user_id, connection)
