# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Explicit public authentication and TLS configuration."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, TypeAlias

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from .models._base import PublicModel

TokenProvider: TypeAlias = Callable[[], Coroutine[Any, Any, str]]
"""A cooperative asynchronous callable that supplies the current bearer token."""
CredentialsProvider: TypeAlias = Callable[[], Coroutine[Any, Any, tuple[str, str]]]
"""A cooperative asynchronous callable supplying one username-token pair."""
_MAXIMUM_MQTT_PASSWORD_BYTES = 65_535
_MAXIMUM_MQTT_USERNAME_LENGTH = 256
_INVALID_USERNAME_MESSAGE = "username must be a non-empty MQTT UTF-8 value"


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value):
        return True
    return callable(value) and inspect.iscoroutinefunction(type(value).__call__)


def _validate_mqtt_username(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > _MAXIMUM_MQTT_USERNAME_LENGTH:
        raise ValueError(_INVALID_USERNAME_MESSAGE)
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError(_INVALID_USERNAME_MESSAGE) from None
    if any(_is_prohibited_mqtt_character(character) for character in normalized):
        raise ValueError(_INVALID_USERNAME_MESSAGE)
    return normalized


def _is_prohibited_mqtt_character(character: str) -> bool:
    code_point = ord(character)
    return (
        code_point < 0x20
        or 0x7F <= code_point <= 0x9F
        or 0xFDD0 <= code_point <= 0xFDEF
        or code_point & 0xFFFF in {0xFFFE, 0xFFFF}
    )


class CertificateMaterial(PublicModel):
    """Provide a certificate from a filesystem path or secret PEM text.

    Exactly one source is required. In-memory material uses a mode-0600 temporary file
    that is removed on close or failure; secret text is excluded from representations
    and public error text.
    """

    path: Path | None = Field(
        default=None,
        repr=False,
        description="Optional certificate file path; mutually exclusive with in-memory data.",
    )
    data: SecretStr | None = Field(
        default=None,
        description=(
            "Optional secret in-memory PEM certificate; mutually exclusive with path "
            "and excluded from representations and public error text."
        ),
    )

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> CertificateMaterial:
        if (self.path is None) == (self.data is None):
            raise ValueError("provide exactly one of certificate path or data")
        return self


class PrivateKeyMaterial(PublicModel):
    """Provide a private key from a filesystem path or secret PEM text.

    Exactly one key source is required. In-memory material uses a mode-0600 temporary
    file that is removed on close or failure; key and password values are excluded
    from representations and public error text.
    """

    path: Path | None = Field(
        default=None,
        repr=False,
        description="Optional private-key file path; mutually exclusive with in-memory data.",
    )
    data: SecretStr | None = Field(
        default=None,
        description=(
            "Optional secret in-memory PEM key; mutually exclusive with path and "
            "excluded from representations and public error text."
        ),
    )
    password: SecretStr | None = Field(
        default=None,
        description=(
            "Optional private-key password whose value is excluded from representations "
            "and public error text."
        ),
    )

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> PrivateKeyMaterial:
        if (self.path is None) == (self.data is None):
            raise ValueError("provide exactly one of private-key path or data")
        return self


class TLSConfig(PublicModel):
    """Configure TLS transport and optional custom certificate authority material.

    TLS and peer verification default to enabled. CA material is valid only while TLS
    is enabled.
    """

    enabled: bool = Field(
        default=True,
        description="Whether TLS transport is enabled; defaults to true.",
    )
    verify: bool = Field(
        default=True,
        description="Whether TLS peer certificates are verified; defaults to true.",
    )
    ca_certificate: CertificateMaterial | None = Field(
        default=None,
        description="Optional custom certificate-authority material; requires TLS.",
    )

    @model_validator(mode="after")
    def validate_disabled_tls(self) -> TLSConfig:
        if not self.enabled and self.ca_certificate is not None:
            raise ValueError("CA certificate requires TLS to be enabled")
        return self


class BearerTokenAuth(PublicModel):
    """Configure MQTT CONNECT authentication with a current bearer token.

    Supply exactly one static token, token provider, or credentials provider. Secret
    token values are excluded from representations and public error text.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)

    kind: Literal["bearer"] = Field(
        default="bearer",
        description="Authentication discriminator; always 'bearer'.",
    )
    username: (
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=256,
                description=(
                    "Optional MQTT username, from 1 through 256 characters; a credentials "
                    "provider supplies it instead, and loopback mock use may infer it."
                ),
            ),
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Optional MQTT username, from 1 through 256 characters; a credentials "
            "provider supplies it instead, and loopback mock use may infer it."
        ),
    )
    token: SecretStr | None = Field(
        default=None,
        description=(
            "Optional static MQTT CONNECT token of at most 65,535 UTF-8 bytes; its "
            "value is excluded from representations and public error text."
        ),
    )
    token_provider: SkipJsonSchema[TokenProvider | None] = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Optional cooperative async provider for the current token; excluded from "
            "serialization and representations."
        ),
    )
    credentials_provider: SkipJsonSchema[CredentialsProvider | None] = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Optional cooperative async provider returning one current username-token "
            "pair from the same credential generation; excluded from serialization "
            "and representations."
        ),
    )

    @model_validator(mode="after")
    def require_one_token_source(self) -> BearerTokenAuth:
        sources = sum(
            source is not None
            for source in (self.token, self.token_provider, self.credentials_provider)
        )
        if sources != 1:
            raise ValueError(
                "provide exactly one of token, token_provider, or credentials_provider"
            )
        if self.credentials_provider is not None and self.username is not None:
            raise ValueError("credentials_provider supplies the MQTT username")
        return self

    @field_validator("username", mode="before")
    @classmethod
    def validate_username(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        return _validate_mqtt_username(value)

    @field_validator("token_provider", "credentials_provider")
    @classmethod
    def validate_async_provider(cls, value: object) -> object:
        if value is not None and not _is_async_callable(value):
            raise ValueError("credential providers must be cooperative async callables")
        return value

    async def _resolve_token(self) -> str:
        if self.token is not None:
            return _validate_token(self.token.get_secret_value(), source="token")
        assert self.token_provider is not None
        token = await self.token_provider()
        return _validate_token(token, source="token_provider")

    async def _resolve_credentials(self, fallback_username: str) -> tuple[str, str]:
        if self.credentials_provider is None:
            return self.username or fallback_username, await self._resolve_token()
        credentials = await self.credentials_provider()
        if not isinstance(credentials, tuple) or len(credentials) != 2:
            raise ValueError("credentials_provider returned invalid credentials")
        username, token = credentials
        if not isinstance(username, str):
            raise ValueError("credentials_provider returned an invalid username")
        try:
            username = _validate_mqtt_username(username)
        except ValueError:
            raise ValueError("credentials_provider returned an invalid username") from None
        return username, _validate_token(token, source="credentials_provider")


def _validate_token(value: object, *, source: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ValueError(f"{source} returned an invalid token")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError(f"{source} returned an invalid token") from None
    if len(encoded) > _MAXIMUM_MQTT_PASSWORD_BYTES:
        raise ValueError(f"{source} returned an invalid token")
    return value


class NoAuth(PublicModel):
    """Configure no-auth, which is not a standalone unauthenticated mode and is accepted only with an explicit reviewed-container-network attestation and explicit TLS disablement.

    It carries no username, password, bearer token, or certificate material, so the
    connection cannot transmit a reusable credential.
    """

    kind: Literal["none"] = Field(
        default="none",
        description="Authentication discriminator; always 'none'.",
    )


class MTLSAuth(PublicModel):
    """Configure MQTT authentication with a client certificate and private key.

    This authentication kind requires TLS to remain enabled.
    """

    kind: Literal["mtls"] = Field(
        default="mtls",
        description="Authentication discriminator; always 'mtls'.",
    )
    client_certificate: CertificateMaterial = Field(
        description="Client certificate supplied by path or secret in-memory PEM text."
    )
    client_key: PrivateKeyMaterial = Field(
        description="Client private key and optional password material."
    )


AuthConfig = Annotated[
    BearerTokenAuth | MTLSAuth | NoAuth,
    Field(
        discriminator="kind",
        description=(
            "Bearer-token, mutual-TLS, or no-authentication configuration selected by its kind."
        ),
    ),
]
"""Authentication configuration discriminated by the literal ``kind`` field."""

__all__ = [
    "AuthConfig",
    "BearerTokenAuth",
    "CertificateMaterial",
    "CredentialsProvider",
    "MTLSAuth",
    "NoAuth",
    "PrivateKeyMaterial",
    "TLSConfig",
    "TokenProvider",
]
