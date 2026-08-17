from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import TokenVerifier
from app.config import get_settings
from app.errors import ApiError


def make_token(audience: str) -> tuple[str, object]:
    settings = get_settings()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "better-auth-user-id",
            "iss": settings.auth_issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        private_key,
        algorithm="RS256",
    )
    return token, private_key.public_key()


def test_jwt_verifier_accepts_expected_issuer_and_audience() -> None:
    token, public_key = make_token(get_settings().auth_audience)
    verifier = TokenVerifier()
    verifier.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    assert verifier.verify(token).subject == "better-auth-user-id"


def test_jwt_verifier_rejects_wrong_audience() -> None:
    token, public_key = make_token("wrong-audience")
    verifier = TokenVerifier()
    verifier.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    with pytest.raises(ApiError) as error:
        verifier.verify(token)
    assert error.value.code == "AUTH_INVALID_TOKEN"
