# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

"""Private read-only MQTT v5 preflight runner with secret-safe diagnostics."""

from __future__ import annotations

import asyncio
import inspect
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ._legion_auth import _LegionCredentialError
from ._network import connect_validated_addresses, resolve_private_endpoint
from .auth import (
    BearerTokenAuth,
    CertificateMaterial,
    MTLSAuth,
    NoAuth,
    PrivateKeyMaterial,
)
from .config import ECNConfig
from .exceptions import AuthenticationError, AuthorizationError, ECNClientError
from .models import CheckStatus, PreflightCheck, PreflightCheckName, PreflightReport

Probe = Callable[[], Awaitable[None]]
SSLContextFactory = Callable[[], ssl.SSLContext | Awaitable[ssl.SSLContext]]


class PreflightRunner:
    def __init__(
        self,
        config: ECNConfig,
        *,
        mqtt_probe: Probe,
        subscription_probes: Sequence[Probe] = (),
        ssl_context_factory: SSLContextFactory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._mqtt_probe = mqtt_probe
        self._subscription_probes = tuple(subscription_probes)
        self._ssl_context_factory = ssl_context_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(self) -> PreflightReport:
        checks = [
            self._result(
                PreflightCheckName.CONFIGURATION,
                CheckStatus.PASS,
                "configuration is valid",
            )
        ]
        checks.append(await self._timed(PreflightCheckName.DNS, self._check_dns))
        checks.append(await self._timed(PreflightCheckName.TCP, self._check_tcp))
        if self._config.tls.enabled:
            checks.append(await self._timed(PreflightCheckName.TLS, self._check_tls))
        elif self._config.plaintext_container_network is not None:
            checks.append(
                self._result(
                    PreflightCheckName.TLS,
                    CheckStatus.SKIPPED,
                    "TLS is explicitly disabled for reviewed container network "
                    f"'{self._config.plaintext_container_network.name}' plaintext transport",
                    required=False,
                )
            )
        else:
            checks.append(
                self._result(
                    PreflightCheckName.TLS,
                    CheckStatus.SKIPPED,
                    "TLS is disabled for an explicit loopback mock configuration",
                    required=False,
                )
            )
        checks.append(
            await self._timed(PreflightCheckName.AUTHENTICATION, self._check_auth_material)
        )
        checks.append(await self._timed(PreflightCheckName.MQTT, self._mqtt_probe))

        if self._subscription_probes:
            checks.extend(
                [
                    await self._timed(PreflightCheckName.SUBSCRIPTION, probe)
                    for probe in self._subscription_probes
                ]
            )
        else:
            checks.append(
                self._result(
                    PreflightCheckName.SUBSCRIPTION,
                    CheckStatus.SKIPPED,
                    "no bounded subscription probe was explicitly requested",
                    required=False,
                )
            )
        checks.append(
            self._result(
                PreflightCheckName.PUBLISH_AUTHORIZATION,
                CheckStatus.UNKNOWN,
                "publish authorization is unknown until a caller authorizes a real publish",
                required=False,
            )
        )

        successful = not any(
            check.required and check.status is CheckStatus.FAIL for check in checks
        )
        mqtt_ready = any(
            check.name is PreflightCheckName.MQTT and check.status is CheckStatus.PASS
            for check in checks
        )
        return PreflightReport(
            generated_at=self._clock(),
            successful=successful,
            ready=successful and mqtt_ready,
            checks=tuple(checks),
        )

    async def _timed(self, name: PreflightCheckName, operation: Probe) -> PreflightCheck:
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._config.connection_timeout):
                await operation()
        except Exception as error:
            return self._failure(name, started, error)
        return self._result(
            name,
            CheckStatus.PASS,
            self._success_detail(name),
            duration_ms=self._elapsed_ms(started),
        )

    async def _check_dns(self) -> None:
        if self._config.plaintext_container_network is not None:
            await resolve_private_endpoint(
                self._config.host,
                self._config.mqtt_port,
            )
            return
        loop = asyncio.get_running_loop()
        await loop.getaddrinfo(
            self._config.host,
            self._config.mqtt_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )

    async def _check_tcp(self) -> None:
        async def connect(hostname: str, _timeout: float) -> None:
            reader, writer = await asyncio.open_connection(
                hostname,
                self._config.mqtt_port,
            )
            del reader
            writer.close()
            await writer.wait_closed()

        if self._config.plaintext_container_network is not None:
            addresses = await resolve_private_endpoint(
                self._config.host,
                self._config.mqtt_port,
            )
            await connect_validated_addresses(
                addresses,
                self._config.connection_timeout,
                connect,
            )
            return
        await connect(self._config.host, self._config.connection_timeout)

    async def _check_tls(self) -> None:
        context_or_awaitable = self._ssl_context_factory()
        context = (
            await context_or_awaitable
            if inspect.isawaitable(context_or_awaitable)
            else context_or_awaitable
        )
        reader, writer = await asyncio.open_connection(
            self._config.host,
            self._config.mqtt_port,
            ssl=context,
            server_hostname=self._config.host,
        )
        del reader
        writer.close()
        await writer.wait_closed()

    async def _check_auth_material(self) -> None:
        auth = self._config.auth
        if isinstance(auth, BearerTokenAuth):
            await auth._resolve_credentials(self._config.integration_name)
            return
        if isinstance(auth, NoAuth):
            return
        assert isinstance(auth, MTLSAuth)
        self._check_material(auth.client_certificate, "client certificate")
        self._check_material(auth.client_key, "client private key")
        if self._config.tls.ca_certificate is not None:
            self._check_material(self._config.tls.ca_certificate, "CA certificate")

    @staticmethod
    def _check_material(
        material: CertificateMaterial | PrivateKeyMaterial,
        label: str,
    ) -> None:
        if material.path is not None and not Path(material.path).is_file():
            raise FileNotFoundError(f"{label} path does not identify a file")
        if material.data is not None and not material.data.get_secret_value().strip():
            raise ValueError(f"{label} data is empty")

    def _failure(
        self,
        name: PreflightCheckName,
        started: float,
        error: Exception,
    ) -> PreflightCheck:
        if isinstance(error, _LegionCredentialError):
            detail = error.message
        elif isinstance(error, AuthenticationError):
            detail = "MQTT broker rejected authentication"
        elif isinstance(error, AuthorizationError):
            detail = "MQTT broker rejected the requested subscription"
        elif isinstance(error, ECNClientError):
            detail = f"public client failure: {error.code}"
        elif isinstance(error, TimeoutError):
            detail = "operation timed out"
        else:
            detail = f"operation failed: {type(error).__name__}"
        return self._result(
            name,
            CheckStatus.FAIL,
            detail,
            duration_ms=self._elapsed_ms(started),
        )

    def _success_detail(self, name: PreflightCheckName) -> str:
        if name is PreflightCheckName.AUTHENTICATION and isinstance(
            self._config.auth,
            NoAuth,
        ):
            network = self._config.plaintext_container_network
            assert network is not None
            return f"no MQTT credential is supplied for reviewed container network '{network.name}'"
        return {
            PreflightCheckName.DNS: "host resolved",
            PreflightCheckName.TCP: "MQTT TCP endpoint is reachable",
            PreflightCheckName.TLS: "TLS negotiation and certificate verification succeeded",
            PreflightCheckName.AUTHENTICATION: "authentication material is available",
            PreflightCheckName.MQTT: "MQTT v5 CONNACK was accepted",
            PreflightCheckName.SUBSCRIPTION: "requested bounded MQTT subscription was accepted",
        }[name]

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return max(0.0, (time.monotonic() - started) * 1000)

    @staticmethod
    def _result(
        name: PreflightCheckName,
        status: CheckStatus,
        detail: str,
        *,
        required: bool = True,
        duration_ms: float = 0.0,
    ) -> PreflightCheck:
        return PreflightCheck(
            name=name,
            status=status,
            required=required,
            duration_ms=duration_ms,
            detail=detail,
        )


__all__: list[str] = []
