"""O6.2: replay saved requests of the pre-tariffs client against the new server."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

API = os.environ.get("API_URL", "http://localhost:8080")
SPEC = Path(__file__).resolve().parent.parent / "docs" / "client-v1.json"


def login(name: str) -> str:
    response = requests.post(
        API + "/v1/auth/login", json={"login": name, "password": "pass123"}, timeout=20
    )
    return response.json().get("token", "") if response.status_code == 200 else ""


def free_drivers(token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    drivers = requests.get(API + "/v1/drivers", headers=headers, timeout=20).json()
    for driver in drivers.get("items", []):
        if driver["district"] == "central" and driver["status"] != "online":
            requests.post(
                API + f"/v1/drivers/{driver['id']}/online", headers=headers, timeout=20
            )


def has_field(body: dict, dotted: str) -> bool:
    value: object = body
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False
    return True


def main() -> int:
    token = login("passenger1")
    dispatcher = login("dispatcher")
    if not token or not dispatcher:
        print("FAIL: demo logins are unavailable")
        return 2
    free_drivers(dispatcher)

    steps = json.loads(SPEC.read_text(encoding="utf-8"))["steps"]
    saved: dict[str, dict[str, str]] = {}
    keys: dict[str, str] = {}
    failures = 0
    for step in steps:
        path = step["path"]
        for name, values in saved.items():
            path = path.replace("{" + name + "}", values.get("id", ""))
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        if step.get("idempotent"):
            keys[step["name"]] = str(uuid.uuid4())
        if "same_key_as" in step:
            keys[step["name"]] = keys[step["same_key_as"]]
        if "same_key_as" in step or step.get("idempotent"):
            headers["Idempotency-Key"] = keys[step["name"]]
        if "if_match_from" in step:
            headers["If-Match"] = saved[step["if_match_from"]].get("etag", '"1"')

        response = requests.request(
            step["method"], API + path, json=step.get("body"), headers=headers, timeout=20
        )
        body = response.json() if response.content else {}
        ok = response.status_code == step["status"]
        if "same_id_as" in step:
            ok = ok and body.get("id") == saved[step["same_id_as"]].get("id")
        ok = ok and all(has_field(body, field) for field in step.get("fields", []))
        print(f"{'PASS' if ok else 'FAIL'}  {step['name']}: {response.status_code}")
        failures += 0 if ok else 1
        if step.get("save"):
            saved[step["save"]] = {
                "id": str(body.get("id", "")),
                "etag": response.headers.get("ETag", '"1"'),
            }
    print(f"Replayed {len(steps)} saved requests; failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as error:
        print(f"replay failed: {error}")
        sys.exit(2)
