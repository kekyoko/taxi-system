"""O4.3 experiment: transient provider 503 -> running -> retry -> one effect."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import requests

API = os.environ.get("API_URL", "http://localhost:8080")
DOWN_S = int(os.environ.get("PROVIDER_DOWN_S", "6"))


def login(name: str) -> str:
    response = requests.post(
        API + "/v1/auth/login", json={"login": name, "password": "pass123"}, timeout=20
    )
    return response.json()["token"]


def call(method: str, path: str, token: str, **kwargs) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(kwargs.pop("headers", {}))
    return requests.request(method, API + path, headers=headers, timeout=20, **kwargs)


def main() -> int:
    passenger, dispatcher = login("passenger1"), login("dispatcher")
    for driver in call("GET", "/v1/drivers", dispatcher).json()["items"]:
        if driver["district"] == "central" and driver["status"] != "online":
            call("POST", f"/v1/drivers/{driver['id']}/online", dispatcher)

    ride = call(
        "POST", "/v1/rides", passenger,
        json={"district": "central", "pickup": "перевірка тимчасової недоступності"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    ).json()["id"]
    call("POST", f"/v1/rides/{ride}/accept", dispatcher)
    call("POST", f"/v1/rides/{ride}/finish", dispatcher)

    requests.post(
        API + f"/sim/admin/unavailable?seconds={DOWN_S}", timeout=20
    ).raise_for_status()
    payment = call(
        "POST", f"/v1/rides/{ride}/payments", passenger, json={},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    operation = payment.json()["operation_id"]

    states: list[str] = []
    final: dict = {}
    for _ in range(60):
        final = call("GET", f"/v1/operations/{operation}", passenger).json()
        if final["status"] not in states:
            states.append(final["status"])
        if final["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    effects = requests.get(API + "/sim/effects", timeout=20).json()["by_key"]
    ok = (
        "running" in states
        and final.get("status") == "succeeded"
        and effects.get(operation) == "succeeded"
    )
    print(json.dumps({
        "states": states,
        "final": final.get("status"),
        "version": final.get("version"),
        "effects_for_key": effects.get(operation),
        "result": "PASS" if ok else "FAIL",
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as error:
        print(f"provider_down experiment failed: {error}")
        sys.exit(2)
