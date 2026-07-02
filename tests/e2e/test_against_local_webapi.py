"""Opt-in end-to-end test against a real, locally running fmsg-webapi.

Skipped unless ``FMSG_E2E=1``. Requires:

- fmsg-webapi running (see its README: Postgres + fmsgd dd.sql + webapi
  dd.sql, ``FMSG_API_TOKEN_ED25519_PRIVATE_KEY`` set),
- ``FMSG_E2E_API_URL``  — base URL (default http://127.0.0.1:8000),
- ``FMSG_E2E_API_KEY``  — an fmsgk_... key for the agent identity,
- ``FMSG_E2E_PEER_KEY`` — an fmsgk_... key for a second identity to
  exchange messages with.

Round-trip: peer sends a root message → agent client sees it in the
inbox and via the WebSocket → agent replies with pid → peer sees the
threaded reply.
"""

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("FMSG_E2E") != "1", reason="set FMSG_E2E=1 to run e2e tests"
)

API_URL = os.getenv("FMSG_E2E_API_URL", "http://127.0.0.1:8000")


@pytest.fixture
def agent_client():
    from plugin.fmsg_client import FmsgClient, TokenManager

    key = os.environ["FMSG_E2E_API_KEY"]
    return FmsgClient(TokenManager(API_URL, key))


@pytest.fixture
def peer_client():
    from plugin.fmsg_client import FmsgClient, TokenManager

    key = os.environ["FMSG_E2E_PEER_KEY"]
    return FmsgClient(TokenManager(API_URL, key))


async def test_round_trip_with_threaded_reply(agent_client, peer_client):
    await agent_client.tokens.get_token()
    await peer_client.tokens.get_token()
    agent_addr = agent_client.tokens.address
    peer_addr = peer_client.tokens.address

    # Peer opens a thread; capture it from the agent's WebSocket.
    async def next_ws_event(client):
        async for event in client.iter_events():
            if event.get("type") == "new_msg":
                return event["data"]

    ws_wait = asyncio.create_task(next_ws_event(agent_client))
    await asyncio.sleep(1.0)  # let the WS attach before sending

    root_id = await peer_client.create_draft(
        peer_addr, [agent_addr], "e2e: hello agent", topic="e2e"
    )
    await peer_client.send(root_id)

    pushed = await asyncio.wait_for(ws_wait, timeout=15.0)
    assert pushed["id"] == root_id
    assert pushed["from"] == peer_addr

    # Agent replies in-thread.
    reply_id = await agent_client.create_draft(
        agent_addr, [peer_addr], "e2e: hello peer", pid=root_id
    )
    await agent_client.send(reply_id)
    await agent_client.mark_read(root_id)

    # Peer sees the threaded reply.
    inbox = await peer_client.list_messages(limit=10)
    reply = next(m for m in inbox if m["id"] == reply_id)
    assert reply["pid"] == root_id
    assert reply["short_text"] == "e2e: hello peer"

    await agent_client.aclose()
    await peer_client.aclose()
