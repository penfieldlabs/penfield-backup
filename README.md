# Penfield Backup

Download all your [Penfield](https://penfield.app) data in one command. No installs. No dependencies. Just Python 3.

```
python3 penfield_backup.py
```

## What it does

Downloads your complete Penfield data to a local folder:

```
penfield-backup/
  memories.json              All your memories (internal scoring fields stripped)
  artifacts/                 Your artifacts, directory structure preserved
  documents/                 Your uploaded documents (original files)
  documents/documents.json   Metadata index (id, filename, content_type, size)
  manifest.json              Export metadata and stats
```

## Requirements

- Python 3.8+
- A Penfield account

That's it. No `pip install`, no virtual environment, no build step. The script uses only Python's standard library.

## Usage

```bash
# Default: saves to ./penfield-backup/
python3 penfield_backup.py

# Custom output folder
python3 penfield_backup.py -o ~/Desktop/my-penfield-data

# Resume an interrupted backup
python3 penfield_backup.py --resume

# Remove saved credentials
python3 penfield_backup.py --logout
```

On first run, a browser window opens so you can authorize read-only access. After that, credentials are cached and subsequent runs are instant.

If a backup is interrupted (network drop, Ctrl-C, laptop closed), run again with `--resume` and it picks up where it left off.

## How authorization works

Penfield Backup uses the OAuth 2.0 Device Code flow and asks for **read-only** access. It only ever reads your data.

1. The tool registers itself as an OAuth client with Penfield
2. You authorize in your browser at `portal.penfield.app/device`
3. Tokens are cached locally at `~/.config/penfield-backup/auth.json` (permissions `0600`)
4. On subsequent runs, the cached token is reused or refreshed automatically

To remove the saved credentials from this computer: `python3 penfield_backup.py --logout`. This deletes the local file only. It does not end the authorization at Penfield, which expires on its own within 30 days if it is not used.

## Environment variables

| Variable   | Effect |
|------------|--------|
| `NO_COLOR` | Set to any value to disable colored output (see [no-color.org](https://no-color.org)) |

## Platform support

| Platform | Status |
|----------|--------|
| macOS    | Python 3 pre-installed via Xcode CLT |
| Linux    | Python 3 pre-installed on most distros |
| Windows  | Requires [Python 3](https://python.org/downloads) install |

## License

AGPL
