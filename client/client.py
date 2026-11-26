"""Console client. Talks to the system only through nginx (O1.7, O2.4, O4.5, O5.4)."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests
from websocket import create_connection

API = os.environ.get("API_URL", "http://localhost:8080")
WS = os.environ.get("WS_URL", API.replace("http://", "ws://").replace("https://", "wss://"))
ORIGIN = os.environ.get("CLIENT_ORIGIN", "http://localhost:8080")
STATE = Path(os.environ.get("CLIENT_STATE", ".client_state.json"))


def load_token() -> str:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8")).get("token", "")
    return ""


def save_token(token: str) -> None:
    STATE.write_text(json.dumps({"token": token}), encoding="utf-8")


def retry_after(headers: dict, default: float) -> float:
    """Honour Retry-After (seconds or HTTP-date) before the next poll/retry."""
    value = headers.get("Retry-After")
    if not value:
        return float(default)
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return float(default)


def call(method: str, path: str, *, token: str | None = None, **kwargs) -> tuple[int, dict, dict]:
    headers = dict(kwargs.pop("headers", {}))
    if token is None:
        token = load_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json")
    try:
        response = requests.request(
            method, API + path, headers=headers, timeout=float(os.environ.get("CLIENT_TIMEOUT", "30")), **kwargs
        )
    except requests.RequestException as error:
        print(f"НЕВІДОМИЙ РЕЗУЛЬТАТ: {error}")
        return 0, {}, {}
    content_type = response.headers.get("Content-Type", "")
    body = response.json() if response.content and ("json" in content_type) else {}
    if response.status_code >= 400:
        print(f"ПОМИЛКА {response.status_code}: {body.get('title', '')} — {body.get('detail', '')} [{body.get('code', '')}]")
    return response.status_code, body, dict(response.headers)


def login(args) -> None:
    status, body, _ = call("POST", "/v1/auth/login", token="", json={"login": args.login, "password": args.password})
    if status == 200:
        save_token(body["token"])
        user = body.get("user", {})
        print(f"Увійшли як {user.get('login')} ({user.get('role')})")


def logout(_) -> None:
    status, _, _ = call("POST", "/v1/auth/logout")
    if status == 204:
        save_token("")
        print("Вийшли")


def me(_) -> None:
    status, body, _ = call("GET", "/v1/me")
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def ride(args) -> None:
    key = args.key or str(uuid.uuid4())
    payload = {
        "district": args.district,
        "pickup": args.pickup,
        "distance_km": args.distance,
        "duration_min": args.duration,
    }
    if args.tariff:
        payload["tariff"] = args.tariff
    deadline = time.monotonic() + 30
    for attempt in range(1, args.attempts + 1):
        status, body, headers = call(
            "POST", "/v1/rides", json=payload,
            headers={"Idempotency-Key": key},
        )
        if status in (201, 200):
            print(json.dumps(body, ensure_ascii=False, indent=2))
            print("Location:", headers.get("Location", ""))
            return
        transient = status in (429, 503, 0) or (
            status == 409 and body.get("code") == "key_in_use"
        )
        if transient and attempt < args.attempts and time.monotonic() < deadline:
            pause = max(retry_after(headers, 1), min(0.5 * 2 ** attempt, 8))
            pause *= 1 + random.random() * 0.25  # jitter avoids a retry storm
            print(f"Повтор {attempt}/{args.attempts} через {pause:.2f} с, той самий ключ {key}")
            time.sleep(pause)
            continue
        print(f"РЕЗУЛЬТАТ НЕВІДОМИЙ (спроб {attempt}, ключ {key})")
        return
    print(f"РЕЗУЛЬТАТ НЕВІДОМИЙ (вичерпано спроби, ключ {key})")


def list_rides(args) -> None:
    """Read every page: an unknown next_cursor must not hide older rides."""
    cursor = args.cursor
    items: list[dict] = []
    pages = 0
    seen: set[str] = set()
    while True:
        path = f"/v1/rides?limit={args.limit}"
        if cursor:
            path += f"&cursor={quote(str(cursor), safe='')}"
        status, body, _ = call("GET", path)
        if status != 200:
            return
        pages += 1
        items.extend(body.get("items", []))
        cursor = body.get("next_cursor")
        if not cursor or str(cursor) in seen:
            break
        seen.add(str(cursor))
    print(json.dumps({"items": items, "pages": pages, "count": len(items)}, ensure_ascii=False, indent=2))


def get_ride(args) -> None:
    status, body, headers = call("GET", f"/v1/rides/{args.id}")
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print("ETag:", headers.get("ETag", ""))


def patch_ride(args) -> None:
    status, body, _ = call(
        "PATCH", f"/v1/rides/{args.id}", json={"pickup": args.pickup},
        headers={"If-Match": args.etag},
    )
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def cancel(args) -> None:
    status, body, _ = call("POST", f"/v1/rides/{args.id}/cancel")
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def staff_action(path: str, args) -> None:
    status, body, _ = call("POST", path)
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def drivers(_) -> None:
    status, body, _ = call("GET", "/v1/drivers")
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def flag(args) -> None:
    status, body, _ = call("POST", f"/v1/admin/features/{args.name}", json={"enabled": args.enabled == "true"})
    if status == 200:
        print(json.dumps(body, indent=2))


def pay(args) -> None:
    key = args.key or str(uuid.uuid4())
    status, body, headers = call(
        "POST", f"/v1/rides/{args.id}/payments", json={"amount_minor": args.amount},
        headers={"Idempotency-Key": key},
    )
    if status != 202:
        return
    operation = body.get("operation_id")
    print("Операція:", operation)
    deadline = time.monotonic() + 60
    delay = retry_after(headers, 2)
    while time.monotonic() < deadline:
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        status, body, headers = call("GET", f"/v1/operations/{operation}")
        if status != 200:
            print(f"РЕЗУЛЬТАТ НЕВІДОМИЙ: GET операції повернув {status}")
            return
        if body.get("status") in ("succeeded", "failed"):
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return
        delay = retry_after(headers, delay)
    print("РЕЗУЛЬТАТ НЕВІДОМИЙ: операція не завершилася вчасно")


def effects(_) -> None:
    status, body, _ = call("GET", "/sim/effects")
    if status == 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))


def concurrency(args) -> None:
    key_prefix = str(uuid.uuid4())
    def one(index: int):
        return call(
            "POST", "/v1/rides",
            json={"district": args.district, "pickup": f"точка {index}", "distance_km": 3},
            headers={"Idempotency-Key": f"{key_prefix}-{index}"},
        )[0]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        codes = list(pool.map(one, range(args.orders)))
    print(json.dumps({
        "sent": len(codes),
        "created": codes.count(201),
        "conflicts_409": codes.count(409),
        "other": {code: codes.count(code) for code in set(codes) if code not in (201, 409)},
    }, indent=2))
    print("Перевірте інваріант: POST /v1/drivers (роль staff) — busy == кількість вільних водіїв району.")


def _apply(notification: dict, applied: set[str]) -> int:
    notification_id = notification.get("id", "")
    if not notification_id or notification_id in applied:
        return 0
    applied.add(notification_id)
    print(f"[{notification.get('type')}] {notification.get('payload', {})}")
    return int(notification.get("seq") or 0)


def watch(args) -> None:
    status, ticket_body, _ = call("POST", "/v1/ws-tickets")
    if status != 201:
        return
    ticket = ticket_body["ticket"]
    applied: set[str] = set()
    last_seq = 0
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        try:
            socket = create_connection(
                f"{WS}/v1/ws?ticket={ticket}", origin=ORIGIN, timeout=5
            )
        except Exception as error:
            print(f"Не вдалося підключитися: {error}")
            time.sleep(1)
            continue
        # Subscribe first, then read head + history, then apply buffered messages.
        buffered = []
        try:
            hello = json.loads(socket.recv())
            if hello.get("type") == "connected":
                print("Підписку встановлено")
            status, head_body, _ = call("GET", "/v1/me/notifications/head")
            head = int(head_body.get("head", 0)) if status == 200 else last_seq
            after = last_seq
            while after < head:
                status, page, _ = call("GET", f"/v1/me/notifications?after={after}&limit=50")
                items = page.get("items", []) if status == 200 else []
                if not items:
                    break
                for item in items:
                    last_seq = max(last_seq, _apply(item, applied))
                    after = int(item.get("seq", after))
                if not page.get("next_cursor"):
                    break
            socket.settimeout(2)
            while time.monotonic() < deadline:
                try:
                    raw = socket.recv()
                except Exception:
                    break
                if not raw:
                    break
                message = json.loads(raw)
                if message.get("type") == "heartbeat":
                    continue
                # WS carries the same monotonic seq as the feed; a gap left by
                # a broken socket is filled on the next history read.
                last_seq = max(last_seq, _apply(message, applied))
        finally:
            socket.close()
        status, ticket_body, _ = call("POST", "/v1/ws-tickets")
        if status != 201:
            break
        ticket = ticket_body["ticket"]
    print("Перегляд завершено")


def main() -> None:
    parser = argparse.ArgumentParser(description="C14 Taxi console client")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login"); p.add_argument("--login", default="passenger1"); p.add_argument("--password", default="pass123"); p.set_defaults(func=login)
    p = sub.add_parser("logout"); p.set_defaults(func=logout)
    p = sub.add_parser("me"); p.set_defaults(func=me)
    p = sub.add_parser("ride")
    p.add_argument("--district", default="central"); p.add_argument("--pickup", default="вул. Хрещатик, 1")
    p.add_argument("--tariff", choices=["standard", "economy", "comfort"]); p.add_argument("--distance", type=float, default=2.5)
    p.add_argument("--duration", type=int, default=10); p.add_argument("--key"); p.add_argument("--attempts", type=int, default=4)
    p.set_defaults(func=ride)
    p = sub.add_parser("list"); p.add_argument("--limit", type=int, default=20); p.add_argument("--cursor"); p.set_defaults(func=list_rides)
    p = sub.add_parser("get"); p.add_argument("id"); p.set_defaults(func=get_ride)
    p = sub.add_parser("patch"); p.add_argument("id"); p.add_argument("--pickup", required=True); p.add_argument("--etag", required=True); p.set_defaults(func=patch_ride)
    p = sub.add_parser("cancel"); p.add_argument("id"); p.set_defaults(func=cancel)
    p = sub.add_parser("accept"); p.add_argument("id"); p.set_defaults(func=lambda a: staff_action(f"/v1/rides/{a.id}/accept", a))
    p = sub.add_parser("finish"); p.add_argument("id"); p.set_defaults(func=lambda a: staff_action(f"/v1/rides/{a.id}/finish", a))
    p = sub.add_parser("pay"); p.add_argument("id"); p.add_argument("--amount", type=int); p.add_argument("--key"); p.set_defaults(func=pay)
    p = sub.add_parser("drivers"); p.set_defaults(func=drivers)
    p = sub.add_parser("offline"); p.add_argument("id"); p.set_defaults(func=lambda a: staff_action(f"/v1/drivers/{a.id}/offline", a))
    p = sub.add_parser("online"); p.add_argument("id"); p.set_defaults(func=lambda a: staff_action(f"/v1/drivers/{a.id}/online", a))
    p = sub.add_parser("flag"); p.add_argument("name"); p.add_argument("enabled", choices=["true", "false"]); p.set_defaults(func=flag)
    p = sub.add_parser("effects"); p.set_defaults(func=effects)
    p = sub.add_parser("concurrency"); p.add_argument("--orders", type=int, default=20); p.add_argument("--workers", type=int, default=20); p.add_argument("--district", default="central"); p.set_defaults(func=concurrency)
    p = sub.add_parser("watch"); p.add_argument("--seconds", type=int, default=30); p.set_defaults(func=watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
