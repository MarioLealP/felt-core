"""Felt service certs on felt-core's outbound links (WP-62 M2b B5, D4/D5).

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

import httpx


def felt_client_context(identity_dir: str) -> ssl.SSLContext:
    d = Path(identity_dir)
    ctx = ssl.create_default_context(cafile=str(d / "felt-ca.pem"))
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # the anchor is an intermediate
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(d / "client" / "current" / "bundle.pem"))
    return ctx


def voice_client(config: Any, **kwargs: Any) -> httpx.AsyncClient:
    """The client for felt-voice: mTLS with her Felt cert when felt-voice is
    on https:// and FELT_IDENTITY_DIR is set; plain otherwise (as before)."""
    url = getattr(config, "voice_url", None) or ""
    identity_dir = getattr(config, "identity_dir", None)
    if url.startswith("https://") and identity_dir:
        return httpx.AsyncClient(verify=felt_client_context(identity_dir), **kwargs)
    return httpx.AsyncClient(**kwargs)
