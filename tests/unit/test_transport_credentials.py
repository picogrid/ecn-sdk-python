# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
import ssl
import stat
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from picogrid_ecn_client._transport import credentials as credential_module
from picogrid_ecn_client._transport.credentials import (
    TemporaryCertificateFiles,
    build_client_ssl_context,
    build_lifecycle_owned_client_ssl_context,
)
from picogrid_ecn_client.auth import (
    CertificateMaterial,
    MTLSAuth,
    PrivateKeyMaterial,
    TLSConfig,
)
from picogrid_ecn_client.exceptions import AuthenticationError, ConfigurationError

_COMPATIBILITY_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIICxjCCAa4CCQCMafaoTZWIXjANBgkqhkiG9w0BAQsFADAlMSMwIQYDVQQDDBpj
b21wYXRpYmlsaXR5LXRlc3QuaW52YWxpZDAeFw0yNjA4MTAwNTAyMDdaFw0zNjA4
MDcwNTAyMDdaMCUxIzAhBgNVBAMMGmNvbXBhdGliaWxpdHktdGVzdC5pbnZhbGlk
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3tMUmbQU7KPOCqVeiqtO
3mva5ujI7BQ2sKX/cAMdB77ATgmwe4LmshEuDv3kk1YKKzTrDM4HhfSPMWvyZxi6
+JKh4n4xB1AESy9HxPI5Nn0L6hTr0QyYXAQV39S0G+ZQgVCxKs2I4I7KK1u6Fbra
sDhrwdKzvyBVW0fW8c0VlrKGPwmViLc1Gb5OuBq2PWqCpHOET378MBFbaecVfI//
K6TmD7oJmu57M7D2wv/4T3OSgYomUAc9GYRI/Lls/NXppDgXshBC3fepHfrX/m9w
pXXITzvdJfiybxdrxoKr1gXrBYf1lh0ecOzxZz/nGA5IQW/aUvsgv0lAb+/3dHmw
GwIDAQABMA0GCSqGSIb3DQEBCwUAA4IBAQAOfzY4wvwLDkgiY2D+kAxEnhGwWDwo
E9D1qvK8fQ9wUvapsHuTNsOdgR0GudQ9ECBeutDT4m+mwjVSlOh2/cfYY6ok/6Zu
tiwe9mdIsx091W7z8yaeXPE5m8bewmesbjyWdRT41/IKciZi7uGqbk7HQcYU9j/+
Iv3ncHIFS/hdzv8zTSm0DeWB9YZ774s17PPTcr0xodWULPUTwmLZE8f6vxlevvLP
dNDuyMd60VNHt2xX2cjSg71e3z9j0+6+SOsHkHAPfxt4GN19nGPpRBHTqxVSq66u
jz21pRDQz1uRqV+tesDuvTereNL5cNUHC5ork1HCgfI5cQw79c522XYO
-----END CERTIFICATE-----
"""


def test_prepared_tls_material_representation_redacts_every_role() -> None:
    canary = b"sensitive-material-canary"
    prepared = credential_module._PreparedTLSMaterial(
        ca=canary,
        client_certificate=canary,
        client_key=canary,
    )

    assert canary.decode("ascii") not in repr(prepared)


@pytest.mark.parametrize(
    ("roles", "expected_role"),
    [
        (frozenset({"ca", "client_certificate"}), "ca"),
        (frozenset({"client_certificate", "client_key"}), "client_certificate"),
        (frozenset({"ca", "client_certificate", "client_key"}), "ca"),
    ],
)
def test_tls_material_response_fallback_uses_fixed_role_precedence(
    roles: frozenset[credential_module._TLSMaterialRole],
    expected_role: credential_module._TLSMaterialRole,
) -> None:
    with pytest.raises(credential_module._TLSMaterialReadFailure) as caught:
        credential_module._decode_tls_material_response(
            b"not-json",
            expected_roles=roles,
        )

    assert caught.value.role == expected_role


def test_cpython_certificate_decoder_contract(tmp_path: Path) -> None:
    certificate = tmp_path / "compatibility-certificate.pem"
    certificate.write_text(_COMPATIBILITY_CERTIFICATE, encoding="ascii")

    decoded = credential_module._decode_client_certificate(certificate)

    assert decoded["notBefore"] == "Aug 10 05:02:07 2026 GMT"
    assert decoded["notAfter"] == "Aug  7 05:02:07 2036 GMT"


def test_in_memory_certificate_files_are_private_and_removed() -> None:
    storage = TemporaryCertificateFiles()
    certificate = storage.certificate_path(
        CertificateMaterial(data=SecretStr("synthetic certificate material")),
        name="certificate.pem",
    )
    private_key = storage.private_key_path(
        PrivateKeyMaterial(data=SecretStr("synthetic private-key material")),
        name="private-key.pem",
    )
    directory = storage.directory
    assert directory is not None
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(certificate.stat().st_mode) == 0o600
    assert stat.S_IMODE(private_key.stat().st_mode) == 0o600
    assert certificate.read_text() == "synthetic certificate material"

    storage.close()
    storage.close()
    assert not directory.exists()
    assert not certificate.exists()
    assert not private_key.exists()


@pytest.mark.parametrize("missing", [False, True])
def test_unusable_mtls_material_is_a_secret_safe_credential_failure(
    tmp_path: Path,
    missing: bool,
) -> None:
    directory = tmp_path
    certificate = directory / "credential-certificate.pem"
    private_key = directory / "credential-key.pem"
    if not missing:
        certificate.write_text("not a certificate")
        private_key.write_text("not a private key")
    storage = TemporaryCertificateFiles()
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(path=certificate),
        client_key=PrivateKeyMaterial(path=private_key),
    )

    with pytest.raises(AuthenticationError) as caught:
        build_client_ssl_context(TLSConfig(enabled=True, verify=False), auth, storage)

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(directory) not in rendered
    assert caught.value.operation == "mqtt.authenticate"
    assert storage._closed is True


@pytest.mark.parametrize(
    ("not_before", "not_after", "observed_at"),
    [
        (
            "Jan  1 00:00:00 2020 GMT",
            "Jan  1 00:00:00 2021 GMT",
            "Jan  2 00:00:00 2021 GMT",
        ),
        (
            "Jan  1 00:00:00 2030 GMT",
            "Jan  1 00:00:00 2031 GMT",
            "Dec 31 00:00:00 2029 GMT",
        ),
    ],
)
def test_expired_or_not_yet_valid_mtls_certificate_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    not_before: str,
    not_after: str,
    observed_at: str,
) -> None:
    certificate = tmp_path / "credential-certificate.pem"
    private_key = tmp_path / "credential-key.pem"
    certificate.write_text("synthetic certificate")
    private_key.write_text("synthetic private key")
    monkeypatch.setattr(
        credential_module,
        "_decode_client_certificate",
        lambda _path: {"notBefore": not_before, "notAfter": not_after},
    )
    storage = TemporaryCertificateFiles()
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(path=certificate),
        client_key=PrivateKeyMaterial(path=private_key),
    )

    with pytest.raises(AuthenticationError, match="validity window") as caught:
        build_client_ssl_context(
            TLSConfig(enabled=True, verify=False),
            auth,
            storage,
            wall_time=lambda: ssl.cert_time_to_seconds(observed_at),
        )

    assert caught.value.operation == "mqtt.authenticate"
    assert storage._closed is True


def test_mtls_certificate_key_mismatch_is_secret_safe_and_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _RejectingContext:
        def load_cert_chain(self, **_kwargs: Any) -> None:
            raise ssl.SSLError("synthetic key mismatch with secret detail")

    certificate = tmp_path / "credential-certificate.pem"
    private_key = tmp_path / "credential-key.pem"
    certificate.write_text("synthetic certificate")
    private_key.write_text("synthetic private key")
    monkeypatch.setattr(
        credential_module,
        "_build_server_auth_context",
        lambda *_args, **_kwargs: _RejectingContext(),
    )
    monkeypatch.setattr(
        credential_module,
        "_decode_client_certificate",
        lambda _path: {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2030 GMT",
        },
    )
    storage = TemporaryCertificateFiles()
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(path=certificate),
        client_key=PrivateKeyMaterial(path=private_key),
    )

    with pytest.raises(AuthenticationError) as caught:
        build_client_ssl_context(
            TLSConfig(enabled=True, verify=False),
            auth,
            storage,
            wall_time=lambda: ssl.cert_time_to_seconds("Jan  1 00:00:00 2025 GMT"),
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert "synthetic key mismatch" not in rendered
    assert str(tmp_path) not in rendered
    assert caught.value.operation == "mqtt.authenticate"
    assert storage._closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_error"),
    [
        ("ca", ConfigurationError),
        ("client_certificate", AuthenticationError),
        ("client_key", AuthenticationError),
    ],
)
async def test_lifecycle_owned_builder_bounds_inline_material_secret_safely(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    expected_error: type[Exception],
) -> None:
    canary = "sensitive-material-canary"
    monkeypatch.setattr(credential_module, "_MAXIMUM_TLS_MATERIAL_BYTES", 8)
    tls = TLSConfig(
        enabled=True,
        verify=role == "ca",
        ca_certificate=(CertificateMaterial(data=SecretStr(canary)) if role == "ca" else None),
    )
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(
            data=SecretStr(canary if role == "client_certificate" else "cert")
        ),
        client_key=PrivateKeyMaterial(data=SecretStr(canary if role == "client_key" else "key")),
    )

    with pytest.raises(expected_error) as caught:
        await build_lifecycle_owned_client_ssl_context(tls, auth)

    assert canary not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_error"),
    [
        ("ca", ConfigurationError),
        ("client_certificate", AuthenticationError),
        ("client_key", AuthenticationError),
    ],
)
async def test_lifecycle_owned_builder_rejects_unencodable_inline_material(
    role: str,
    expected_error: type[Exception],
) -> None:
    invalid = "\ud800"
    tls = TLSConfig(
        enabled=True,
        verify=role == "ca",
        ca_certificate=(CertificateMaterial(data=SecretStr(invalid)) if role == "ca" else None),
    )
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(
            data=SecretStr(invalid if role == "client_certificate" else "cert")
        ),
        client_key=PrivateKeyMaterial(data=SecretStr(invalid if role == "client_key" else "key")),
    )

    with pytest.raises(expected_error):
        await build_lifecycle_owned_client_ssl_context(tls, auth)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_error"),
    [
        ("ca", ConfigurationError),
        ("client_certificate", AuthenticationError),
        ("client_key", AuthenticationError),
    ],
)
async def test_lifecycle_owned_builder_bounds_paths_before_ipc_allocation(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    expected_error: type[Exception],
) -> None:
    monkeypatch.setattr(credential_module, "_MAXIMUM_TLS_MATERIAL_PATH_BYTES", 8)
    long_path = Path("/sensitive-path-canary")
    tls = TLSConfig(
        enabled=True,
        verify=role == "ca",
        ca_certificate=CertificateMaterial(path=long_path) if role == "ca" else None,
    )
    auth: object = object()
    if role != "ca":
        auth = MTLSAuth(
            client_certificate=(
                CertificateMaterial(path=long_path)
                if role == "client_certificate"
                else CertificateMaterial(data=SecretStr("cert"))
            ),
            client_key=(
                PrivateKeyMaterial(path=long_path)
                if role == "client_key"
                else PrivateKeyMaterial(data=SecretStr("key"))
            ),
        )

    with pytest.raises(expected_error) as caught:
        await build_lifecycle_owned_client_ssl_context(tls, auth)

    assert str(long_path) not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["password-too-long", "\ud800"])
async def test_lifecycle_owned_builder_bounds_private_key_password(
    monkeypatch: pytest.MonkeyPatch,
    password: str,
) -> None:
    monkeypatch.setattr(credential_module, "_MAXIMUM_PRIVATE_KEY_PASSWORD_BYTES", 8)
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(data=SecretStr("cert")),
        client_key=PrivateKeyMaterial(
            data=SecretStr("key"),
            password=SecretStr(password),
        ),
    )

    with pytest.raises(AuthenticationError):
        await build_lifecycle_owned_client_ssl_context(
            TLSConfig(enabled=True, verify=False),
            auth,
        )


@pytest.mark.asyncio
async def test_tls_material_cleanup_is_bounded_and_consumes_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credential_module,
        "_TLS_MATERIAL_CLEANUP_TIMEOUT_SECONDS",
        0.05,
    )

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        "import time;time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        close_fds=True,
    )
    assert process.stdin is not None
    process.stdin.close()

    release_exchange = asyncio.Event()

    async def stubborn_exchange() -> tuple[bytes | None, bytes | None]:
        while not release_exchange.is_set():
            try:
                await release_exchange.wait()
            except asyncio.CancelledError:
                continue
        return b"", None

    exchange = asyncio.create_task(stubborn_exchange())
    cleanup = asyncio.create_task(
        credential_module._terminate_tls_material_reader(process, exchange)
    )
    await asyncio.sleep(0)
    for _ in range(5):
        cleanup.cancel()
        await asyncio.sleep(0)

    async with asyncio.timeout(1):
        await cleanup

    assert cleanup.cancelling() == 0
    assert process.returncode is not None
    assert not exchange.done()
    release_exchange.set()
    async with asyncio.timeout(1):
        assert await exchange == (b"", None)


@pytest.mark.asyncio
async def test_lifecycle_owned_builder_does_not_read_ignored_ca_when_verification_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unexpected_subprocess(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ignored CA material must not start a reader")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_subprocess)
    missing_ca = tmp_path / "unused-ca.pem"

    context = await build_lifecycle_owned_client_ssl_context(
        TLSConfig(
            enabled=True,
            verify=False,
            ca_certificate=CertificateMaterial(path=missing_ca),
        ),
        object(),
    )

    assert context is not None
    assert context.verify_mode is ssl.CERT_NONE


@pytest.mark.asyncio
async def test_lifecycle_owned_builder_rejects_non_regular_ca_path_secret_safely(
    tmp_path: Path,
) -> None:
    canary = tmp_path / "sensitive-ca-directory"
    canary.mkdir()
    tls = TLSConfig(
        enabled=True,
        verify=True,
        ca_certificate=CertificateMaterial(path=canary),
    )

    with pytest.raises(ConfigurationError) as caught:
        await build_lifecycle_owned_client_ssl_context(tls, object())

    assert str(canary) not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_lifecycle_owned_builder_preserves_encrypted_key_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_passwords: list[str | None] = []

    class _RecordingContext:
        def load_cert_chain(
            self,
            *,
            certfile: str,
            keyfile: str,
            password: str | None,
        ) -> None:
            assert Path(certfile).is_file()
            assert Path(keyfile).is_file()
            observed_passwords.append(password)

    monkeypatch.setattr(
        credential_module,
        "_build_server_auth_context",
        lambda *_args, **_kwargs: _RecordingContext(),
    )
    monkeypatch.setattr(
        credential_module,
        "_decode_client_certificate",
        lambda _path: {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2030 GMT",
        },
    )
    auth = MTLSAuth(
        client_certificate=CertificateMaterial(data=SecretStr("certificate")),
        client_key=PrivateKeyMaterial(
            data=SecretStr("encrypted key"),
            password=SecretStr("key password"),
        ),
    )

    await build_lifecycle_owned_client_ssl_context(
        TLSConfig(enabled=True, verify=False),
        auth,
    )

    assert observed_passwords == ["key password"]
