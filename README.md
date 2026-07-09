# hermes-fmsg

[fmsg](https://github.com/markmnl/fmsgd) platform plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) — message your
Hermes agent at an fmsg address and get threaded replies back, the same way
Hermes speaks Telegram, WhatsApp and email.

- **Inbound**: real-time push over fmsg-webapi's WebSocket (`GET /fmsg/ws`),
  with catch-up on reconnect so nothing is missed while the gateway is down.
- **Outbound**: draft → attach → send via the JSON REST routes, threading
  replies with `pid` so conversations stay grouped in fmsg clients.
- **Auth**: an fmsg API key (`fmsgk_...`) exchanged for a short-lived JWT.
  No password, no long-lived token on the wire.
- **Deps**: `httpx` (already a Hermes dependency) and `websockets`. No SDK.

## Install

```bash
make install          # copies the plugin to ~/.hermes/plugins/fmsg/
pip install websockets  # in Hermes' Python environment, if not present
```

Then restart the Hermes gateway (`hermes gateway`). `hermes gateway status`
should list **fmsg** once the env vars below are set.

## Create the agent's fmsg identity

Run the agent as a **derived sub-account** of your own address so it has its
own revocable identity (e.g. `@alice_hermes@example.com`).

**You need to create your fmsg sub-account and obtain an API key, check your fmsg host.**

## Configure

In `~/.hermes/.env`:

```bash
FMSG_API_URL=https://fmsgapi.example.com
FMSG_API_KEY=fmsgk_xxxxxxxx_yyyyyyyyyyyyyyyy

# Who may talk to the agent (default deny):
FMSG_ALLOWED_USERS=@alice@example.com,@bob@example.com

# Optional:
FMSG_HOME_CHANNEL=@alice@example.com   # cron / notification delivery target
FMSG_DEFAULT_TOPIC=Hermes              # topic for agent-initiated threads
# FMSG_ALLOW_ALL_USERS=true            # dev only
```

Or in `~/.hermes/config.yaml`:

```yaml
platforms:
  fmsg:
    enabled: true
    extra:
      api_url: "https://fmsgapi.example.com"
      api_key: "fmsgk_..."
      default_topic: "Hermes"
```

Environment variables win over `config.yaml`.

## How fmsg concepts map to Hermes

| fmsg                                                      | Hermes                                                                                            |
|-----------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| counterparty address `@user@domain`                       | chat + user identity (DM)                                                                         |
| thread: root message with `topic`, replies chaining `pid` | session `thread_id` = root message id — each fmsg thread is its own conversation context          |
| `short_text` / `GET /fmsg/:id/data`                       | inbound message text                                                                              |
| attachments                                               | inbound: cached for the agent's vision/file tools; outbound: agent files sent as fmsg attachments |
| `important` / `no_reply`                                  | surfaced to the agent as message context                                                          |
| multi-party parent `from`/`to`/`add_to`                   | reply-all to **that parent’s** participants by default; subset only in exceptional cases          |
| `POST /fmsg/:id/read`                                     | read receipt after the agent handles a message                                                    |


The agent's replies set `pid` to the latest inbound message of the thread.
Agent-initiated messages (cron jobs, notifications) open a new root thread
with `FMSG_DEFAULT_TOPIC`.

### Multi-party / reply-all

**Default:** a reply’s `to` is every participant on **the parent message**
you are replying to (`from` + `to` + `add_to`, excluding the agent). That is
normal reply-all on that message — keep everyone unless you have a strong
reason not to (e.g. privately warning others about a malicious participant).

| Situation | Outbound `to` |
|-----------|----------------|
| Reply to a DM parent | that counterparty |
| Reply to a multi-party parent | all other participants on **that** parent |
| Reply to a later 1:1 message in a group thread | only that message’s participants (not the whole history) |
| New root (cron / no `pid`) | single target (home / chat_id) |
| Metadata `fmsg_reply_all=false` | counterparty only (exceptional) |
| Metadata `fmsg_to` / `recipients` | explicit list (self stripped; exceptional subset) |

Parent participants are cached on inbound by message id and re-fetched via
`GET /fmsg/:id` when the cache is cold. Multi-party parents also get a short
channel-context note for the model.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -v
```

Unit tests run against an in-memory fake of fmsg-webapi (no network, no
hermes-agent checkout needed — the `gateway` package is stubbed in
`tests/conftest.py`).

### End-to-end

`tests/e2e/` runs against a real fmsg-webapi (Postgres + `fmsgd/dd.sql` +
fmsg-webapi's `dd.sql`, with `FMSG_API_TOKEN_ED25519_PRIVATE_KEY` set — or
use the [fmsg-docker](https://github.com/markmnl/fmsg-docker) stack):

```bash
FMSG_E2E=1 \
FMSG_E2E_API_URL=http://127.0.0.1:8000 \
FMSG_E2E_API_KEY=fmsgk_...   \
FMSG_E2E_PEER_KEY=fmsgk_...  \
.venv/bin/python -m pytest tests/e2e/ -v
```

## Roadmap

- Upstream to `NousResearch/hermes-agent` as a bundled plugin under
  `plugins/platforms/fmsg/` (the directory layout here matches theirs, so
  the move is mechanical).
