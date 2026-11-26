"""Idempotent demo data: two passengers, one dispatcher, seven drivers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connection  # noqa: E402
from app.security import hash_password  # noqa: E402

USERS = [
    ("u-passenger-1", "passenger1", "pass123", "user", "central"),
    ("u-passenger-2", "passenger2", "pass123", "user", "central"),
    ("u-dispatcher", "dispatcher", "pass123", "staff", "central"),
]

DRIVERS = [
    ("d-1", "central", 50.4501, 30.5234),
    ("d-2", "central", 50.4510, 30.5240),
    ("d-3", "central", 50.4520, 30.5250),
    ("d-4", "central", 50.4530, 30.5260),
    ("d-5", "central", 50.4540, 30.5270),
    ("d-6", "north", 50.4600, 30.5300),
    ("d-7", "north", 50.4610, 30.5310),
]


def main() -> None:
    with connection() as conn, conn.transaction():
        for user_id, login, password, role, district in USERS:
            conn.execute(
                """
                INSERT INTO users(id, login, password_hash, role, district)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, login, hash_password(password), role, district),
            )
        for driver_id, district, lat, lon in DRIVERS:
            conn.execute(
                """
                INSERT INTO drivers(id, district, status, version, lat, lon)
                VALUES (%s, %s, 'online', 1, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (driver_id, district, lat, lon),
            )
    print("seed ok: 3 users, 7 drivers")


if __name__ == "__main__":
    main()
