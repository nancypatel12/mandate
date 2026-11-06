"""Mandate tokens: HS256 JWTs whose payload is exactly a MandateClaims.

The token carries authority. Revocation state and spend live in the database,
and a purchase needs both.
"""

import jwt
from pydantic import ValidationError

from .models import MandateClaims

ALG = "HS256"
MIN_KEY_BYTES = 32
MAX_TOKEN_CHARS = 4096
REQUIRED = list(MandateClaims.model_fields)


class TokenInvalid(Exception):
    """Not a mandate we signed. The message is for logs and never contains the token."""


def _key(key: str) -> bytes:
    raw = key.encode() if isinstance(key, str) else b""
    # A short key is a deployment bug, not a bad token, so it raises ValueError
    # and surfaces as INTERNAL_ERROR, not INVALID_SIGNATURE.
    if len(raw) < MIN_KEY_BYTES:
        raise ValueError(f"signing key must be at least {MIN_KEY_BYTES} bytes")
    return raw


def sign(claims: MandateClaims, key: str) -> str:
    return jwt.encode(claims.model_dump(mode="json"), _key(key), algorithm=ALG)


def verify(token: object, key: str) -> MandateClaims:
    secret = _key(key)
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise TokenInvalid("token missing or wrong size")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALG],  # pinned: no "none", no algorithm switching
            # Expiry is checked by the policy, after revocation, so it gets its own reason code.
            options={"verify_exp": False, "verify_iat": False, "require": REQUIRED},
        )
    except jwt.InvalidTokenError as e:
        raise TokenInvalid(type(e).__name__) from None
    try:
        return MandateClaims.model_validate(payload)
    except ValidationError:
        raise TokenInvalid("signed, but not a well-formed mandate") from None
