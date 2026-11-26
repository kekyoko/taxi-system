from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain import (
    can_transition,
    decode_cursor,
    encode_cursor,
    fare_minor,
    fingerprint,
    night_overlap,
)
from app.security import hash_password, token_hash, verify_password

KYIV_OFFSET = timezone(timedelta(hours=2))  # 2026-11, Europe/Kyiv (testing pure arithmetic)


def test_fare_uses_integer_minor_units_and_minimum():
    started = datetime(2026, 11, 26, 12, 0, tzinfo=KYIV_OFFSET)
    fare = fare_minor(distance_km=Decimal("0"), duration_min=1, tariff="standard", started_at=started)
    assert fare == 2500  # minimum, not float arithmetic
    assert isinstance(fare, int)


def test_fare_night_surcharge_when_ride_crosses_midnight():
    started = datetime(2026, 11, 26, 22, 30, tzinfo=KYIV_OFFSET)
    finished = datetime(2026, 11, 27, 0, 30, tzinfo=KYIV_OFFSET)
    day = fare_minor(distance_km=Decimal("5"), duration_min=10, tariff="standard", started_at=started)
    night = fare_minor(
        distance_km=Decimal("5"), duration_min=10, tariff="standard",
        started_at=started, finished_at=finished,
    )
    assert night > day


def test_night_boundaries_are_half_open():
    begin = datetime(2026, 11, 26, 23, 0, tzinfo=KYIV_OFFSET)
    end_next = datetime(2026, 11, 27, 6, 0, tzinfo=KYIV_OFFSET)
    assert not night_overlap(begin - timedelta(seconds=1), begin)
    assert night_overlap(begin, begin + timedelta(minutes=1))
    assert night_overlap(end_next - timedelta(minutes=1), end_next + timedelta(minutes=1))
    assert not night_overlap(end_next, end_next + timedelta(minutes=1))


def test_cursor_roundtrip_and_tamper_detection():
    stamp = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    cursor = encode_cursor(stamp, "ride-1")
    decoded_stamp, ride_id = decode_cursor(cursor)
    assert ride_id == "ride-1"
    assert decoded_stamp == stamp
    with pytest.raises(ValueError):
        decode_cursor(cursor[:-2] + "xx")


def test_fingerprint_is_canonical():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_state_machine():
    assert can_transition("requested", "assigned")
    assert can_transition("assigned", "riding")
    assert can_transition("riding", "finished")
    assert not can_transition("finished", "riding")
    assert not can_transition("cancelled", "assigned")


def test_password_hash_and_token_hash():
    encoded = hash_password("pass123")
    assert verify_password("pass123", encoded)
    assert not verify_password("wrong", encoded)
    assert len(token_hash("abc")) == 64
