import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.main import app
from app.routes.rides import RideCreate, RidePatch


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


def test_client_facing_me_route_is_published():
    paths = app.openapi()["paths"]
    assert "/v1/me" in paths
    assert "/v1/rides" in paths


def test_old_patch_payload_is_accepted():
    assert RidePatch.model_validate({"pickup": "вул. Хрещатик, 1"}).pickup == "вул. Хрещатик, 1"


@pytest.mark.parametrize("field", ["user_id", "status", "version", "driver_id"])
def test_patch_rejects_server_owned_fields(field):
    # O3.2: a forged owner/state/version is a client error, not silently ignored.
    with pytest.raises(ValidationError):
        RidePatch.model_validate({"pickup": "а", field: "змінено"})


def test_old_client_without_tariff_defaults_to_standard():
    # O6.2: a saved request from the previous client version stays valid.
    ride = RideCreate.model_validate({"district": "central", "pickup": "вул. Хрещатик, 1"})
    assert ride.tariff == "standard"
    assert str(ride.distance_km) == "2.5"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(200, "succeeded"), (422, "declined"), (503, "unavailable")],
)
def test_provider_result_classes(monkeypatch, status_code, expected):
    # O4.3: business decline and technical unavailability are different outcomes.
    from app import worker

    monkeypatch.setattr(
        worker, "settings", SimpleNamespace(sim_url="http://sim", worker_retries=0)
    )
    monkeypatch.setattr(worker.httpx, "post", lambda *a, **kw: FakeResponse(status_code))
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    state, _ = worker._call_provider("op-1", 100, "UAH")
    assert state == expected


def test_xautoclaim_reclaims_messages_for_both_redis_py_shapes():
    # redis-py 5 returns a list; older releases a tuple. Both must be retried.
    from app.worker import claimed_items

    message = ("1-0", {"event_id": "e-1", "event_type": "payment.requested"})
    assert claimed_items(["0-0", [message], []]) == [message]
    assert claimed_items(("0-0", [message], [])) == [message]
    assert claimed_items(["0-0", [], []]) == []
    assert claimed_items(None) == []


def test_gateway_message_has_seq_and_is_not_applied_twice():
    # O5.3 / O6.2: an event published by the new producer is consumed by the
    # gateway with a monotonic seq, and a redelivery is not shown twice.
    import gateway.main as gateway

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)
            return True

    connection = gateway.Connection(socket=FakeSocket(), token_hash="h")
    gateway.connections["u-test"] = {connection}
    fields = {
        "event_type": "ride.assigned",
        "payload": json.dumps(
            {
                "user_id": "u-test",
                "notification_id": "n-1",
                "seq": 7,
                "body": {"ride_id": "r-1"},
            }
        ),
    }
    try:
        asyncio.run(gateway._deliver(fields))
        asyncio.run(gateway._deliver(fields))
    finally:
        gateway.connections.pop("u-test", None)
    assert len(connection.socket.sent) == 1
    assert connection.socket.sent[0]["seq"] == 7
    assert connection.socket.sent[0]["id"] == "n-1"
