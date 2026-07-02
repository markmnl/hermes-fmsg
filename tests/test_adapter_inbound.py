"""FmsgAdapter inbound: event mapping, threading, dedupe, read receipts, catch-up."""

from tests.conftest import BOT_ADDRESS

USER = "@bob@example.com"


async def _prime(adapter):
    """Fetch a token so the adapter knows its own address."""
    await adapter._tokens.get_token()


async def test_root_message_maps_to_event(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(USER, [BOT_ADDRESS], "hello agent", topic="Greetings")
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))

    assert len(adapter.handled_events) == 1
    event = adapter.handled_events[0]
    assert event.text == "hello agent"
    assert event.message_id == str(msg_id)
    assert event.source.chat_id == USER
    assert event.source.user_id == USER
    assert event.source.thread_id == str(msg_id)  # root: thread is itself
    assert event.source.chat_topic == "Greetings"
    assert event.reply_to_message_id is None


async def test_reply_resolves_thread_root_via_pid_chain(adapter, fake_api):
    await _prime(adapter)
    root = fake_api.seed_message(USER, [BOT_ADDRESS], "root", topic="T")
    mid = fake_api.seed_message(USER, [BOT_ADDRESS], "mid", pid=root)
    leaf = fake_api.seed_message(USER, [BOT_ADDRESS], "leaf", pid=mid)

    await adapter._on_message(fake_api._public(fake_api.messages[leaf]))

    event = adapter.handled_events[0]
    assert event.source.thread_id == str(root)
    assert event.source.chat_topic == "T"
    assert event.reply_to_message_id == str(mid)
    # Chain is cached: a sibling reply resolves without refetching the root.
    calls_before = len(fake_api.calls)
    sib = fake_api.seed_message(USER, [BOT_ADDRESS], "sibling", pid=mid)
    await adapter._on_message(fake_api._public(fake_api.messages[sib]))
    get_calls = [c for c in fake_api.calls[calls_before:] if c.startswith("GET /fmsg/")]
    assert get_calls == []  # no pid-walk fetches needed
    assert adapter.handled_events[1].source.thread_id == str(root)


async def test_marks_read_after_dispatch(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(USER, [BOT_ADDRESS], "hi")
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))
    assert fake_api.messages[msg_id]["read"] is True


async def test_skips_duplicates_and_own_messages(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(USER, [BOT_ADDRESS], "once")
    public = fake_api._public(fake_api.messages[msg_id])
    await adapter._on_message(public)
    await adapter._on_message(public)
    assert len(adapter.handled_events) == 1

    own = fake_api.seed_message(BOT_ADDRESS, [USER], "my own reply")
    await adapter._on_message(fake_api._public(fake_api.messages[own]))
    assert len(adapter.handled_events) == 1


async def test_long_body_fetched_via_data_endpoint(adapter, fake_api):
    await _prime(adapter)
    long_text = "x" * 2000  # beyond the 768-byte short_text preview
    msg_id = fake_api.seed_message(USER, [BOT_ADDRESS], long_text)
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))
    assert adapter.handled_events[0].text == long_text


async def test_attachments_cached_as_media(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(
        USER, [BOT_ADDRESS], "see attached",
        attachments={"photo.png": b"\x89PNG-bytes", "notes.txt": b"text"},
    )
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))
    event = adapter.handled_events[0]
    assert len(event.media_urls) == 2
    assert "image/png" in event.media_types
    with open(event.media_urls[0], "rb") as f:
        assert f.read() in (b"\x89PNG-bytes", b"text")


async def test_flags_surface_in_metadata(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(
        USER, [BOT_ADDRESS], "urgent", important=True, no_reply=True
    )
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))
    event = adapter.handled_events[0]
    assert event.metadata["important"] is True
    assert event.metadata["no_reply"] is True
    assert "important" in event.channel_context
    assert "no_reply" in event.channel_context


async def test_catch_up_dispatches_missed_messages_oldest_first(adapter, fake_api):
    await _prime(adapter)
    seen = fake_api.seed_message(USER, [BOT_ADDRESS], "already handled")
    adapter._last_seen_id = seen
    m1 = fake_api.seed_message(USER, [BOT_ADDRESS], "missed 1")
    m2 = fake_api.seed_message(USER, [BOT_ADDRESS], "missed 2")

    await adapter._catch_up()

    texts = [e.text for e in adapter.handled_events]
    assert texts == ["missed 1", "missed 2"]
    assert adapter._last_seen_id == m2


async def test_first_run_catch_up_only_unread(adapter, fake_api):
    await _prime(adapter)
    fake_api.seed_message(USER, [BOT_ADDRESS], "old and read", read=True)
    fake_api.seed_message(USER, [BOT_ADDRESS], "new and unread")

    assert adapter._last_seen_id == 0
    await adapter._catch_up()

    texts = [e.text for e in adapter.handled_events]
    assert texts == ["new and unread"]


async def test_last_seen_persists_across_instances(adapter, fake_api):
    await _prime(adapter)
    msg_id = fake_api.seed_message(USER, [BOT_ADDRESS], "hello")
    await adapter._on_message(fake_api._public(fake_api.messages[msg_id]))

    reloaded = type(adapter)(adapter.config)
    reloaded._state_path = adapter._state_path
    reloaded._load_state()
    assert reloaded._last_seen_id == msg_id
