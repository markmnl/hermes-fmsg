# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- Native repository-root installation through `hermes plugins install`.
- Packaging contract tests, CI, security guidance, contribution guidance, and
  developer-preview launch material.

### Changed

- WebSocket token rotation now scales its safety margin for short-lived JWTs,
  avoiding repeated reconnects that do not refresh the token.
- Installation and troubleshooting documentation now follows Hermes' native
  plugin workflow.

## [0.1.0] - 2026-07-17

### Added

- fmsg WebSocket inbox delivery with reconnect catch-up.
- REST-based drafts, attachments, sends, and read receipts.
- API-key exchange for short-lived JWT authentication.
- fmsg thread-tree mapping to Hermes sessions and ancestry-hydrated branches.
- Parent-specific reply-all and one-to-one thread continuation.
