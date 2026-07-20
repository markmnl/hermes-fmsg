---
name: hermes-fmsg
description: Operate and troubleshoot the hermes-fmsg messaging plugin. Use when installing or verifying the plugin, configuring safe fmsg access, sending or replying over fmsg, working with threads, home-channel or cron delivery, or diagnosing authentication, allowlist, connection, attachment, and delivery problems.
version: v0.1.0
author: markmnl
license: MIT
metadata:
  hermes:
    tags: [communication, messaging, fmsg, hermes]
---

# Operate Hermes fmsg

Use the `hermes-fmsg` platform plugin as the only message transport. This skill
guides operation; it does not replace the plugin.

## Set up and verify

1. Explain that the operator needs their own fmsg address to communicate with
   Hermes over fmsg. Recommend creating a separate agent or sub-account address
   for Hermes; never use the operator's password or API key as the agent.
2. Bind `FMSG_API_KEY` to the dedicated Hermes address. Add the operator's fmsg
   address to `FMSG_ALLOWED_USERS` and recommend setting that same address as
   `FMSG_HOME_CHANNEL` for agent-initiated messages and cron delivery.
3. Check `hermes --version` and `hermes plugins list --plain --no-bundled`.
4. If the plugin is absent, install it with
   `hermes plugins install markmnl/hermes-fmsg --enable`.
5. Configure `FMSG_API_URL` and the other settings through the plugin installer
   or Hermes configuration without inspecting secret values.
6. Restart the gateway, then check `hermes gateway status`. Confirm the logs
   report `fmsg connected` without exposing credentials or private URLs.

## Send safely

- Reply normally in the current fmsg conversation. Let the adapter preserve
  the sender, parent message, recipients, and Hermes session.
- Send to the configured home address with the `send_message` target `fmsg`,
  or run `hermes send --to fmsg "message"` from a shell.
- Deliver files through Hermes's normal attachment mechanism.
- Do not attempt `fmsg:@user@example.com` as a Hermes 0.18.x explicit target;
  target resolution rejects it before the adapter runs. State this limitation
  instead of bypassing the adapter.

Treat a root fmsg message as a new conversation. Replies continue the parent
chain, and multiple Hermes messages in one turn chain in order. Replies include
all participants of the parent message unless the user explicitly requests a
narrower recipient set.

## Troubleshoot

- **Plugin missing or disabled:** reinstall with `--enable` or run
  `hermes plugins enable fmsg-platform`.
- **Authentication rejected:** ask the operator to verify that the API key is
  current, unrevoked, permitted from this network, and belongs to the intended
  agent identity. Never inspect or echo its value.
- **Inbound message ignored:** verify the sender is in `FMSG_ALLOWED_USERS`,
  then restart the gateway.
- **Connection interrupted:** inspect sanitized fmsg gateway logs. The adapter
  reconnects with backoff and catches up from the inbox.
- **Delivery failed:** identify whether the failure concerns the home-channel
  target, parent thread, recipient policy, attachment limit, or remote host;
  report the adapter error without secrets.

## Guardrails

- Do not read, print, request, or expose `FMSG_API_KEY` or a JWT.
- Do not call the fmsg Web API, construct drafts, or choose a `from` address.
- Do not enable `FMSG_ALLOW_ALL_USERS` on an Internet-connected agent.
- Do not claim success until the adapter reports success or a recipient
  confirms delivery.

## Verify

Confirm the plugin is enabled, the gateway is connected, and an allowed fmsg
identity can complete a threaded round trip with the agent. When attachments
are relevant, include one small test file in the verification.
