from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import AppUser

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    subject: str


class TokenVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.jwks = PyJWKClient(settings.auth_jwks_url, cache_keys=True, lifespan=300)

    def verify(self, token: str) -> Identity:
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["EdDSA", "RS256"],
                audience=self.settings.auth_audience,
                issuer=self.settings.auth_issuer,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise ApiError(401, "AUTH_INVALID_TOKEN", "登录状态无效，请重新登录") from exc
        return Identity(subject=claims["sub"])


_verifier: TokenVerifier | None = None


def get_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "AUTH_REQUIRED", "请先登录")
    global _verifier
    if _verifier is None:
        _verifier = TokenVerifier()
    return _verifier.verify(credentials.credentials)


def get_current_user(
    identity: Annotated[Identity, Depends(get_identity)],
    db: Annotated[Session, Depends(get_db)],
) -> AppUser:
    user = db.scalar(select(AppUser).where(AppUser.auth_subject == identity.subject))
    if user:
        return user
    user = AppUser(auth_subject=identity.subject)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        user = db.scalar(select(AppUser).where(AppUser.auth_subject == identity.subject))
        if user is None:
            raise
    return user


CurrentUser = Annotated[AppUser, Depends(get_current_user)]
