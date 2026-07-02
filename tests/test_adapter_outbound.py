"""FmsgAdapter outbound: draft→send flow, threading, media, standalone send."""

from types import SimpleNamespace

from tests.conftest import API_KEY, BOT_ADDRESS

USER = "@bob@example.com"


async def test_send_new_root_uses_default_topic(adapter, fake_api):
    result = await adapter.send(USER, "hello from hermes")
    assert result.success
    msg = fake_api.messages[int(result.message_id)]
    assert msg["_sent"] is True
    assert msg["from"] == BOT_ADDRESS
    assert msg["to"] == [USER]
    assert msg["topic"] == "Hermes"
    assert msg["pid"] is None
    assert msg["_data"] == "hello from hermes"


async def test_send_with_reply_to_sets_pid(adapter, fake_api):
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "question", topic="Q")
    result = await adapter.send(USER, "answer", reply_to=str(root))
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] == root
    assert msg["topic"] is None


async def test_send_threads_to_last_inbound_via_metadata(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "root", topic="T")
    reply = fake_api.seed_message(USER, [BOT_ADDRESS], "latest", pid=root)
    await adapter._on_message(fake_api._public(fake_api.messages[reply]))

    result = await adapter.send(USER, "response", metadata={"thread_id": str(root)})
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] == reply  # replies chain to the latest inbound, not the root


async def test_draft_send_call_order(adapter, fake_api):
    await adapter.send(USER, "ordered")
    create = fake_api.calls.index("POST /fmsg")
    send = next(i for i, c in enumerate(fake_api.calls) if c.endswith("/send"))
    assert create < send


async def test_send_document_attaches_file(adapter, fake_api, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-fake")
    result = await adapter.send_document(USER, str(f), caption="the report")
    assert result.success
    msg_id = int(result.message_id)
    assert fake_api.messages[msg_id]["_data"] == "the report"
    assert fake_api.attachments[msg_id]["report.pdf"] == b"%PDF-fake"
    assert fake_api.messages[msg_id]["_sent"] is True


async def test_oversized_attachment_rejected(adapter, fake_api, tmp_path, monkeypatch):
    import plugin.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "MAX_ATTACH_BYTES", 10)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 11)
    result = await adapter.send_document(USER, str(f))
    assert not result.success
    assert result.error_kind == "too_long"
    assert not fake_api.messages  # no draft leaked


async def test_failed_attach_cleans_up_draft(adapter, fake_api, tmp_path, monkeypatch):
    from plugin.fmsg_client import FmsgApiError

    async def boom(msg_id, filename, content):
        raise FmsgApiError(500, "disk full")

    monkeypatch.setattr(adapter._client, "attach", boom)
    f = tmp_path / "doc.txt"
    f.write_text("data")
    result = await adapter.send_document(USER, str(f))
    assert not result.success
    assert result.retryable
    assert all(m["_sent"] for m in fake_api.messages.values()) or not fake_api.messages


async def test_long_message_truncated(adapter, fake_api):
    result = await adapter.send(USER, "y" * (adapter.MAX_MESSAGE_LENGTH + 100))
    msg = fake_api.messages[int(result.message_id)]
    assert len(msg["_data"]) == adapter.MAX_MESSAGE_LENGTH


async def test_standalone_send(fake_api, monkeypatch):
    import plugin.adapter as adapter_mod
    import plugin.fmsg_client as client_mod

    # Route standalone's self-built clients through the fake transport.
    import httpx
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake_api.transport()
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", patched)

    pconfig = SimpleNamespace(extra={"api_url": "http://fmsg.test", "api_key": API_KEY})
    result = await adapter_mod._standalone_send(pconfig, USER, "cron says hi")
    assert result.get("success") is True
    msg = fake_api.messages[int(result["message_id"])]
    assert msg["_sent"] is True
    assert msg["topic"] == "Hermes"


async def test_standalone_send_missing_config(fake_api, monkeypatch):
    import plugin.adapter as adapter_mod

    monkeypatch.delenv("FMSG_API_URL", raising=False)
    monkeypatch.delenv("FMSG_API_KEY", raising=False)
    pconfig = SimpleNamespace(extra={})
    result = await adapter_mod._standalone_send(pconfig, USER, "hi")
    assert "error" in result
