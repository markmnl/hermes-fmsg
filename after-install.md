# fmsg-platform installed!

## Required before inbound works

1. `FMSG_API_KEY` + `FMSG_API_URL` (prompted at install; hosted default
   `https://api.fmsg.io`)
2. `FMSG_HOME_CHANNEL=@you@domain` — your owner address (cron / notifications)
3. `FMSG_ALLOWED_USERS=@you@domain` — who may talk to the agent  
   If you leave `FMSG_ALLOWED_USERS` empty but set `FMSG_HOME_CHANNEL`, the
   plugin defaults the allowlist to the home channel at gateway start.
4. Start the gateway and confirm fmsg is connected:

```bash
hermes gateway restart
hermes gateway status
# gateway log should include: fmsg connected
```

## Common footguns

| Symptom | Cause |
|---------|--------|
| You reply to the agent; nothing happens | Gateway stopped, or sender not on the allowlist |
| `Unauthorized user: @you@domain on fmsg` in gateway log | Empty allowlist (default deny) |
| `hermes send` works but replies do not | Standalone send does not need the gateway; inbound does |

## Access control

- Prefer a narrow `FMSG_ALLOWED_USERS` list.
- Do **not** set `FMSG_ALLOW_ALL_USERS=true` on a network-exposed agent, unless you want anyone to be able to fmsg your hermes.
- Add more senders later in `~/.hermes/.env`, then `hermes gateway restart`.

## Sanity check

```bash
hermes plugins list --plain --no-bundled
# expect: enabled ... fmsg-platform

# From your fmsg client, message the agent address.
# If ignored, check gateway.log for "Unauthorized user" or missing "fmsg connected".
```
