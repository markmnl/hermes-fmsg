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


def test_hosted_api_url_is_used_when_not_configured(monkeypatch):
    import plugin.adapter as adapter_mod
    from gateway.config import PlatformConfig

    monkeypatch.delenv("FMSG_API_URL", raising=False)
    monkeypatch.setenv("FMSG_API_KEY", API_KEY)

    adapter = adapter_mod.FmsgAdapter(PlatformConfig(enabled=True, extra={}))

    assert adapter._api_url == "https://api.fmsg.io"
    assert adapter_mod.check_requirements() is True
    assert adapter_mod._env_enablement()["api_url"] == "https://api.fmsg.io"


LISA = "@lisa@example.com"
MARK = "@mark@example.com"


async def test_reply_all_multi_party_thread(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(
        MARK, [BOT_ADDRESS, LISA], "hi guys", topic="What do you think?"
    )
    await adapter._on_message(fake_api._public(fake_api.messages[root]))

    result = await adapter.send(
        MARK, "pros and cons...", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    assert result.success
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] == root
    assert msg["topic"] is None
    assert set(msg["to"]) == {MARK, LISA}
    assert BOT_ADDRESS not in msg["to"]


async def test_dm_reply_stays_single_recipient(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "only us", topic="DM")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    result = await adapter.send(USER, "ok", reply_to=str(root), metadata={"thread_id": str(root)})
    msg = fake_api.messages[int(result.message_id)]
    assert msg["to"] == [USER]


async def test_reply_all_false_forces_single(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(MARK, [BOT_ADDRESS, LISA], "group", topic="G")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    result = await adapter.send(
        MARK,
        "private aside",
        reply_to=str(root),
        metadata={"thread_id": str(root), "fmsg_reply_all": False},
    )
    msg = fake_api.messages[int(result.message_id)]
    assert msg["to"] == [MARK]


async def test_fmsg_to_override(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(MARK, [BOT_ADDRESS, LISA], "group", topic="G")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    only_lisa = "@other@example.com"
    result = await adapter.send(
        MARK,
        "override",
        reply_to=str(root),
        metadata={"thread_id": str(root), "fmsg_to": [only_lisa, BOT_ADDRESS]},
    )
    msg = fake_api.messages[int(result.message_id)]
    # self excluded even if listed
    assert msg["to"] == [only_lisa]


async def test_add_to_participants_included(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(
        MARK,
        [BOT_ADDRESS],
        "start",
        topic="G",
        add_to=[{"add_to_from": MARK, "to": [LISA], "time": 1.0}],
    )
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    result = await adapter.send(
        MARK, "to all", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    msg = fake_api.messages[int(result.message_id)]
    assert set(msg["to"]) == {MARK, LISA}


async def test_reply_all_fetches_when_cache_cold(adapter, fake_api):
    """No inbound processing — resolve participants via GET on pid."""
    await adapter._tokens.get_token()
    root = fake_api.seed_message(MARK, [BOT_ADDRESS, LISA], "cold", topic="C")
    result = await adapter.send(MARK, "answer", reply_to=str(root))
    msg = fake_api.messages[int(result.message_id)]
    assert set(msg["to"]) == {MARK, LISA}


async def test_reply_uses_parent_message_participants_not_whole_thread(adapter, fake_api):
    """Recipients follow the parent message, not historical thread union."""
    await adapter._tokens.get_token()
    root = fake_api.seed_message(MARK, [BOT_ADDRESS, LISA], "all", topic="T")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    later = fake_api.seed_message(MARK, [BOT_ADDRESS], "only agent", pid=root)
    await adapter._on_message(fake_api._public(fake_api.messages[later]))

    # Reply to the narrow parent → only Mark.
    result = await adapter.send(
        MARK, "private", reply_to=str(later), metadata={"thread_id": str(root)}
    )
    msg = fake_api.messages[int(result.message_id)]
    assert msg["to"] == [MARK]
    assert msg["pid"] == later

    # Reply to the multi-party root → Mark + Lisa.
    result2 = await adapter.send(
        MARK, "group answer", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    msg2 = fake_api.messages[int(result2.message_id)]
    assert set(msg2["to"]) == {MARK, LISA}
    assert msg2["pid"] == root


async def test_standalone_send_reply_all(fake_api, monkeypatch):
    import plugin.adapter as adapter_mod
    import plugin.fmsg_client as client_mod
    import httpx

    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake_api.transport()
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", patched)

    root = fake_api.seed_message(MARK, [BOT_ADDRESS, LISA], "group root", topic="T")
    pconfig = SimpleNamespace(extra={"api_url": "http://fmsg.test", "api_key": API_KEY})
    result = await adapter_mod._standalone_send(
        pconfig, MARK, "cron into thread", thread_id=str(root)
    )
    assert result.get("success") is True
    msg = fake_api.messages[int(result["message_id"])]
    assert set(msg["to"]) == {MARK, LISA}
    assert msg["pid"] == root


async def test_multi_message_agent_turn_chains_pids(adapter, fake_api):
    """Second agent send parents to the first outbound, not the user prompt."""
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "prompt", topic="Q")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))

    r1 = await adapter.send(
        USER, "first chunk", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    r2 = await adapter.send(
        USER, "second chunk", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    m1 = fake_api.messages[int(r1.message_id)]
    m2 = fake_api.messages[int(r2.message_id)]
    assert m1["pid"] == root
    assert m2["pid"] == int(r1.message_id)  # chained, not both to root


async def test_new_inbound_resets_outbound_chain(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "prompt", topic="Q")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))
    r1 = await adapter.send(
        USER, "answer", reply_to=str(root), metadata={"thread_id": str(root)}
    )
    assert fake_api.messages[int(r1.message_id)]["pid"] == root

    follow = fake_api.seed_message(USER, [BOT_ADDRESS], "follow up", pid=root)
    await adapter._on_message(fake_api._public(fake_api.messages[follow]))
    r2 = await adapter.send(
        USER, "next answer", reply_to=str(follow), metadata={"thread_id": str(root)}
    )
    assert fake_api.messages[int(r2.message_id)]["pid"] == follow


async def test_agent_initiated_continues_dm_thread(adapter, fake_api):
    """Gateway home-channel style send (no reply_to/thread_id) parents to last DM."""
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "hey", topic="DM")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))

    down = await adapter.send(USER, "⚠️ Gateway restarting")
    up = await adapter.send(USER, "♻️ Gateway online")
    m_down = fake_api.messages[int(down.message_id)]
    m_up = fake_api.messages[int(up.message_id)]
    assert m_down["pid"] == root
    assert m_down["topic"] is None
    assert m_up["pid"] == int(down.message_id)  # chain notices too
    assert m_up["topic"] is None


