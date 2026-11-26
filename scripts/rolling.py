"""O6.4: load experiment while the canary flag rolls out (no release needed)."""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

API = os.environ.get("API_URL", "http://localhost:8080")
TOTAL = int(os.environ.get("ROLLING_REQUESTS", "300"))
WORKERS = int(os.environ.get("ROLLING_WORKERS", "10"))


def login(name: str) -> str:
    response = requests.post(
        API + "/v1/auth/login", json={"login": name, "password": "pass123"}, timeout=20
    )
    return response.json().get("token", "") if response.status_code == 200 else ""


def flag(dispatcher: str, enabled: bool) -> None:
    requests.post(
        API + "/v1/admin/features/tariffs_v2",
        json={"enabled": enabled},
        headers={"Authorization": f"Bearer {dispatcher}"},
        timeout=20,
    )


def one(token: str) -> tuple[int, str, float]:
    started = time.perf_counter()
    try:
        response = requests.get(
            API + "/v1/rides?limit=1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        return response.status_code, response.headers.get("X-Instance", "?"), (
            time.perf_counter() - started
        ) * 1000
    except requests.RequestException:
        return 0, "?", (time.perf_counter() - started) * 1000


def batch(token: str, size: int) -> list[tuple[int, str, float]]:
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda _: one(token), range(size)))


def main() -> int:
    passenger, dispatcher = login("passenger1"), login("dispatcher")
    if not passenger or not dispatcher:
        print("FAIL: demo logins are unavailable")
        return 2
    size = TOTAL // 3
    samples: list[tuple[int, str, float]] = []
    started = time.perf_counter()
    try:
        for enabled in (True, False, True):
            flag(dispatcher, enabled)
            samples.extend(batch(passenger, size))
        duration = time.perf_counter() - started
    finally:
        flag(dispatcher, False)

    statuses = Counter(code for code, _, _ in samples)
    instances = Counter(name for _, name, _ in samples)
    latencies = [latency for _, _, latency in samples]
    technical = sum(count for code, count in statuses.items() if code == 0 or code >= 500)
    subject = sum(count for code, count in statuses.items() if code in (409, 422))
    report = {
        "requests": len(samples),
        "duration_s": round(duration, 2),
        "workers": WORKERS,
        "statuses": dict(statuses),
        "instances": dict(instances),
        "avg_ms": round(sum(latencies) / len(latencies), 1),
        "max_ms": round(max(latencies), 1),
        "technical_errors": technical,
        "technical_error_rate": round(technical / len(samples), 5),
        "subject_deviations": subject,
        "error_rule": "0 or HTTP >= 500 is technical; 409/422 are counted separately",
    }
    print(json.dumps(report, indent=2))
    return 0 if technical / len(samples) <= 0.005 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as error:
        print(f"rolling experiment failed: {error}")
        sys.exit(2)
