"""Calling a site's Frigate over HTTPS signed by a private CA (`make certs`, FRIGATE_CA_FILE)."""

import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.conftest import ROOT
from vigisift.infra.frigate.client import HttpFrigateApi
from vigisift.ports.frigate import FrigateSite, FrigateUnavailableError

CERTS = ROOT / "deploy" / "certs"

pytestmark = pytest.mark.skipif(
    not (CERTS / "frigate.crt").exists(), reason="no dev certificates: run `make certs`"
)


class _Reviews(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps([]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def https_frigate() -> Iterator[FrigateSite]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Reviews)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(CERTS / "frigate.crt", CERTS / "frigate.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FrigateSite("remote", f"https://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


async def test_a_frigate_signed_by_the_given_ca_is_trusted(https_frigate: FrigateSite) -> None:
    api = HttpFrigateApi({}, ca_file=str(CERTS / "ca.crt"))
    try:
        assert await api.reviews(https_frigate, after=0, before=1, limit=1) == []
    finally:
        await api.aclose()


async def test_without_the_ca_the_certificate_is_refused(https_frigate: FrigateSite) -> None:
    api = HttpFrigateApi({})
    try:
        with pytest.raises(FrigateUnavailableError, match="ConnectError"):
            await api.reviews(https_frigate, after=0, before=1, limit=1)
    finally:
        await api.aclose()
