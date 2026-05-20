# Changelog

## 1.0.1 — 2026-05-20

### Fixed

- Checkpoint no longer cleared when post-backup verification fails. Previously
  a failed verification would delete the checkpoint, forcing a full re-download
  on `--resume`.

### Added

- Test suite (61 tests covering all core logic).
- RELEASE_NOTES.md for consistency with other Penfield tools.

## 1.0.0 — 2026-04-03

Initial public release.

- OAuth 2.0 Device Code flow with Dynamic Client Registration and read-only scope
- Downloads all memories, artifacts, and documents
- Binary artifact support (base64 detection via API content_type or file extension)
- Token caching with automatic refresh (TokenManager handles mid-backup 401s)
- Checkpoint/resume: `--resume` flag picks up interrupted backups
- Post-backup verification (file counts vs expected)
- Exponential backoff retries on 5xx / network errors
- Progress bars and colored terminal output (`NO_COLOR` supported)
- Cross-platform: macOS, Linux, Windows 10+
- Zero dependencies — Python 3 standard library only
