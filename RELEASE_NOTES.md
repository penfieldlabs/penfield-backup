# Penfield Backup v1.0.1

Download all your [Penfield](https://penfield.app) data in one command. No installs. No dependencies. Just Python 3.

## Features

- **OAuth 2.0 Device Code flow** with Dynamic Client Registration and read-only scope
- **Downloads everything** — memories, artifacts, and documents
- **Binary artifact support** — base64 detection via API content_type or file extension
- **Token auto-refresh** — TokenManager handles mid-backup 401s transparently
- **Checkpoint/resume** — `--resume` picks up interrupted backups where they left off
- **Post-backup verification** — file counts checked against expected totals
- **Exponential backoff** — retries on 5xx and network errors
- **Cross-platform** — macOS, Linux, Windows 10+
- **Zero dependencies** — Python 3 standard library only

## Requirements

- Python 3.8+
- A Penfield account

## License

AGPL-3.0
