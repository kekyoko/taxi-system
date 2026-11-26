"""Controlled crash points shared by every process (FAULTS=on + flag file)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import settings
from .db import transaction


def inject_fault(name: str) -> None:
    if not settings.fault_mode:
        return
    flag = Path(settings.faults_dir) / name
    # One flag must cause exactly one crash even when several processes poll it.
    try:
        flag.unlink()
    except FileNotFoundError:
        return
    with transaction() as conn:
        conn.execute(
            "INSERT INTO fault_hits(name, instance) VALUES (%s, %s)",
            (name, settings.instance),
        )
    print(
        json.dumps({"event": "fault.hit", "name": name, "instance": settings.instance}),
        flush=True,
    )
    os._exit(137)
