"""Environment-driven settings shared by all Python services."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str = _env(
        "DATABASE_URL", "postgresql://taxi:taxi@postgres:5432/taxi"
    )
    redis_url: str = _env("REDIS_URL", "redis://redis:6379/0")
    instance: str = _env("INSTANCE", "app-a")
    client_origin: str = _env("CLIENT_ORIGIN", "http://localhost:8080")
    extra_client_origin: str = _env("EXTRA_CLIENT_ORIGIN", "http://localhost:8081")
    cursor_secret: str = _env("CURSOR_SECRET", "taxi-lab-cursor-secret")
    accept_deadline_minutes: int = _int("ACCEPT_DEADLINE_MINUTES", 2)
    feature_tariffs_v2: bool = _bool("FEATURE_TARIFFS_V2", False)
    fault_mode: bool = _bool("FAULTS", False)
    faults_dir: str = _env("FAULTS_DIR", "/faults")
    sim_url: str = _env("SIM_URL", "http://external-sim:8085")
    poll_interval_ms: int = _int("POLL_INTERVAL_MS", 200)
    worker_retries: int = _int("WORKER_RETRIES", 3)
    log_level: str = _env("LOG_LEVEL", "info")


settings = Settings()
