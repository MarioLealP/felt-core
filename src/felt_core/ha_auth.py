"""Home Assistant as a caller (felt-ha-hub D3, 2026-10-18): HA's "Felt"
conversation agent proves who it is with an Authentik access token - the
client_credentials grant of the `ha-felt` OAuth provider, logged in as the
`ha-felt` service account with its app password. felt-core checks the token
itself (standard rule 2: an independently token-authenticated workload
endpoint - proxy identity headers mean nothing here): signature against
Authentik's JWKS, issuer, audience (the provider's client id), expiry, and
that it's the ha-felt account. Tokens live 15 minutes; HA refreshes them.

HA isn't part of Felt, so it isn't in Felt's own CA (pki_felt, M2b D6).
"""

from __future__ import annotations

import logging
from functools import lru_cache

import jwt

from felt_core.config import FeltConfig

logger = logging.getLogger(__name__)

HA_ROUTES = frozenset(
    {
        ("POST", "/v1/ha/chat"),
        ("GET", "/v1/ha/controls"),
        ("POST", "/v1/ha/controls"),
        ("GET", "/v1/ha/systems"),
    }
)
_ALGORITHMS = ["RS256", "ES256"]


def is_ha_route(method: str, path: str) -> bool:
    return (method, path) in HA_ROUTES


@lru_cache(maxsize=4)
def _jwks(url: str) -> jwt.PyJWKClient:
    # caches the keys; refetches on an unknown kid (Authentik key rotation)
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def check(config: FeltConfig, authorization: str) -> str | None:
    """None when the token is a valid ha-felt token, else why not."""
    if not (config.ha_agent_issuer and config.ha_agent_client_id):
        return "ha agent not configured"
    if not authorization.startswith("Bearer "):
        return "no bearer token"
    token = authorization[7:]
    try:
        key = _jwks(
            config.ha_agent_jwks_url or config.ha_agent_issuer + "jwks/"
        ).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=_ALGORITHMS,
            audience=config.ha_agent_client_id,
            issuer=config.ha_agent_issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as e:
        return f"bad token ({type(e).__name__})"
    if claims.get("preferred_username") != config.ha_agent_username:
        return "not the ha agent account"
    return None
