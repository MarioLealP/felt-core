"""felt_core.felt_tls.voice_client against a real test PKI shaped like
clockmost's (root -> name-constrained pki_felt-style intermediate -> leaves)
and a stand-in felt-voice that requires a client cert (WP-62 M2b B5.1)."""

import shutil
import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace

import httpx
import pytest

from felt_core.felt_tls import voice_client

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
NAME = "felt-voice.felt.internal"


def _openssl(*a, cwd):
    subprocess.run(["openssl", *a], cwd=cwd, check=True, capture_output=True)


def _leaf(d, name, issuer, ext):
    _openssl(
        "req",
        "-new",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        f"{name}.key",
        "-subj",
        f"/CN={name}",
        "-out",
        f"{name}.csr",
        cwd=d,
    )
    (d / f"{name}.ext").write_text(ext)
    _openssl(
        "x509",
        "-req",
        "-in",
        f"{name}.csr",
        "-CA",
        f"{issuer}.pem",
        "-CAkey",
        f"{issuer}.key",
        "-CAcreateserial",
        "-days",
        "1",
        "-extfile",
        f"{name}.ext",
        "-out",
        f"{name}.pem",
        cwd=d,
    )


def _bundle(d, name, issuer):
    return (
        (d / f"{name}.key").read_text()
        + (d / f"{name}.pem").read_text()
        + (d / f"{issuer}.pem").read_text()
    )


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    for ca in ("root", "wrong"):
        _openssl(
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            f"{ca}.key",
            "-subj",
            f"/CN={ca}",
            "-days",
            "2",
            "-out",
            f"{ca}.pem",
            cwd=d,
        )
    _leaf(
        d,
        "feltint",
        "root",
        "basicConstraints=critical,CA:true,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "nameConstraints=critical,permitted;URI:clockmost.internal,permitted;DNS:felt.internal\n",
    )
    srv = "basicConstraints=critical,CA:false\nextendedKeyUsage=serverAuth\nsubjectAltName="
    cli = "basicConstraints=critical,CA:false\nextendedKeyUsage=clientAuth\nsubjectAltName="
    _leaf(
        d,
        "voice",
        "feltint",
        srv + f"DNS:{NAME},URI:spiffe://clockmost.internal/workload/felt-voice\n",
    )
    _leaf(
        d,
        "notvoice",
        "feltint",
        srv + "DNS:tray.felt.internal,URI:spiffe://clockmost.internal/workload/tray\n",
    )
    _leaf(d, "rogue", "wrong", srv + f"DNS:{NAME}\n")
    for n in ("core", "core2"):
        _leaf(
            d,
            n,
            "feltint",
            cli
            + "DNS:felt-core.felt.internal,URI:spiffe://clockmost.internal/workload/felt-core\n",
        )
    ident = d / "identity"
    (ident / "client" / "current").mkdir(parents=True)
    shutil.copy(d / "feltint.pem", ident / "felt-ca.pem")
    (ident / "client" / "current" / "bundle.pem").write_text(_bundle(d, "core", "feltint"))
    return d


def _server(pki, cert_name, issuer):
    """A stand-in felt-voice: TLS 1.3, client cert required (pki_felt anchor),
    records the peer's URI + serial, answers 200."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    (pki / f"{cert_name}.bundle").write_text(_bundle(pki, cert_name, issuer))
    ctx.load_cert_chain(str(pki / f"{cert_name}.bundle"))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(str(pki / "feltint.pem"))
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(4)
    seen = []

    def loop():
        while True:
            c, _ = ls.accept()
            try:
                with ctx.wrap_socket(c, server_side=True) as s:
                    p = s.getpeercert()
                    if p is None:  # CERT_REQUIRED: can't happen, but don't guess
                        continue
                    seen.append(
                        ([v for k, v in p["subjectAltName"] if k == "URI"], p["serialNumber"])
                    )
                    s.recv(4096)
                    s.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                    )
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=loop, daemon=True).start()
    return ls.getsockname()[1], seen


def _cfg(pki, port, identity=True):
    return SimpleNamespace(
        voice_url=f"https://{NAME}:{port}", identity_dir=str(pki / "identity") if identity else None
    )


async def _get(cfg):
    """Connect to 127.0.0.1 but verify the server as felt-voice.felt.internal
    (in production the compose extra_hosts entry does the resolving)."""
    port = cfg.voice_url.rsplit(":", 1)[1]
    async with voice_client(cfg, timeout=5) as c:
        r = await c.get(f"https://127.0.0.1:{port}/", extensions={"sni_hostname": NAME})
        return r.status_code


def _cert_error(e):
    return "CERTIFICATE_VERIFY_FAILED" in str(e) or "certificate" in str(e).lower()


@pytest.mark.asyncio
async def test_mtls_to_felt_voice(pki):
    port, seen = _server(pki, "voice", "feltint")
    assert await _get(_cfg(pki, port)) == 200
    assert seen[-1][0] == ["spiffe://clockmost.internal/workload/felt-core"]


@pytest.mark.asyncio
async def test_renewed_client_cert_used_next_call(pki):
    port, seen = _server(pki, "voice", "feltint")
    await _get(_cfg(pki, port))
    first = seen[-1][1]
    (pki / "identity" / "client" / "current" / "bundle.pem").write_text(
        _bundle(pki, "core2", "feltint")
    )
    await _get(_cfg(pki, port))
    assert seen[-1][1] != first  # no restart needed (D5)
    (pki / "identity" / "client" / "current" / "bundle.pem").write_text(
        _bundle(pki, "core", "feltint")
    )


@pytest.mark.asyncio
async def test_wrong_server_name_refused(pki):
    port, _ = _server(pki, "notvoice", "feltint")
    with pytest.raises(httpx.ConnectError) as e:
        await _get(_cfg(pki, port))
    assert _cert_error(e.value), e.value


@pytest.mark.asyncio
async def test_server_from_another_ca_refused(pki):
    port, _ = _server(pki, "rogue", "wrong")
    with pytest.raises(httpx.ConnectError) as e:
        await _get(_cfg(pki, port))
    assert _cert_error(e.value), e.value


@pytest.mark.asyncio
async def test_https_without_identity_fails_closed(pki):
    port, _ = _server(pki, "voice", "feltint")
    with pytest.raises(httpx.ConnectError) as e:  # system trust store: never silently unverified
        await _get(_cfg(pki, port, identity=False))
    assert _cert_error(e.value), e.value


def test_plain_http_unchanged():
    c = voice_client(SimpleNamespace(voice_url="http://192.168.10.104:8421", identity_dir="/nope"))
    assert isinstance(c, httpx.AsyncClient)  # no cert files touched for plain HTTP
