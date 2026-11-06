import asyncio
import re

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text

from tests.conftest import PASSWORD, REDIS_URL
from vigisift.domain.operator import Role
from vigisift.features.auth import repository
from vigisift.features.auth.origin import same_origin
from vigisift.features.auth.passwords import hash_password
from vigisift.runtime import Runtime

# The only paths open to people who aren't signed in.
PUBLIC = {
    "/",
    "/auth/login",
    "/auth/logout",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


async def test_every_route_except_login_needs_a_session(
    app: FastAPI, anonymous: AsyncClient
) -> None:
    """A route added later without a lock turns this test red. Walks the OpenAPI schema
    (FastAPI's list of public routes); the WebSocket has its own test."""
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if path in PUBLIC:
            continue
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        for method in operations:
            response = await anonymous.request(method.upper(), concrete, json={})
            assert response.status_code == 401, f"{method.upper()} {path} is open to strangers"
            checked += 1
    assert checked >= 8


async def test_logging_in_sets_a_hardened_cookie(anonymous: AsyncClient) -> None:
    response = await anonymous.post("/auth/login", json={"username": "anna", "password": PASSWORD})

    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie, "JavaScript (and XSS) must not read the session cookie"
    assert "samesite=strict" in cookie
    me = (await anonymous.get("/auth/me")).json()
    assert (me["username"], me["role"]) == ("anna", "operator")


async def test_a_wrong_password_and_an_unknown_user_look_the_same(anonymous: AsyncClient) -> None:
    wrong = await anonymous.post("/auth/login", json={"username": "anna", "password": "nope"})
    ghost = await anonymous.post("/auth/login", json={"username": "ghost", "password": "nope"})

    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json() == ghost.json(), "must not reveal which usernames are real"


async def test_guessing_locks_the_account_even_for_the_right_password(
    anonymous: AsyncClient, runtime: Runtime
) -> None:
    for _ in range(runtime.settings.login_max_failures):
        await anonymous.post("/auth/login", json={"username": "anna", "password": "guess"})

    right = await anonymous.post("/auth/login", json={"username": "anna", "password": PASSWORD})

    assert right.status_code == 429, (
        "while locked, even the right password isn't confirmed as right"
    )


async def test_logging_out_kills_the_session_server_side(client: AsyncClient) -> None:
    stolen = client.cookies["vigisift_session"]

    await client.post("/auth/logout")
    client.cookies.set("vigisift_session", stolen)

    assert (await client.get("/auth/me")).status_code == 401, "the old cookie must be useless"


async def test_disabling_an_operator_takes_effect_on_the_next_request(
    client: AsyncClient, runtime: Runtime
) -> None:
    async with runtime.session() as session:
        await session.execute(text("UPDATE operators SET disabled = TRUE WHERE username = 'anna'"))

    assert (await client.get("/alerts")).status_code == 401


async def test_the_session_store_never_holds_the_raw_token(client: AsyncClient) -> None:
    token = client.cookies["vigisift_session"]
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    keys = [key async for key in redis.scan_iter("session:*")]
    await redis.aclose()

    assert keys and all(token not in key for key in keys)


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"origin": "http://test"}, True),
        ({}, True),
        ({"origin": "https://evil.example"}, False),
        ({"sec-fetch-site": "cross-site"}, False),
    ],
)
async def test_writes_from_another_site_are_refused(
    client: AsyncClient, headers: dict[str, str], allowed: bool
) -> None:
    response = await client.post("/auth/logout", headers=headers)

    assert (response.status_code != 403) is allowed


async def test_a_burst_of_parallel_guesses_gets_no_more_tries_than_the_limit(
    anonymous: AsyncClient, runtime: Runtime
) -> None:
    """Attempts count BEFORE the check: 50 concurrent requests still get only 5 attempts."""
    limit = runtime.settings.login_max_failures
    responses = await asyncio.gather(
        *(
            anonymous.post("/auth/login", json={"username": "anna", "password": f"guess-{i}"})
            for i in range(50)
        )
    )

    codes = [r.status_code for r in responses]
    assert codes.count(401) == limit, "exactly this many times the password was really checked"
    assert codes.count(429) == 50 - limit


async def test_one_operators_typos_do_not_lock_out_a_colleague_on_the_same_network(
    anonymous: AsyncClient, runtime: Runtime
) -> None:
    for _ in range(runtime.settings.login_max_failures):
        await anonymous.post("/auth/login", json={"username": "anna", "password": "typo"})

    colleague = await anonymous.post("/auth/login", json={"username": "boss", "password": PASSWORD})

    assert colleague.status_code == 204, "a whole center behind a NAT shares one IP"


async def test_resetting_a_password_logs_out_every_open_session(
    client: AsyncClient, runtime: Runtime
) -> None:
    async with runtime.session() as session:
        await repository.upsert(
            session, "anna", "Anna", hash_password("a new long password"), Role.OPERATOR
        )

    assert (await client.get("/auth/me")).status_code == 401, "a stolen cookie must be useless"


async def test_a_session_in_constant_use_still_has_a_maximum_age(
    client: AsyncClient, runtime: Runtime
) -> None:
    assert (await client.get("/auth/me")).status_code == 200
    runtime.settings.session_max_age_sec = 1
    await asyncio.sleep(1.1)

    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://hub.example.com", True),
        ("https://hub.example.com/", True),
        ("http://hub.example.com", False),
        ("https://frigate.example.com", False),
        (None, True),
    ],
)
def test_behind_a_proxy_origin_is_compared_with_the_public_origin(
    origin: str | None, allowed: bool
) -> None:
    """The proxy changes Host to api:8000; the browser's Origin is what can be trusted."""
    assert same_origin(origin, None, "api:8000", "https://hub.example.com") is allowed
