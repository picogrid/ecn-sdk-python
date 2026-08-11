# Copyright (c) Picogrid, Inc.
# SPDX-License-Identifier: MPL-2.0

from typing import assert_type

from picogrid_ecn_client import ConnectionStatus, ECNClient, ECNConfig


async def check_client_lifecycle(config: ECNConfig) -> None:
    client = ECNClient(config)
    assert_type(client.status, ConnectionStatus)
    assert_type(client.is_ready, bool)
    assert_type(await client.start(), None)
    assert_type(await client.close(), None)

    async with ECNClient(config) as active_client:
        assert_type(active_client, ECNClient)
        assert_type(active_client.status, ConnectionStatus)
        assert_type(active_client.is_ready, bool)
