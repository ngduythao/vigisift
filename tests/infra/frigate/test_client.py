import httpx
import pytest

from vigisift.infra.frigate.client import HttpFrigateApi
from vigisift.ports.frigate import (
    FrigateClipMissingError,
    FrigateRejectedError,
    FrigateSite,
    FrigateUnavailableError,
)

SITE = FrigateSite("demo", "https://frigate.test:8971/")
# Frigate returns the JWT in a cookie with the Secure and HttpOnly flags.
LOGIN_OK = httpx.Response(
    200, headers={"set-cookie": "frigate_token=jwt-1; HttpOnly; Path=/; Secure; SameSite=lax"}
)


class Frigate:
    """A fake Frigate over HTTP: records every request, returns scripted responses in turn."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def api(self, credentials: dict[str, tuple[str, str]] | None = None) -> HttpFrigateApi:
        return HttpFrigateApi(credentials or {}, transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


async def _reviews(api: HttpFrigateApi) -> list[dict[str, object]]:
    return await api.reviews(SITE, after=100.0, before=200.0, limit=50)


async def test_without_credentials_the_internal_port_is_called_directly() -> None:
    frigate = Frigate(httpx.Response(200, json=[{"id": "r"}]))

    rows = await _reviews(frigate.api())

    [request] = frigate.requests
    assert rows == [{"id": "r"}]
    assert "authorization" not in request.headers
    assert request.url.path == "/api/review"
    assert dict(request.url.params) == {"after": "100.0", "before": "200.0", "limit": "50"}


async def test_with_credentials_it_logs_in_once_and_sends_a_bearer_token() -> None:
    frigate = Frigate(LOGIN_OK, httpx.Response(200, json=[]), httpx.Response(200, json=[]))
    api = frigate.api({"demo": ("vigisift", "s3cret")})

    await _reviews(api)
    await _reviews(api)

    assert frigate.paths() == ["POST /api/login", "GET /api/review", "GET /api/review"]
    assert frigate.requests[1].headers["authorization"] == "Bearer jwt-1"
    assert frigate.requests[2].headers["authorization"] == "Bearer jwt-1", "the token is kept"


async def test_an_expired_token_triggers_exactly_one_new_login() -> None:
    relogin = httpx.Response(200, headers={"set-cookie": "frigate_token=jwt-2; Secure"})
    frigate = Frigate(
        LOGIN_OK, httpx.Response(401), relogin, httpx.Response(200, json=[{"id": "r"}])
    )

    rows = await _reviews(frigate.api({"demo": ("vigisift", "s3cret")}))

    assert rows == [{"id": "r"}]
    assert frigate.requests[-1].headers["authorization"] == "Bearer jwt-2"


async def test_still_unauthorised_after_a_new_login_is_an_error() -> None:
    frigate = Frigate(LOGIN_OK, httpx.Response(401), LOGIN_OK, httpx.Response(401))

    with pytest.raises(FrigateUnavailableError):
        await _reviews(frigate.api({"demo": ("vigisift", "s3cret")}))
    assert frigate.paths().count("POST /api/login") == 2, "no repeated sign-ins"


async def test_a_refused_login_does_not_leak_the_password() -> None:
    frigate = Frigate(httpx.Response(401, json={"message": "Login failed"}))

    with pytest.raises(FrigateUnavailableError) as error:
        await _reviews(frigate.api({"demo": ("vigisift", "s3cret")}))
    assert "s3cret" not in str(error.value)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"message": "not a list"}),
        httpx.Response(200, text="<html>proxy error</html>"),
        httpx.Response(502),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
    ],
)
async def test_anything_but_a_list_of_reviews_is_unavailable(
    reply: httpx.Response | Exception,
) -> None:
    with pytest.raises(FrigateUnavailableError):
        await _reviews(Frigate(reply).api())


async def _push(api: HttpFrigateApi) -> None:
    await api.set_review_rules(SITE, "shop_aisle", prompt="p", concerns=[])


async def test_a_refused_rule_push_is_rejected_not_retried() -> None:
    with pytest.raises(FrigateRejectedError) as error:
        await _push(Frigate(httpx.Response(400, json={"message": "bad camera"})).api())
    assert "bad camera" in str(error.value)


async def test_a_missing_admin_role_is_retried_once_it_is_fixed() -> None:
    with pytest.raises(FrigateUnavailableError) as error:
        await _push(Frigate(httpx.Response(403, json={"message": "Forbidden"})).api())
    assert "admin" in str(error.value)


@pytest.mark.parametrize("status", [404, 408, 429, 503])
async def test_a_busy_or_misrouted_frigate_is_unavailable_so_the_push_is_retried(
    status: int,
) -> None:
    with pytest.raises(FrigateUnavailableError):
        await _push(Frigate(httpx.Response(status)).api())


async def test_a_refused_read_is_unavailable_not_a_rejected_change() -> None:
    with pytest.raises(FrigateUnavailableError):
        await _reviews(Frigate(httpx.Response(400, json={"message": "bad query"})).api())


async def _clip(api: HttpFrigateApi, review_id: str = "1.0-a") -> bytes:
    return b"".join([chunk async for chunk in api.review_clip(SITE, review_id)])


async def test_a_clip_is_fetched_with_padding_and_an_escaped_review_id() -> None:
    frigate = Frigate(LOGIN_OK, httpx.Response(200, content=b"mp4-bytes"))

    body = await _clip(frigate.api({"demo": ("vigisift", "s3cret")}), "a/b")

    request = frigate.requests[1]
    assert body == b"mp4-bytes"
    assert request.url.raw_path == b"/api/review/a%2Fb/clip.mp4?padding=2"
    assert request.headers["authorization"] == "Bearer jwt-1"


async def test_an_expired_token_on_a_clip_signs_in_again_once() -> None:
    relogin = httpx.Response(200, headers={"set-cookie": "frigate_token=jwt-2; Secure"})
    frigate = Frigate(LOGIN_OK, httpx.Response(401), relogin, httpx.Response(200, content=b"mp4"))

    assert await _clip(frigate.api({"demo": ("vigisift", "s3cret")})) == b"mp4"
    assert frigate.requests[3].headers["authorization"] == "Bearer jwt-2"


@pytest.mark.parametrize("status", [400, 404])
async def test_no_recording_is_a_missing_clip(status: int) -> None:
    frigate = Frigate(httpx.Response(status, json={"message": "No recordings"}))

    with pytest.raises(FrigateClipMissingError):
        await _clip(frigate.api())


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.Response(500), httpx.Response(403)]
)
async def test_a_failing_frigate_is_not_a_missing_clip(failure: httpx.Response | Exception) -> None:
    with pytest.raises(FrigateUnavailableError):
        await _clip(Frigate(failure).api())
