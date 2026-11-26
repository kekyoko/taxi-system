"""One-command verification of the implemented lab items through nginx only."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import requests
from websocket import create_connection

API = os.environ.get("API_URL", "http://localhost:8080")
WS_URL = os.environ.get("WS_URL", API.replace("http://", "ws://").replace("https://", "wss://"))
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def request(method: str, path: str, token: str = "", **kwargs):
    headers = dict(kwargs.pop("headers", {}))
    headers.setdefault("Content-Type", "application/json")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, API + path, headers=headers, timeout=20, **kwargs)


def wait_health() -> bool:
    for _ in range(60):
        try:
            if requests.get(API + "/health", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def login(login: str) -> str:
    response = request("POST", "/v1/auth/login", json={"login": login, "password": "pass123"})
    return response.json().get("token", "") if response.status_code == 200 else ""


def free_drivers(staff_token: str) -> None:
    drivers = request("GET", "/v1/drivers", staff_token).json().get("items", [])
    for driver in drivers:
        if driver["district"] == "central" and driver["status"] != "online":
            request("POST", f"/v1/drivers/{driver['id']}/online", staff_token)


def new_ride(token: str, key: str | None = None, body: dict | None = None):
    payload = {"district": "central", "pickup": "вул. Хрещатик, 1", "distance_km": 3, "duration_min": 12}
    payload.update(body or {})
    return request(
        "POST", "/v1/rides", token,
        json=payload, headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


def main() -> int:
    check("nginx /health", wait_health())
    passenger = login("passenger1")
    other = login("passenger2")
    dispatcher = login("dispatcher")
    check("login via both app instances", bool(passenger and other and dispatcher))

    response = request("GET", "/v1/me", headers={"X-User-Id": "u-passenger-1"})
    check("X-User-Id no longer accepted", response.status_code == 401, str(response.status_code))

    free_drivers(dispatcher)
    key = str(uuid.uuid4())
    first = new_ride(passenger, key)
    second = new_ride(passenger, key)
    changed = new_ride(passenger, key, {"pickup": "інша адреса"})
    check("main operation 201 + Location", first.status_code == 201 and bool(first.headers.get("Location")))
    check("X-Request-ID on response", bool(first.headers.get("X-Request-ID")))
    check("idempotent replay returns saved result",
          second.status_code == 201 and second.json().get("id") == first.json().get("id"))
    check("same key with another body is 422", changed.status_code == 422, str(changed.status_code))
    ride_id = first.json().get("id", "")
    etag = first.headers.get("ETag", '"1"')

    forged = request("PATCH", f"/v1/rides/{ride_id}", passenger,
                     json={"pickup": "а", "status": "finished", "user_id": "u-passenger-2"},
                     headers={"If-Match": etag})
    check("forged owner/state fields are 422", forged.status_code == 422, str(forged.status_code))

    check("own ride readable", request("GET", f"/v1/rides/{ride_id}", passenger).status_code == 200)
    check("foreign ride is 404", request("GET", f"/v1/rides/{ride_id}", other).status_code == 404)
    check("If-Match missing is 428",
          request("PATCH", f"/v1/rides/{ride_id}", passenger, json={"pickup": "а"}).status_code == 428)
    check("stale If-Match is 412",
          request("PATCH", f"/v1/rides/{ride_id}", passenger,
                  json={"pickup": "а"}, headers={"If-Match": '"99"'}).status_code == 412)
    patched = request("PATCH", f"/v1/rides/{ride_id}", passenger,
                      json={"pickup": "вул. Володимирська, 10"}, headers={"If-Match": etag})
    check("conditional PATCH 200 + new ETag", patched.status_code == 200 and patched.headers.get("ETag") != etag)

    cancelled = request("POST", f"/v1/rides/{ride_id}/cancel", passenger)
    replay = request("POST", f"/v1/rides/{ride_id}/cancel", passenger)
    check("repeated cancel does not free driver twice",
          cancelled.status_code == 200 and replay.json().get("replayed") is True)

    check("passenger cannot take a driver offline",
          request("POST", "/v1/drivers/d-6/offline", passenger).status_code == 403)
    offline = request("POST", "/v1/drivers/d-6/offline", dispatcher)
    check("staff takes driver offline", offline.status_code == 200)
    request("POST", "/v1/drivers/d-6/online", dispatcher)

    # Concurrency: 20 orders against the 5 central drivers.
    free_drivers(dispatcher)
    keys = [str(uuid.uuid4()) for _ in range(20)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(
            lambda i: request("POST", "/v1/rides", passenger,
                              json={"district": "central", "pickup": f"точка {i}"},
                              headers={"Idempotency-Key": keys[i]}).status_code,
            range(20),
        ))
    check("invariant: 20 concurrent orders, 5 drivers",
          codes.count(201) <= 5 and codes.count(409) >= 15,
          f"201={codes.count(201)}, 409={codes.count(409)}")

    # Tariff flag and backward compatibility.
    request("POST", "/v1/admin/features/tariffs_v2", dispatcher, json={"enabled": True})
    # least_conn alternates backends. A probe on app-b makes the next POST
    # land on the canary-enabled app-a, so the tariff check is deterministic.
    tariff_statuses = []
    for _ in range(6):
        free_drivers(dispatcher)
        for _probe in range(6):
            probe = request("GET", "/health")
            if probe.headers.get("X-Instance") == "app-b":
                break
        tariff_ride = new_ride(passenger, body={"tariff": "comfort", "distance_km": 7.5})
        tariff_statuses.append(tariff_ride.status_code)
        if tariff_ride.status_code == 201:
            break
    check("tariffs v2 enabled by flag on canary instance",
          201 in tariff_statuses, str(tariff_statuses))
    free_drivers(dispatcher)
    old_client = request("POST", "/v1/rides", passenger,
                         json={"district": "central", "pickup": "без тарифу"},
                         headers={"Idempotency-Key": str(uuid.uuid4())})
    check("old client without tariff still works", old_client.status_code == 201)
    request("POST", "/v1/admin/features/tariffs_v2", dispatcher, json={"enabled": False})
    free_drivers(dispatcher)
    disabled = new_ride(passenger, body={"tariff": "comfort"})
    check("flag off rejects new tariff", disabled.status_code == 409, str(disabled.status_code))

    # Async payment with the external simulator.
    free_drivers(dispatcher)
    paid_ride = new_ride(passenger)
    ride_id = paid_ride.json().get("id", "")
    check("dispatcher accepts", request("POST", f"/v1/rides/{ride_id}/accept", dispatcher).status_code == 200)
    check("dispatcher finishes", request("POST", f"/v1/rides/{ride_id}/finish", dispatcher).status_code == 200)
    payment = request("POST", f"/v1/rides/{ride_id}/payments", passenger,
                      json={}, headers={"Idempotency-Key": str(uuid.uuid4())})
    check("payment starts asynchronously", payment.status_code == 202 and bool(payment.headers.get("Location")))
    check("payment returns Retry-After", bool(payment.headers.get("Retry-After")))
    operation_id = payment.json().get("operation_id", "")
    final = None
    for _ in range(30):
        final = request("GET", f"/v1/operations/{operation_id}", passenger).json()
        if final.get("status") in ("succeeded", "failed"):
            break
        time.sleep(1)
    check("operation reached a terminal state", bool(final and final.get("status") in ("succeeded", "failed")),
          str(final and final.get("status")))
    check("operation status is owner-only",
          request("GET", f"/v1/operations/{operation_id}", other).status_code == 404)
    effects = request("GET", "/sim/effects").json()
    check("one external effect per provider key",
          effects.get("by_key", {}).get(operation_id) is not None)

    feed = request("GET", "/v1/me/notifications?limit=50", passenger)
    head = request("GET", "/v1/me/notifications/head", passenger)
    positions = [item.get("seq", 0) for item in feed.json().get("items", [])]
    check("notification feed and head", feed.status_code == 200 and head.status_code == 200,
          f"head={head.json().get('head')}")
    check("notification positions grow without duplicates",
          positions == sorted(set(positions)), f"{len(positions)} items")
    ticket_response = request("POST", "/v1/ws-tickets", passenger)
    check("WebSocket ticket issued", ticket_response.status_code == 201)
    if ticket_response.status_code == 201:
        try:
            socket = create_connection(
                f"{WS_URL}/v1/ws?ticket={ticket_response.json()['ticket']}",
                origin=os.environ.get("CLIENT_ORIGIN", API),
                timeout=5,
            )
            hello = json.loads(socket.recv())
            check("WebSocket upgrade with ticket and Origin", hello.get("type") == "connected")
            socket.settimeout(10)
            free_drivers(dispatcher)
            created = new_ride(passenger)
            message = json.loads(socket.recv())
            check("WS notification carries seq",
                  created.status_code == 201 and int(message.get("seq") or 0) > 0,
                  str(message)[:120])
            socket.close()
        except Exception as error:  # noqa: BLE001 - verification reports the handshake failure
            check("WebSocket upgrade with ticket and Origin", False, str(error))
            check("WS notification carries seq", False, str(error))

    # A ticket is single-use: a second one is needed for the negative Origin test.
    second = request("POST", "/v1/ws-tickets", passenger)
    if second.status_code == 201:
        try:
            bad = create_connection(
                f"{WS_URL}/v1/ws?ticket={second.json()['ticket']}",
                suppress_origin=True, timeout=5,
            )
            check("WebSocket rejects a request without Origin", False)
            bad.close()
        except Exception:  # noqa: BLE001 - 1008 before accept() is the expected outcome
            check("WebSocket rejects a request without Origin", True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\nВсього: {len(results)}, успішно: {len(results) - len(failed)}, невдало: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as error:
        print(f"check failed: {error}")
        sys.exit(2)
