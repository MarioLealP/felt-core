"""Meru's machine-to-machine standard on felt-core's in-cluster links (D127).

On k3s felt-core proves two things to the services she calls, the way every Meru app does:

- **which machine**: her own certificate from Meru's CA (nuwa-app ``certificate:``, OpenBao
  ``pki_meru`` through cert-manager), mounted at FELT_MERU_TLS_DIR as ``tls.crt``, ``tls.key``
  and ``ca.crt``. Renewed in place before its 24 hours run out; a fresh context per client picks
  the new one up, no restart.
- **who and what**: a 10-minute Authentik token per callee (client credentials, audience = the
  callee). The login is her pod's own Kubernetes token (nuwa-app ``authentikLogin``,
  FELT_AUTHENTIK_TOKEN_FILE) sent as the client assertion - no password anywhere. Authentik grants
  what the groups listing ``felt-core/felt-core`` grant (blueprints drive-rag.yaml, poe2rag.yaml).

Both unset = compose: the Felt certs of felt_tls, exactly as before.
"""

import asyncio
import ssl
import time
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel

from felt_core.clients.felt_tls import felt_client

ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
EARLY_S = 60  # a token this close to expiry counts as used up


def meru_client_context(tls_dir: str) -> ssl.SSLContext:
    """Trusts only Meru's CA and presents her Meru certificate."""
    d = Path(tls_dir)
    ctx = ssl.create_default_context(cafile=str(d / "ca.crt"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(d / "tls.crt"), str(d / "tls.key"))
    return ctx


def meru_client(config: Any, url: str | None, **kwargs: Any) -> httpx2.AsyncClient:
    """A client for an in-cluster peer that checks certificates.

    A client for an in-cluster peer that checks certificates (drive-rag): her Meru certificate
    when FELT_MERU_TLS_DIR is set and the URL is https://, else felt_tls's Felt-cert client
    (compose).
    """
    tls_dir = getattr(config, "meru_tls_dir", None)
    if (url or "").startswith("https://") and tls_dir:
        return httpx2.AsyncClient(verify=meru_client_context(tls_dir), **kwargs)
    return felt_client(config, url, **kwargs)


class _TokenAnswer(BaseModel):
    access_token: str
    expires_in: int = 600


class _Cached(BaseModel):
    token: str
    expires: float  # time.monotonic()


_cache: dict[tuple[str, str], _Cached] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def access_token(config: Any, audience: str) -> str | None:
    """A valid Authentik token for ``audience`` (= the callee's client id), or None with no Authentik login (compose).

    Cached until a minute before it expires; one fetch at a time per
    audience.
    """
    token_file = getattr(config, "authentik_token_file", None)
    if not token_file:
        return None
    key = (config.authentik_token_url, audience)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached and time.monotonic() < cached.expires - EARLY_S:
            return cached.token
        async with httpx2.AsyncClient(timeout=10) as http:
            response = await http.post(
                config.authentik_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": audience,
                    "scope": audience,  # the provider's own scope mapping (<callee>_permissions)
                    "client_assertion_type": ASSERTION,
                    # re-read each time: the kubelet swaps in a fresh one before the old expires
                    "client_assertion": Path(token_file).read_text().strip(),
                },
            )
        response.raise_for_status()
        answer = _TokenAnswer.model_validate_json(response.content)
        expires = time.monotonic() + answer.expires_in
        _cache[key] = _Cached(token=answer.access_token, expires=expires)
        return answer.access_token


async def auth_headers(config: Any, audience: str) -> dict[str, str]:
    """``Authorization: Bearer`` for ``audience``, or nothing on compose."""
    token = await access_token(config, audience)
    return {"Authorization": f"Bearer {token}"} if token else {}
