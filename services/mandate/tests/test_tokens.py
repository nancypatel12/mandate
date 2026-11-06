import base64
import json

import jwt
import pytest

from mandate import tokens
from mandate.tokens import TokenInvalid

from .conftest import KEY, claims


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_round_trip():
    c = claims()
    assert tokens.verify(tokens.sign(c, KEY), KEY) == c


def test_wrong_key():
    t = tokens.sign(claims(), KEY)
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY + "x")


def test_tampered_payload():
    # Raise the cap in the payload, keep the old signature.
    head, body, sig = tokens.sign(claims(), KEY).split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["per_txn_cap_cents"] = 1_000_000
    with pytest.raises(TokenInvalid):
        tokens.verify(f"{head}.{b64(payload)}.{sig}", KEY)


def test_alg_none_rejected():
    payload = claims().model_dump(mode="json")
    t = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY)


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_other_hmac_alg_rejected():
    t = jwt.encode(claims().model_dump(mode="json"), KEY, algorithm="HS512")
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY)


@pytest.mark.parametrize("t", ["", "garbage", "a.b.c", None, 123, b"bytes", "x" * 10_000])
def test_garbage(t):
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY)


def test_validly_signed_but_not_a_mandate():
    # Signed with our key, but missing claims. Still not a mandate.
    t = jwt.encode({"mandate_id": "mnd_x"}, KEY, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY)


def test_signed_non_revocable_mandate_rejected():
    payload = claims().model_dump(mode="json") | {"revocable": False}
    t = jwt.encode(payload, KEY, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        tokens.verify(t, KEY)


def test_expired_token_still_verifies():
    # Expiry is a policy decision with its own reason code, ordered after revocation.
    c = claims(exp=1)
    assert tokens.verify(tokens.sign(c, KEY), KEY).exp == 1


def test_short_key_refused():
    with pytest.raises(ValueError):
        tokens.sign(claims(), "short")
    with pytest.raises(ValueError):
        tokens.verify("a.b.c", "short")
