"""Felt service certs on felt-core's outbound links (WP-62 M2b B5: felt-voice D4,
drive-rag D10.2; D5).

felt-core's 2-hour Felt client cert is renewed hourly by the server-side
helper into FELT_IDENTITY_DIR (mounted read-only from
/var/lib/clockmost-vault/service/felt-core): ``client/current/bundle.pem``
(key + cert + intermediate in one file, swapped atomically) and
``felt-ca.pem`` (the pki_felt intermediate, the ONLY trust anchor for Felt
peers - never the system store or the clockmost root).

A fresh context per client, i.e. per call: a renewed cert is used from the
next call on, no restart (D5).
"""

import ssl
from pathlib import Path
from typing import Any

import certifi
import httpx


def felt_client_context(identity_dir: str) -> ssl.SSLContext:
    d = Path(identity_dir)
    ctx = ssl.create_default_context(cafile=str(d / "felt-ca.pem"))
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # the anchor is an intermediate
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(d / "client" / "current" / "bundle.pem"))
    return ctx


def machine_client(config: Any, url: str | None, **kwargs: Any) -> httpx.AsyncClient:
    """A client for one of Caddy's machine hosts (B5.3, e.g. poe2-api): Caddy
    shows its normal public certificate (verified against the public CAs, as
    httpx does by default), and she presents her Felt cert. Plain otherwise."""
    identity_dir = getattr(config, "identity_dir", None)
    if (url or "").startswith("https://") and identity_dir:
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(Path(identity_dir) / "client" / "current" / "bundle.pem"))
        return httpx.AsyncClient(verify=ctx, **kwargs)
    return httpx.AsyncClient(**kwargs)


def felt_client(config: Any, url: str | None, **kwargs: Any) -> httpx.AsyncClient:
    """A client for a Felt peer at ``url``: mTLS with her Felt cert when the URL
    is https:// and FELT_IDENTITY_DIR is set; plain otherwise (as before)."""
    identity_dir = getattr(config, "identity_dir", None)
    if (url or "").startswith("https://") and identity_dir:
        return httpx.AsyncClient(verify=felt_client_context(identity_dir), **kwargs)
    return httpx.AsyncClient(**kwargs)


def voice_client(config: Any, **kwargs: Any) -> httpx.AsyncClient:
    """felt-voice (B5.1)."""
    return felt_client(config, getattr(config, "voice_url", None), **kwargs)
