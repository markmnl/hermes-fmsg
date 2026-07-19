<p align="center">
  <a href="https://github.com/markmnl/hermes-fmsg/actions/workflows/test.yml">
    <img src="https://github.com/markmnl/hermes-fmsg/actions/workflows/test.yml/badge.svg" alt="Test">
  </a>
</p>

# HERMES FMSG

[fmsg](https://github.com/markmnl/fmsg) platform plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent) — give your Hermes
agent its own address and durable, threaded conversations.

An fmsg thread is also the agent's conversation context: roots map to Hermes
sessions, linear replies continue them, and later branches get their own session
with direct ancestry restored as context.


```bash
hermes plugins install markmnl/hermes-fmsg --enable
```

<br/>

## Why fmsg for an agent?

- **A durable agent identity:** people and other agents can contact a stable
  `@agent@domain` address without sharing a human login.
- **Context-native threads:** fmsg's message tree maps to Hermes sessions and
  branch context instead of flattening every conversation into one chat.
- **Offline-safe delivery:** WebSocket push is backed by inbox catch-up after a
  restart or network interruption.
- **Revocable access:** an agent uses its own expiring API key, exchanged for
  short-lived JWTs, rather than a human password or persistent user token.
- **Federated reach:** addresses on independently operated fmsg hosts can
  communicate without joining the same messaging silo.
- **Operator control:** self-host the messaging service and choose storage,
  quotas, retention, and policy without per-message platform metering.
- **Rich workflows:** text, attachments, reply-all, cron delivery, and
  agent-to-agent messaging use the same address.


## Status

Developer preview (`0.1.0`). The core message, attachment, reconnect, auth,
threading, and branching paths are implemented and unit tested. The plugin is a
standalone community integration, not bundled with Hermes Agent.

Tested with Hermes Agent `0.18.2`. Please report compatibility problems with
your Hermes version and operating system.

## Quickstart

### 1. Get an agent address and API key

Choose either route:

- **Hosted:** use an fmsg service provider, create an agent/sub-account under
  your user address, and copy its one-time API key. You also need the provider's
  fmsg Web API URL.

[fmsg.io](https://fmsg.io) provides accounts with up to 5 sub-accounts for free.

- **Self-hosted:** deploy the open-source
  [fmsg-docker](https://github.com/markmnl/fmsg-docker) stack, then create a
  derived sub-account and API key through its Web API or CLI.

Use a dedicated agent identity such as `@alice_hermes@example.com`. Do not give
the plugin a human password or a user's identity-provider token.

### 2. Install with Hermes

```bash
hermes plugins install markmnl/hermes-fmsg --enable
```

The Hermes installer prompts for:

- `FMSG_API_URL` — the base URL of the host's fmsg Web API.
- `FMSG_API_KEY` — the agent's `fmsgk_...` API key.

The repository root is a native Hermes plugin package; cloning or copying files
manually is not required.

### 3. Allow trusted senders

Add at least one address to `~/.hermes/.env`:

```dotenv
FMSG_ALLOWED_USERS=@alice@example.com,@bob@example.com
```

The default is deny. `FMSG_ALLOW_ALL_USERS=true` bypasses the allowlist and is
intended only for isolated development.

Optional settings:

```dotenv
FMSG_HOME_CHANNEL=@alice@example.com
FMSG_HOME_CHANNEL_NAME=Alice
FMSG_DEFAULT_TOPIC=Hermes
```

Environment variables take precedence over `platforms.fmsg.extra` in
`~/.hermes/config.yaml`.

### 4. Start or restart the gateway

```bash
hermes gateway restart
hermes gateway status
hermes plugins list --plain --no-bundled
```

The plugin list should show `fmsg-platform` enabled and the gateway log should
show `fmsg connected`. Send a message to the agent's address to open its first
Hermes session.

## How conversations map to Hermes

| fmsg | Hermes |
|---|---|
| counterparty address `@user@domain` | chat and authenticated user identity |
| root message | new session keyed by root message ID |
| first child path | continuation of the root session |
| later sibling branch | new `{root}:br:{message}` session with direct ancestry context |
| `short_text` or message data | inbound prompt text |
| attachments | cached inbound media or outbound fmsg attachments |
| `important` / `no_reply` | event metadata and model context |
| parent participants | reply-all recipients for that parent message |
| read route | receipt after Hermes dispatches the message |

Multiple Hermes replies in one turn form a chain: the first replies to the
latest inbound message, and each subsequent message replies to the previous
outbound message. A later inbound message resets that chain.

Agent-initiated messages without an explicit thread continue the most recent
one-to-one conversation with that address. Set metadata
`fmsg_new_thread=true` to force a new root, with an optional `topic` override.
Multi-party conversations are never selected for automatic continuation.

### Reply-all behavior

A reply includes every participant on the specific parent message (`from`,
`to`, and `add_to`), excluding the agent. Exceptional callers can select a
subset with `fmsg_to` / `recipients`, or set `fmsg_reply_all=false` for the
counterparty only.

## Security notes

- Review any Hermes plugin before installation; plugins execute inside the
  Hermes process.
- Keep the fmsg API key in `~/.hermes/.env`, never in a repository or issue.
- Restrict the key to expected CIDRs where the host supports it.
- Prefer short expiry periods and rotate or revoke a key immediately if it is
  exposed.
- Keep `FMSG_ALLOWED_USERS` narrow. Do not enable `FMSG_ALLOW_ALL_USERS` on an
  Internet-connected agent.

Please report suspected vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## Troubleshooting

```bash
hermes --version
hermes plugins list --plain --no-bundled
hermes gateway status
hermes logs --help
```

Common checks:

- **Plugin is disabled:** rerun the install with `--enable`, or use
  `hermes plugins enable fmsg-platform`.
- **Missing dependency:** install `httpx` and `websockets` in the Python
  environment used by Hermes.
- **API key rejected:** confirm it is unexpired, unrevoked, allowed from the
  gateway's IP, and belongs to the intended agent address.
- **Messages ignored:** add the sender to `FMSG_ALLOWED_USERS` and restart the
  gateway.
- **Connection drops:** the adapter reconnects with backoff and catches up from
  the inbox. Include sanitized `fmsg` lines from the gateway log in a bug report.

Never post API keys, JWTs, private addresses, or unredacted URLs containing
credentials.

## Updating and uninstalling

```bash
hermes plugins update fmsg-platform
hermes plugins disable fmsg-platform
hermes plugins remove fmsg-platform
```

Consult `hermes plugins --help` if command names differ on an older Hermes
release.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -v
```

Unit tests use an in-memory fake fmsg Web API and stub only the Hermes gateway
surface needed by the adapter.

The legacy developer copy target remains available:

```bash
make install
```

### End-to-end test

The opt-in test exchanges a threaded round trip between two real API-key
identities:

```bash
FMSG_E2E=1 \
FMSG_E2E_API_URL=http://127.0.0.1:8000 \
FMSG_E2E_API_KEY=fmsgk_... \
FMSG_E2E_PEER_KEY=fmsgk_... \
.venv/bin/python -m pytest tests/e2e/ -v
```

It requires a running fmsg Web API with the `fmsgd` base schema and the Web API
schema applied. The `fmsg-docker` stack provides the full integration
environment.
