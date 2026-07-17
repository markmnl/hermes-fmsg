# Contributing

Bug reports, compatibility results, documentation improvements, and focused
pull requests are welcome.

## Before opening an issue

- Update Hermes Agent and this plugin.
- Check existing open and closed issues.
- Reproduce with the smallest possible message or thread.
- Remove API keys, JWTs, private addresses, message content, and sensitive URLs
  from logs.

Use the compatibility-report template for successful tests too; knowing which
Hermes, Python, and operating-system combinations work is useful during the
preview.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -v
```

Changes to message parsing, delivery, authentication, threading, or recipient
selection need focused tests. Changes to the plugin manifest must update both
`plugin.yaml` and `plugin/plugin.yaml`; a contract test enforces equality.

Run the opt-in end-to-end test for changes that affect the fmsg Web API wire
contract. Do not use production credentials in tests.
