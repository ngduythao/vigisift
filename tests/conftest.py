from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text

from tests.fakes.ports import FakeFrigateApi, FakeMqttClient
from vigisift.app import create_app
from vigisift.config import Settings
from vigisift.features.alerts.repository import upsert_site
from vigisift.features.auth.passwords import hash_password
from vigisift.infra.db.database import apply_schema, create_engine
from vigisift.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
# Separate database, Valkey DB and channel: tests TRUNCATE tables before each run,
# so they must not touch data from an app run by hand or from `make demo`.
TEST_DATABASE = "vigisift_test"
DATABASE_URL = f"postgresql+asyncpg://postgres:postgres@127.0.0.1:5434/{TEST_DATABASE}"
REDIS_URL = "redis://127.0.0.1:6380/15"
EVENTS_CHANNEL = "vigisift:test-events"

# A sample Frigate site. The addresses needn't be real: tests don't call Frigate.
SITES = [("demo", "Demo store", "http://frigate.test:8971", "http://frigate.test:5000")]

# Two sample accounts with one password. Hashed once for the whole test session:
# argon2 is deliberately slow.
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)
OPERATORS = [("anna", "Anna", "operator"), ("boss", "Admin", "admin")]

SEED = [
    ("lamp-1", "Ceiling light", "living room", "light", "ONLINE"),
    ("fan-1", "Fan", "bedroom", "fan", "OFF"),
    ("plug-1", "TV plug", "living room", "plug", "OFFLINE"),
]


@pytest.fixture(scope="session", autouse=True)
def test_database() -> None:
    """Create the test database if missing (Postgres in compose only creates `vigisift`)."""
    admin = "postgresql://postgres:postgres@127.0.0.1:5434/postgres"
    with psycopg.connect(admin, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)
        ).fetchone()
        if exists is None:
            connection.execute(f'CREATE DATABASE "{TEST_DATABASE}"')


@pytest.fixture
def settings() -> Settings:
    # Don't read .env: the suite must give the same result on every machine, even one
    # with an old .env or pointing at other services.
    return Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        # httpx sends cookies over http://test; the real build defaults to Secure.
        session_cookie_secure=False,
        ws_session_check_sec=0.2,
        redis_url=REDIS_URL,
        events_channel=EVENTS_CHANNEL,
        # No real Ollama calls; re-check tests call check_pending with a fake model.
        rule_check_enabled=False,
    )


@pytest.fixture
def mqtt() -> FakeMqttClient:
    return FakeMqttClient()


@pytest.fixture(autouse=True)
async def clean_redis() -> AsyncIterator[None]:
    redis = Redis.from_url(REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest.fixture
def frigate() -> FakeFrigateApi:
    return FakeFrigateApi()


@pytest.fixture
async def app(
    settings: Settings,
    mqtt: FakeMqttClient,
    frigate: FakeFrigateApi,
) -> AsyncIterator[FastAPI]:
    engine = create_engine(settings)
    await apply_schema(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE devices, telemetry, commands, llm_calls, camera_alerts, sites,"
                " alert_verdicts, camera_rules, operators RESTART IDENTITY CASCADE"
            )
        )
        for username, display_name, role in OPERATORS:
            await connection.execute(
                text(
                    "INSERT INTO operators (username, display_name, password_hash, role)"
                    " VALUES (:username, :name, :hash, :role)"
                ),
                {"username": username, "name": display_name, "hash": PASSWORD_HASH, "role": role},
            )
        for site in SITES:
            await upsert_site(connection, *site)
        for device_id, name, room, kind, state in SEED:
            await connection.execute(
                text(
                    "INSERT INTO devices (id, name, room, kind, state, updated_at)"
                    " VALUES (:id, :name, :room, :kind, :state, NOW())"
                ),
                {"id": device_id, "name": name, "room": room, "kind": kind, "state": state},
            )
    await engine.dispose()

    app = create_app(settings, mqtt, frigate=frigate)
    async with app.router.lifespan_context(app):
        yield app


async def login(http: AsyncClient, username: str) -> None:
    response = await http.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 204, response.text


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that isn't signed in."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client signed in as operator."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, "anna")
        yield http


@pytest.fixture
async def admin(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client signed in as admin."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, "boss")
        yield http


@pytest.fixture
def runtime(app: FastAPI) -> Runtime:
    runtime: Runtime = app.state.runtime
    return runtime
