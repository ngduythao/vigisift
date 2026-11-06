import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from tests.conftest import DATABASE_URL, PASSWORD, PASSWORD_HASH, SEED
from tests.fakes.ports import FakeFrigateApi, FakeMqttClient
from vigisift.app import create_app
from vigisift.config import Settings


def _reset() -> None:
    engine = create_sync_engine(DATABASE_URL.replace("+asyncpg", "+psycopg"), future=True)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE devices, telemetry, commands CASCADE"))
        for device_id, name, room, kind, state in SEED:
            connection.execute(
                text(
                    "INSERT INTO devices (id, name, room, kind, state, updated_at)"
                    " VALUES (:id, :name, :room, :kind, :state, NOW())"
                ),
                {"id": device_id, "name": name, "room": room, "kind": kind, "state": state},
            )
        connection.execute(
            text(
                "INSERT INTO operators (username, display_name, password_hash, role)"
                " VALUES ('anna', 'Anna', :hash, 'operator') ON CONFLICT (username) DO NOTHING"
            ),
            {"hash": PASSWORD_HASH},
        )
    engine.dispose()


def test_a_browser_sees_telemetry_without_polling(settings: Settings) -> None:
    _reset()
    mqtt = FakeMqttClient()
    app = create_app(settings, mqtt, frigate=FakeFrigateApi())

    with TestClient(app) as http:
        assert http.post("/auth/login", json={"username": "anna", "password": PASSWORD}).is_success
        with http.websocket_connect("/ws/telemetry") as socket:
            mqtt.deliver("device/lamp-1/telemetry", json.dumps({"state": "OFF", "lux": 12}))
            pushed = json.loads(socket.receive_text())

    assert pushed["device_id"] == "lamp-1"
    assert pushed["state"] == "OFF"
    assert pushed["lux"] == 12


def test_the_event_stream_needs_a_login(settings: Settings) -> None:
    _reset()
    app = create_app(settings, FakeMqttClient(), frigate=FakeFrigateApi())

    with (
        TestClient(app) as http,
        pytest.raises(WebSocketDisconnect) as closed,
        http.websocket_connect("/ws/telemetry") as socket,
    ):
        socket.receive_text()

    assert closed.value.code == 1008, "signed out means hearing no alerts"


def test_a_page_from_another_origin_cannot_open_the_stream(settings: Settings) -> None:
    _reset()
    app = create_app(settings, FakeMqttClient(), frigate=FakeFrigateApi())

    with TestClient(app) as http:
        assert http.post("/auth/login", json={"username": "anna", "password": PASSWORD}).is_success
        with (
            pytest.raises(WebSocketDisconnect) as closed,
            http.websocket_connect(
                "/ws/telemetry", headers={"origin": "https://evil.example"}
            ) as ws,
        ):
            ws.receive_text()

    assert closed.value.code == 1008


def test_logging_out_also_closes_the_open_stream(settings: Settings) -> None:
    _reset()
    app = create_app(settings, FakeMqttClient(), frigate=FakeFrigateApi())

    with TestClient(app) as http:
        assert http.post("/auth/login", json={"username": "anna", "password": PASSWORD}).is_success
        with http.websocket_connect("/ws/telemetry") as ws:
            http.post("/auth/logout")
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()

    assert closed.value.code == 1008