async def test_agent_initiated_cold_lookup_from_api(adapter, fake_api):
    """After restart (empty memory) still finds last DM via list_messages/list_sent."""
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "prior chat", topic="DM")
    # Seed a prior outbound the adapter never saw in-process.
    prior = fake_api.seed_message(BOT_ADDRESS, [USER], "earlier notice", pid=root)
    assert adapter._last_by_chat == {}

    result = await adapter.send(USER, "♻️ Gateway online")
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] == prior  # latest DM with USER
    assert msg["topic"] is None


async def test_fmsg_new_thread_forces_root(adapter, fake_api):
    await adapter._tokens.get_token()
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "hey", topic="DM")
    await adapter._on_message(fake_api._public(fake_api.messages[root]))

    result = await adapter.send(
        USER,
        "brand new topic",
        metadata={"fmsg_new_thread": True, "topic": "Alerts"},
    )
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] is None
    assert msg["topic"] == "Alerts"


async def test_agent_initiated_skips_multi_party_parent(adapter, fake_api):
    """Home-channel pings must not attach to a multi-party parent."""
    await adapter._tokens.get_token()
    group = fake_api.seed_message(
        MARK, [BOT_ADDRESS, LISA], "group", topic="G"
    )
    await adapter._on_message(fake_api._public(fake_api.messages[group]))
    # Only multi-party history with MARK — no pure DM — so a root is OK.
    result = await adapter.send(MARK, "⚠️ Gateway restarting")
    msg = fake_api.messages[int(result.message_id)]
    assert msg["pid"] is None
    assert msg["topic"] == "Hermes"


async def test_standalone_send_continues_dm(fake_api, monkeypatch):
    import plugin.adapter as adapter_mod
    import plugin.fmsg_client as client_mod
    import httpx

    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake_api.transport()
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", patched)

    root = fake_api.seed_message(USER, [BOT_ADDRESS], "dm", topic="T")
    prior = fake_api.seed_message(BOT_ADDRESS, [USER], "old cron", pid=root)
    pconfig = SimpleNamespace(extra={"api_url": "http://fmsg.test", "api_key": API_KEY})
    result = await adapter_mod._standalone_send(pconfig, USER, "cron says hi")
    assert result.get("success") is True
    msg = fake_api.messages[int(result["message_id"])]
    assert msg["pid"] == prior
    assert msg["topic"] is None
