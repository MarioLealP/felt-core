"""felt_core.meru (D127): her Meru certificate against a stand-in drive-rag that requires one, and
the per-callee Authentik tokens (client credentials with her pod's own token as the assertion)."""

import shutil
import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from felt_core import meru

pytestmark = pytest.mark.asyncio
NAME = "drive-rag.drive-rag.svc"
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


def _openssl(*a, cwd):
    subprocess.run(["openssl", *a], cwd=cwd, check=True, capture_output=True)


def _ca(d, name):
    _openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
        "-keyout", f"{name}.key", "-subj", f"/CN={name}", "-days", "2", "-out", f"{name}.crt",
        "-addext", "basicConstraints=critical,CA:true",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        cwd=d,
    )  # fmt: skip


def _leaf(d, name, issuer, dns):
    _openssl(
        "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
        "-keyout", f"{name}.key", "-subj", f"/CN={dns}", "-out", f"{name}.csr",
        cwd=d,
    )  # fmt: skip
    (d / f"{name}.ext").write_text(
        f"basicConstraints=critical,CA:false\nextendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=DNS:{dns}\n"
    )
    _openssl(
        "x509", "-req", "-in", f"{name}.csr", "-CA", f"{issuer}.crt", "-CAkey", f"{issuer}.key",
        "-CAcreateserial", "-days", "1", "-extfile", f"{name}.ext", "-out", f"{name}.crt",
        cwd=d,
    )  # fmt: skip


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """Meru's CA, a stranger's CA, drive-rag's + felt-core's certs, felt-core's mounted tls dir."""
    d = tmp_path_factory.mktemp("pki")
    _ca(d, "meru")
    _ca(d, "wrong")
    _leaf(d, "drive", "meru", NAME)
    _leaf(d, "rogue", "wrong", NAME)
    _leaf(d, "core", "meru", "felt-core.felt-core.svc")
    tls = d / "tls"
    tls.mkdir()
    shutil.copy(d / "meru.crt", tls / "ca.crt")
    shutil.copy(d / "core.crt", tls / "tls.crt")
    shutil.copy(d / "core.key", tls / "tls.key")
    return d


def _server(pki, cert):
    """A stand-in drive-rag: client cert required (Meru's CA), records the peer's DNS names."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pki / f"{cert}.crt"), str(pki / f"{cert}.key"))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(str(pki / "meru.crt"))
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(4)
    seen = []

    def loop():
        while True:
            c, _ = ls.accept()
            try:
                with ctx.wrap_socket(c, server_side=True) as s:
                    p = s.getpeercert() or {}
                    seen.append([v for k, v in p.get("subjectAltName", ()) if k == "DNS"])
                    s.recv(4096)
                    s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=loop, daemon=True).start()
    return ls.getsockname()[1], seen


async def _get(cfg, port):
    """Connect to 127.0.0.1 but verify the server as drive-rag.drive-rag.svc (as cluster DNS)."""
    async with meru.meru_client(cfg, f"https://{NAME}:{port}", timeout=5) as c:
        r = await c.get(f"https://127.0.0.1:{port}/", extensions={"sni_hostname": NAME})
        return r.status_code


@needs_openssl
async def test_mtls_with_her_meru_cert(pki):
    port, seen = _server(pki, "drive")
    assert await _get(SimpleNamespace(meru_tls_dir=str(pki / "tls")), port) == 200
    assert seen[-1] == ["felt-core.felt-core.svc"]


@needs_openssl
async def test_server_outside_merus_ca_refused(pki):
    port, _ = _server(pki, "rogue")
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _get(SimpleNamespace(meru_tls_dir=str(pki / "tls")), port)


async def test_compose_keeps_the_felt_client(monkeypatch):
    seen = []
    monkeypatch.setattr(meru, "felt_client", lambda config, url, **kw: seen.append(url) or "felt")
    cfg = SimpleNamespace(meru_tls_dir=None)
    assert meru.meru_client(cfg, "https://drive-rag.felt.internal:8000") == "felt"
    assert seen == ["https://drive-rag.felt.internal:8000"]


# --- tokens ---------------------------------------------------------------------------------


@pytest.fixture
def authentik(monkeypatch, tmp_path):
    """A stand-in token endpoint; returns (config, the requests it saw, the mounted login)."""
    meru._cache.clear()
    seen = []

    def handler(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen.append(form)
        return httpx.Response(200, json={"access_token": f"tok-{len(seen)}", "expires_in": 600})

    orig = httpx.AsyncClient
    monkeypatch.setattr(meru.httpx, "AsyncClient", lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
    login = tmp_path / "token"
    login.write_text("sa-token-1\n")
    cfg = SimpleNamespace(authentik_token_file=str(login), authentik_token_url="https://auth.test/token/")
    return cfg, seen, login


async def test_no_login_no_token():
    cfg = SimpleNamespace(authentik_token_file=None)
    assert await meru.auth_headers(cfg, "drive-rag") == {}


async def test_client_credentials_with_her_pod_token(authentik):
    cfg, seen, _ = authentik
    assert await meru.auth_headers(cfg, "drive-rag") == {"Authorization": "Bearer tok-1"}
    assert seen == [
        {
            "grant_type": "client_credentials",
            "client_id": "drive-rag",
            "scope": "drive-rag",
            "client_assertion_type": meru.ASSERTION,
            "client_assertion": "sa-token-1",
        }
    ]


async def test_cached_per_audience_until_nearly_expired(authentik, monkeypatch):
    cfg, seen, login = authentik
    now = [1000.0]
    monkeypatch.setattr(meru.time, "monotonic", lambda: now[0])
    assert await meru.access_token(cfg, "drive-rag") == "tok-1"
    assert await meru.access_token(cfg, "drive-rag") == "tok-1"
    assert await meru.access_token(cfg, "poe2rag") == "tok-2"
    login.write_text("sa-token-2")  # the kubelet swapped in a fresh one
    now[0] += 600 - meru.EARLY_S
    assert await meru.access_token(cfg, "drive-rag") == "tok-3"
    assert seen[-1]["client_assertion"] == "sa-token-2"
    assert len(seen) == 3
