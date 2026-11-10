import ssl
from collections.abc import AsyncIterator, Mapping
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote

import httpx

from vigisift.ports.frigate import (
    FrigateClipMissingError,
    FrigateRejectedError,
    FrigateSite,
    FrigateUnavailableError,
)

# Frigate's default (`auth.cookie_name`). Frigate returns the JWT in this cookie;
# read it from the header directly because of the Secure flag: httpx's jar drops it over HTTP.
_TOKEN_COOKIE = "frigate_token"


# 401 (session expired), 403 (no permission), 404 (wrong address), 408 and 429 are
# delivery failures, not problems with what was sent.
_REJECTED = {400, 409, 422}

# Frigate answers 404 for an unknown review and 400 when no recording covers it.
_NO_CLIP = {400, 404}
# Seconds around the review, so the clip shows the person arriving and leaving.
_CLIP_PADDING_SEC = 2


class HttpFrigateApi:
    """The Frigate API over HTTP, one client shared by all sites.

    A site with credentials signs in (`POST /api/login`) and sends a Bearer token; the
    token is kept until a 401, then signs in again exactly once, because Frigate limits
    sign-in rate. A site without credentials is called directly: use only with internal
    port 5000 on the same network.
    """

    def __init__(
        self,
        credentials: Mapping[str, tuple[str, str]],
        timeout_sec: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
        ca_file: str | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._tokens: dict[str, str] = {}
        # HTTPS to a Frigate with a private certificate: trust that CA as well as the system's.
        verify: ssl.SSLContext | bool = True
        if ca_file is not None:
            verify = ssl.create_default_context()
            verify.load_verify_locations(ca_file)
        self._http = httpx.AsyncClient(timeout=timeout_sec, transport=transport, verify=verify)

    async def reviews(
        self, site: FrigateSite, *, after: float, before: float, limit: int
    ) -> list[dict[str, Any]]:
        params = {"after": after, "before": before, "limit": limit}
        body = await self._request(site, "GET", "/api/review", params=params)
        if not isinstance(body, list) or not all(isinstance(row, dict) for row in body):
            raise FrigateUnavailableError(f"{site.id}: /api/review did not return a list")
        return body

    async def set_review_rules(
        self, site: FrigateSite, camera: str, *, prompt: str, concerns: list[str]
    ) -> None:
        # update_topic tells Frigate's summary process to re-read the camera's
        # `review` section right away, with no restart. No query string: on this
        # endpoint the query overrides the body.
        await self._request(
            site,
            "PUT",
            "/api/config/set",
            json={
                "requires_restart": 0,
                "update_topic": f"config/cameras/{camera}/review",
                "config_data": {
                    "cameras": {
                        camera: {
                            "review": {
                                "genai": {
                                    "activity_context_prompt": prompt,
                                    "additional_concerns": concerns,
                                }
                            }
                        }
                    }
                },
            },
        )

    async def review_rules(self, site: FrigateSite) -> dict[str, tuple[str, list[str]]]:
        config = await self._request(site, "GET", "/api/config")
        cameras = config.get("cameras") if isinstance(config, dict) else None
        if not isinstance(cameras, dict):
            raise FrigateUnavailableError(f"{site.id}: /api/config has no cameras")
        rules: dict[str, tuple[str, list[str]]] = {}
        for name, camera in cameras.items():
            genai = ((camera or {}).get("review") or {}).get("genai") or {}
            rules[str(name)] = (
                str(genai.get("activity_context_prompt") or ""),
                [str(c) for c in genai.get("additional_concerns") or []],
            )
        return rules

    async def review_clip(self, site: FrigateSite, review_id: str) -> AsyncIterator[bytes]:
        url = f"{site.api_url.rstrip('/')}/api/review/{quote(review_id, safe='')}/clip.mp4"
        try:
            for may_sign_in_again in (site.id in self._credentials, False):
                async with self._http.stream(
                    "GET",
                    url,
                    params={"padding": _CLIP_PADDING_SEC},
                    headers=await self._auth(site),
                ) as response:
                    if response.status_code == 401 and may_sign_in_again:
                        self._tokens.pop(site.id, None)
                        continue
                    if response.status_code in _NO_CLIP:
                        raise FrigateClipMissingError(f"{site.id}: {review_id}")
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
                    return
        except httpx.HTTPError as error:
            raise FrigateUnavailableError(f"{site.id}: {type(error).__name__}") from error

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        site: FrigateSite,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{site.api_url.rstrip('/')}{path}"
        try:
            response = await self._http.request(
                method, url, params=params, json=json, headers=await self._auth(site)
            )
            if response.status_code == 401 and site.id in self._credentials:
                self._tokens.pop(site.id, None)
                response = await self._http.request(
                    method, url, params=params, json=json, headers=await self._auth(site)
                )
            if response.status_code == 403:
                # A configuration problem on the site, not with these rules: report it
                # as not delivered, so the next pass pushes again once fixed.
                raise FrigateUnavailableError(
                    f"{site.id}: vigisift's Frigate account needs the admin role"
                )
            if method != "GET" and response.status_code in _REJECTED:
                # Frigate rejected this change itself (bad config, no such camera...):
                # pushing it again won't help. Keep Frigate's reason short.
                raise FrigateRejectedError(f"{site.id}: {_message(response)[:300]}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise FrigateUnavailableError(f"{site.id}: {type(error).__name__}") from error
        except ValueError as error:
            raise FrigateUnavailableError(f"{site.id}: {path} is not JSON") from error

    async def _auth(self, site: FrigateSite) -> dict[str, str]:
        if site.id not in self._credentials:
            return {}
        token = self._tokens.get(site.id) or await self._login(site)
        return {"Authorization": f"Bearer {token}"}

    async def _login(self, site: FrigateSite) -> str:
        user, password = self._credentials[site.id]
        response = await self._http.post(
            f"{site.api_url.rstrip('/')}/api/login", json={"user": user, "password": password}
        )
        if response.status_code != 200:
            # No body included: it may hold things that shouldn't go in logs.
            raise FrigateUnavailableError(f"{site.id}: sign-in failed ({response.status_code})")
        cookies: SimpleCookie = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cookies.load(header)
        if _TOKEN_COOKIE not in cookies:
            raise FrigateUnavailableError(f"{site.id}: sign-in returned no token")
        token = cookies[_TOKEN_COOKIE].value
        self._tokens[site.id] = token
        return token


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "rejected"
    message = body.get("message") if isinstance(body, dict) else None
    return str(message) if message else "rejected"
