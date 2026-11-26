"""Pure domain rules. No I/O here: every rule gets time as an argument (Д1.1)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .config import settings

KYIV = ZoneInfo("Europe/Kyiv")

TARIFFS: dict[str, dict[str, Decimal]] = {
    "standard": {"base": Decimal("2000"), "per_km": Decimal("800"), "per_min": Decimal("100"), "minimum": Decimal("2500")},
    "economy": {"base": Decimal("1800"), "per_km": Decimal("650"), "per_min": Decimal("90"), "minimum": Decimal("2200")},
    "comfort": {"base": Decimal("3500"), "per_km": Decimal("1200"), "per_min": Decimal("140"), "minimum": Decimal("4000")},
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "requested": {"assigned", "cancelled"},
    "assigned": {"riding", "cancelled", "requested"},
    "riding": {"finished"},
    "finished": set(),
    "cancelled": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def night_overlap(start: datetime, end: datetime) -> bool:
    """True when [start, end) intersects a local 23:00-06:00 night window."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    local_start = start.astimezone(KYIV)
    local_end = end.astimezone(KYIV)
    # A night window belongs to the previous calendar date for morning intervals.
    day = local_start.date() - timedelta(days=1)
    while day <= local_end.date():
        night_begin = datetime.combine(day, time(23, 0), tzinfo=KYIV)
        night_end = datetime.combine(day + timedelta(days=1), time(6, 0), tzinfo=KYIV)
        if local_start < night_end and local_end > night_begin:
            return True
        day += timedelta(days=1)
    return False


def fare_minor(
    *,
    distance_km: Decimal,
    duration_min: int,
    tariff: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> int:
    """Price in minor currency units (kopiykas), rounded half-up once."""
    if tariff not in TARIFFS:
        raise ValueError(f"unknown tariff: {tariff}")
    if distance_km < 0 or duration_min < 0:
        raise ValueError("distance and duration must be non-negative")
    rate = TARIFFS[tariff]
    raw = rate["base"] + rate["per_km"] * Decimal(distance_km) + rate["per_min"] * int(duration_min)
    if finished_at is not None and night_overlap(started_at, finished_at):
        raw = raw * Decimal("1.2")
    total = max(raw, rate["minimum"])
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def accept_deadline(now: datetime, minutes: int | None = None) -> datetime:
    return now + timedelta(minutes=minutes or settings.accept_deadline_minutes)


def fingerprint(body: dict) -> str:
    """Canonical body hash: key order and extra client fields do not change it."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def encode_cursor(created_at: datetime, ride_id: str) -> str:
    payload = json.dumps(
        {"t": created_at.astimezone(timezone.utc).isoformat(), "id": ride_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(settings.cursor_secret.encode(), payload, hashlib.sha256).digest()[:12]
    return _b64(payload) + "." + _b64(signature)


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _unb64(encoded_payload)
        expected = hmac.new(settings.cursor_secret.encode(), payload, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(expected, _unb64(encoded_signature)):
            raise ValueError("bad signature")
        data = json.loads(payload)
        created_at = datetime.fromisoformat(data["t"])
        if created_at.tzinfo is None:
            raise ValueError("naive timestamp")
        return created_at.astimezone(timezone.utc), str(data["id"])
    except Exception as error:  # noqa: BLE001 - any malformed cursor is a 400
        raise ValueError("malformed cursor") from error


def uuid_str() -> str:
    import uuid

    return str(uuid.uuid4())
