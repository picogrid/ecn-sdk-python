# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from picogrid_ecn_client import (
    AuthenticationError,
    ECNClient,
    ECNConfig,
    Entity,
    EntityCategory,
    NoAuth,
    ReviewedContainerNetwork,
    TLSConfig,
)
from picogrid_ecn_client.testing import MockECN


def _no_auth_config(mock: MockECN) -> ECNConfig:
    return ECNConfig(
        host="127.0.0.1",
        mqtt_port=mock.mqtt_port,
        integration_name="no-auth-integration",
        auth=NoAuth(),
        tls=TLSConfig(enabled=False),
        plaintext_container_network=ReviewedContainerNetwork(name="reviewed-network"),
        connection_timeout=1,
        operation_timeout=1,
        shutdown_timeout=1,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_auth_plaintext_connects_publishes_and_subscribes_with_mock_ecn() -> None:
    async with MockECN(allow_unauthenticated=True) as mock:
        client = ECNClient(_no_auth_config(mock))
        try:
            await client.start()
            events = await client.entities.watch(categories={EntityCategory.TRACK})
            entity = Entity(
                id=uuid4(),
                category=EntityCategory.TRACK,
                integration="no-auth-integration",
                recorded_at=datetime.now(UTC),
                type="synthetic-track",
            )

            await client.entities.publish(entity)

            received = await asyncio.wait_for(anext(events), timeout=1)
            assert received.entity == entity
            await events.aclose()
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mock_ecn_rejects_no_auth_connection_without_explicit_opt_in() -> None:
    async with MockECN() as mock:
        client = ECNClient(_no_auth_config(mock))
        try:
            with pytest.raises(AuthenticationError, match="rejected authentication"):
                await client.start()
            assert mock.events.authentication_failed.is_set()
        finally:
            await client.close()
