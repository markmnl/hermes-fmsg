"""FmsgClient: REST call shapes, 401 retry, draft rules, attachments."""

import json

import pytest

from plugin.fmsg_client import FmsgApiError
from tests.conftest import BOT_ADDRESS

USER = "@bob@example.com"


async def test_create_draft_and_send(client, fake_api):
    draft_id = await client.create_draft(BOT_ADDRESS, [USER], "hello bob", topic="Greetings")
    msg = fake_api.messages[draft_id]
    assert msg["_sent"] is False
    assert msg["topic"] == "Greetings"
    assert msg["_data"] == "hello bob"
    assert msg["size"] == len("hello bob")

    await client.send(draft_id)
    assert fake_api.messages[draft_id]["_sent"] is True


async def test_create_draft_reply_sets_pid_not_topic(client, fake_api):
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "root", topic="T")
    reply_id = await client.create_draft(BOT_ADDRESS, [USER], "a reply", pid=root)
    assert fake_api.messages[reply_id]["pid"] == root
    assert fake_api.messages[reply_id]["topic"] is None


async def test_pid_and_topic_mutually_exclusive(client):
    with pytest.raises(ValueError):
        await client.create_draft(BOT_ADDRESS, [USER], "x", pid=1, topic="T")


async def test_retries_once_on_401_with_fresh_token(client, fake_api):
    await client.tokens.get_token()
    fake_api.reject_next_requests = 1
    msgs = await client.list_messages()
    assert msgs == []
    assert fake_api.token_exchanges == 2  # initial + forced re-exchange


async def test_attach_multipart(client, fake_api):
    draft_id = await client.create_draft(BOT_ADDRESS, [USER], "with file")
    result = await client.attach(draft_id, "notes.txt", b"file-bytes")
    assert result["filename"] == "notes.txt"
    assert fake_api.attachments[draft_id]["notes.txt"] == b"file-bytes"


async def test_get_attachment_and_data(client, fake_api):
    msg_id = fake_api.seed_message(
        USER, [BOT_ADDRESS], "body text", attachments={"pic.png": b"\x89PNG"}
    )
    assert await client.get_attachment(msg_id, "pic.png") == b"\x89PNG"
    assert await client.get_data(msg_id) == b"body text"


async def test_list_messages_only_inbox(client, fake_api):
    fake_api.seed_message(USER, [BOT_ADDRESS], "for bot")
    fake_api.seed_message(USER, ["@carol@example.com"], "not for bot")
    msgs = await client.list_messages()
    assert len(msgs) == 1
    assert msgs[0]["short_text"] == "for bot"
    assert "_data" not in msgs[0]


async def test_api_error_carries_status(client, fake_api):
    with pytest.raises(FmsgApiError) as exc:
        await client.get_message(999)
    assert exc.value.status_code == 404


def test_ws_url_scheme_swap(client):
    assert client.ws_url() == "ws://fmsg.test/fmsg/ws"
    client.api_url = "https://api.example.com"
    assert client.ws_url() == "wss://api.example.com/fmsg/ws"
