from urllib.parse import urlsplit


def same_origin(
    origin: str | None, fetch_site: str | None, host: str | None, public_origin: str | None
) -> bool:
    """Whether a request (or WebSocket) comes from vigisift's own UI.

    Browsers always send `Origin` with writes and WebSockets; non-browser clients
    (curl, scripts) don't, and don't carry anyone's cookie either. Behind a reverse
    proxy, the proxy often changes `Host`, so set `public_origin`
    (for example https://vigisift.example.com) to compare both scheme and name.
    """
    if fetch_site == "cross-site":
        return False
    if origin is None:
        return True
    if public_origin is not None:
        return origin.rstrip("/") == public_origin.rstrip("/")
    return urlsplit(origin).netloc == host
