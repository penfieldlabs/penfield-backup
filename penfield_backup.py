#!/usr/bin/env python3
"""
Penfield Backup — Download all your Penfield data in one command.

    python3 penfield_backup.py

No installs. No dependencies. Just Python 3.

Output: a folder with memories.json + documents/ + artifacts/
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_BASE = "https://api.penfield.app"
AUTH_BASE = "https://auth.penfield.app"
DEVICE_AUTH_URL = f"{AUTH_BASE}/oauth/device_authorization"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
REGISTER_URL = f"{AUTH_BASE}/oauth/register"
SCOPES = "read offline_access"
VERSION = "1.0.1"
USER_AGENT = f"PenfieldBackup/{VERSION} (Python)"
HTTP_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
DEVICE_POLL_MAX = 300  # seconds — give up after 5 minutes

# ---------------------------------------------------------------------------
# Terminal output — colors, progress, helpers
# ---------------------------------------------------------------------------
_NO_COLOR = os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty()

BLUE = "" if _NO_COLOR else "\033[38;5;39m"
GREEN = "" if _NO_COLOR else "\033[32m"
YELLOW = "" if _NO_COLOR else "\033[33m"
RED = "" if _NO_COLOR else "\033[31m"
DIM = "" if _NO_COLOR else "\033[2m"
BOLD = "" if _NO_COLOR else "\033[1m"
RESET = "" if _NO_COLOR else "\033[0m"

# Enable ANSI on Windows 10+
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def banner():
    print(f"""
{BLUE}{BOLD}  Penfield Backup{RESET} {DIM}v{VERSION}{RESET}
{DIM}  Your data. Your copy. Always.{RESET}
""")


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def progress_bar(current: int, total: int, width: int = 30, label: str = ""):
    if total <= 0:
        return
    pct = min(current / total, 1.0)
    filled = int(width * pct)
    bar = f"{'█' * filled}{'░' * (width - filled)}"
    line = f"\r  {bar} {current}/{total} {DIM}{label[:35]}{RESET}"
    # Pad based on visible length (strip ANSI codes) to clear previous output
    visible_len = len(_ANSI_RE.sub("", line))
    pad = max(0, 80 - visible_len)
    sys.stdout.write(line + " " * pad)
    sys.stdout.flush()
    if current >= total:
        print()


def die(msg: str) -> None:
    print(f"\n  {RED}Error: {msg}{RESET}\n", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP helpers — zero-dep, stdlib only, with retries and timeouts
# ---------------------------------------------------------------------------
class NetworkError(Exception):
    """Raised on retriable network failures (connection refused, timeout, etc.)."""
    pass


def _make_request(url: str, method: str = "GET", body: bytes | None = None,
                  headers: dict | None = None) -> urllib.request.Request:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    req.add_header("User-Agent", USER_AGENT)
    return req


def http_json(url: str, method: str = "GET", data: dict | None = None,
              headers: dict | None = None,
              content_type: str = "application/json") -> tuple[int, dict | str]:
    """Make an HTTP request, return (status, parsed_json_or_body)."""
    hdrs = dict(headers or {})
    body = None

    if data is not None:
        if content_type == "application/x-www-form-urlencoded":
            body = urllib.parse.urlencode(data).encode()
        else:
            body = json.dumps(data).encode()
        hdrs["Content-Type"] = content_type

    req = _make_request(url, method, body, hdrs)

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw.decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise NetworkError(f"Network error: {e.reason}") from e
    except OSError as e:
        raise NetworkError(f"Connection failed: {e}") from e


def http_json_retry(url: str, method: str = "GET", data: dict | None = None,
                    headers: dict | None = None,
                    content_type: str = "application/json",
                    retries: int = MAX_RETRIES) -> tuple[int, dict | str]:
    """http_json with exponential backoff retries on 5xx / network errors."""
    last_status, last_data = 0, {}
    last_error: NetworkError | None = None
    for attempt in range(retries):
        try:
            status, resp_data = http_json(url, method, data, headers, content_type)
            if status < 500:
                return status, resp_data
            last_status, last_data = status, resp_data
            last_error = None
        except NetworkError as e:
            last_error = e
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    if last_error is not None:
        die(f"{last_error} (after {retries} attempts)")
    return last_status, last_data


def http_download(url: str, dest_path: str, headers: dict | None = None,
                  retries: int = MAX_RETRIES) -> bool:
    """Download a file as binary with retries. Returns True on success."""
    for attempt in range(retries):
        try:
            req = _make_request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            return True
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return False
    return False


# ---------------------------------------------------------------------------
# Token manager — auto-refreshes expired tokens mid-backup
# ---------------------------------------------------------------------------
class TokenManager:
    """Holds auth state and transparently refreshes tokens on 401."""

    def __init__(self, access_token: str, refresh_token: str | None,
                 client_id: str | None) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id

    def api_get(self, endpoint: str) -> tuple[int, dict | str]:
        """Authenticated GET with automatic token refresh on 401."""
        status, data = http_json_retry(
            f"{API_BASE}{endpoint}",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        if status == 401 and self._try_refresh():
            status, data = http_json_retry(
                f"{API_BASE}{endpoint}",
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
        return status, data

    def download(self, url: str, dest_path: str) -> bool:
        """Authenticated download with automatic token refresh on failure."""
        ok = http_download(url, dest_path,
                           headers={"Authorization": f"Bearer {self.access_token}"})
        if not ok and self._try_refresh():
            ok = http_download(url, dest_path,
                               headers={"Authorization": f"Bearer {self.access_token}"})
        return ok

    def _try_refresh(self) -> bool:
        """Attempt to refresh. Returns True if successful."""
        if not self.refresh_token or not self.client_id:
            return False
        result = try_refresh(self.client_id, self.refresh_token)
        if result:
            self.access_token, self.refresh_token = result
            save_auth(self.client_id, self.access_token, self.refresh_token)
            return True
        return False


# ---------------------------------------------------------------------------
# Auth cache — ~/.config/penfield-backup/auth.json (0600)
# ---------------------------------------------------------------------------
def _auth_path() -> Path:
    """Platform-appropriate config directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "penfield-backup" / "auth.json"


def load_auth() -> dict:
    """Load cached auth. Returns dict or empty dict."""
    p = _auth_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_auth(client_id: str, access_token: str, refresh_token: str | None) -> None:
    """Cache auth credentials with restrictive permissions."""
    p = _auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "client_id": client_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def clear_auth() -> None:
    """Remove cached auth."""
    p = _auth_path()
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Checkpoint — resume interrupted backups
# ---------------------------------------------------------------------------
def _checkpoint_path(output_dir: Path) -> Path:
    return output_dir / ".checkpoint.json"


def load_checkpoint(output_dir: Path) -> dict:
    p = _checkpoint_path(output_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_checkpoint(output_dir: Path, data: dict) -> None:
    p = _checkpoint_path(output_dir)
    p.write_text(json.dumps(data, indent=2))


def clear_checkpoint(output_dir: Path) -> None:
    p = _checkpoint_path(output_dir)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# OAuth — Dynamic Client Registration + Device Code Flow + Token Refresh
# ---------------------------------------------------------------------------
def register_client() -> str:
    """Register a dynamic OAuth client. Returns client_id."""
    status, data = http_json_retry(REGISTER_URL, method="POST", data={
        "client_name": "Penfield Backup",
        "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
        "redirect_uris": ["http://localhost/callback"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPES,
    })

    if status >= 400 or not isinstance(data, dict) or "client_id" not in data:
        detail = json.dumps(data, indent=2) if isinstance(data, dict) else str(data)
        die(f"Client registration failed ({status}): {detail}")

    return data["client_id"]


def try_refresh(client_id: str, refresh_token: str) -> tuple[str, str] | None:
    """Attempt to refresh the access token. Returns (access, refresh) or None."""
    try:
        status, tok = http_json(TOKEN_URL, method="POST", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }, content_type="application/x-www-form-urlencoded")
    except NetworkError:
        return None

    if status == 200 and isinstance(tok, dict) and "access_token" in tok:
        return tok["access_token"], tok.get("refresh_token", refresh_token)
    return None


def verify_token(token: str) -> bool:
    """Quick check that the token actually works."""
    try:
        status, _ = http_json(
            f"{API_BASE}/api/v2/memories?page=1&per_page=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        return status == 200
    except NetworkError:
        return False


def device_code_flow(client_id: str) -> tuple[str, str | None]:
    """Run the OAuth device code flow. Returns (access_token, refresh_token)."""
    status, data = http_json_retry(DEVICE_AUTH_URL, method="POST", data={
        "client_id": client_id,
        "scope": SCOPES,
    }, content_type="application/x-www-form-urlencoded")

    if status >= 400 or not isinstance(data, dict):
        die(f"Device authorization failed ({status}): {data}")

    user_code = data["user_code"]
    verification_uri = data.get("verification_uri_complete") or data["verification_uri"]
    device_code = data["device_code"]
    interval = data.get("interval", 5)

    print(f"  {BOLD}Open this URL and enter the code:{RESET}\n")
    print(f"  {BLUE}{verification_uri}{RESET}")
    print(f"  Code: {BOLD}{BLUE}{user_code}{RESET}\n")

    try:
        webbrowser.open(verification_uri)
        print(f"  {DIM}(Opened in your browser){RESET}")
    except Exception:
        pass

    print(f"\n  {DIM}Waiting for authorization...{RESET}", end="", flush=True)

    started = time.time()
    while time.time() - started < DEVICE_POLL_MAX:
        time.sleep(interval)
        try:
            status, tok = http_json(TOKEN_URL, method="POST", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            }, content_type="application/x-www-form-urlencoded")
        except NetworkError:
            print(".", end="", flush=True)
            continue

        if status == 200 and isinstance(tok, dict) and "access_token" in tok:
            print(f"\r  {GREEN}✓ Authorized{RESET}                        ")
            return tok["access_token"], tok.get("refresh_token")

        error = tok.get("error", "") if isinstance(tok, dict) else ""
        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if error == "slow_down":
            interval += 1
            continue
        if error == "expired_token":
            die("Device code expired. Please run again.")
        if error == "access_denied":
            die("Authorization denied by user.")
        if error:
            desc = tok.get("error_description", "") if isinstance(tok, dict) else ""
            die(f"Authorization error: {error} — {desc}")

    die("Authorization timed out. Please run again.")
    return "", None  # unreachable


def authenticate() -> TokenManager:
    """Get a valid token manager. Uses cache → refresh → device flow."""
    cached = load_auth()

    # Try cached access token
    if cached.get("access_token"):
        print(f"  {DIM}Checking saved credentials...{RESET}", end=" ", flush=True)
        if verify_token(cached["access_token"]):
            print(f"{GREEN}✓{RESET}")
            return TokenManager(cached["access_token"], cached.get("refresh_token"),
                                cached.get("client_id"))
        print(f"{YELLOW}expired{RESET}")

    # Try refresh token
    if cached.get("refresh_token") and cached.get("client_id"):
        print(f"  {DIM}Refreshing token...{RESET}", end=" ", flush=True)
        result = try_refresh(cached["client_id"], cached["refresh_token"])
        if result:
            access_token, refresh_token = result
            save_auth(cached["client_id"], access_token, refresh_token)
            print(f"{GREEN}✓{RESET}")
            return TokenManager(access_token, refresh_token, cached["client_id"])
        print(f"{YELLOW}failed{RESET}")

    # Full device code flow
    print(f"\n  {BOLD}Authorization required{RESET}")

    client_id = cached.get("client_id")
    if not client_id:
        print(f"  {DIM}Registering client...{RESET}", end=" ", flush=True)
        client_id = register_client()
        print(f"{GREEN}✓{RESET}")

    access_token, refresh_token = device_code_flow(client_id)
    save_auth(client_id, access_token, refresh_token)
    return TokenManager(access_token, refresh_token, client_id)


# ---------------------------------------------------------------------------
# Backup: Memories
# ---------------------------------------------------------------------------
INTERNAL_FIELDS = frozenset((
    "importance", "confidence", "strength", "surprise_score",
    "embedding_status", "access_count", "last_accessed",
))


def strip_internal(memory: dict) -> dict:
    """Remove internal scoring fields from a memory."""
    return {k: v for k, v in memory.items() if k not in INTERNAL_FIELDS}


def _write_memories_json(output_dir: Path, memories: list[dict]) -> None:
    """Write memories.json to disk (called per-page for crash safety)."""
    cleaned = [strip_internal(m) for m in memories]
    (output_dir / "memories.json").write_text(json.dumps({
        "memories": cleaned,
        "count": len(cleaned),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2, ensure_ascii=False))


def fetch_memories(tm: TokenManager, output_dir: Path,
                   checkpoint: dict) -> list[dict]:
    """Fetch all memories, paginated. Writes per-page. Returns list."""
    print(f"\n  {BOLD}Downloading memories...{RESET}")
    all_memories: list[dict] = []
    start_page = checkpoint.get("memories_page", 1)
    page = start_page
    per_page = 100
    total = 0

    # Load already-saved memories from a previous partial run
    if start_page > 1:
        mem_path = output_dir / "memories.json"
        if mem_path.exists():
            try:
                saved = json.loads(mem_path.read_text())
                all_memories = saved.get("memories", [])
                print(f"  {DIM}Resuming from page {start_page} "
                      f"({len(all_memories)} already saved){RESET}")
            except Exception:
                pass

    while True:
        status, data = tm.api_get(
            f"/api/v2/memories?page={page}&per_page={per_page}&sort=-created_at",
        )
        if status != 200:
            print(f"\n  {YELLOW}Warning: failed to fetch memories page {page} "
                  f"({status}){RESET}")
            break

        payload = data.get("data", {}) if isinstance(data, dict) else {}
        items = payload.get("items", [])
        pagination = payload.get("pagination", {})

        if page == 1 or total == 0:
            total = pagination.get("total", len(items))

        all_memories.extend(items)
        progress_bar(len(all_memories), total, label="memories")

        # Persist both data and checkpoint every page (crash-safe)
        _write_memories_json(output_dir, all_memories)
        checkpoint["memories_page"] = page + 1
        save_checkpoint(output_dir, checkpoint)

        if not pagination.get("has_next", False) or not items:
            break
        page += 1

    # Final write (in case loop exited without writing)
    _write_memories_json(output_dir, all_memories)

    return [strip_internal(m) for m in all_memories]


# ---------------------------------------------------------------------------
# Backup: Artifacts
# ---------------------------------------------------------------------------
def list_artifacts_recursive(tm: TokenManager, prefix: str = "/",
                             _depth: int = 0) -> list[str]:
    """Recursively list all artifact file paths (max depth 20)."""
    if _depth > 20:
        return []

    status, data = tm.api_get(
        f"/api/v2/artifacts/list?prefix={urllib.parse.quote(prefix)}",
    )
    if status != 200:
        return []

    listing = data.get("data", data) if isinstance(data, dict) else {}
    files: list[str] = []

    for f in listing.get("files", []):
        name = f.get("name") or f.get("path") or str(f) if isinstance(f, dict) else str(f)
        full_path = f"/{name}" if prefix == "/" else f"{prefix}{name}"
        files.append(full_path)

    for folder in listing.get("folders", []):
        folder_path = f"/{folder}/" if prefix == "/" else f"{prefix}{folder}/"
        files.extend(list_artifacts_recursive(tm, folder_path, _depth + 1))

    return files


def _safe_artifact_path(file_path: str) -> str:
    """Sanitize an artifact path for safe local writing."""
    safe = file_path.lstrip("/")
    safe = safe.replace("..", "_")
    safe = re.sub(r"^[a-zA-Z]:", "", safe)  # Windows drive letters
    return safe


# Binary content detection for artifacts
_BINARY_PREFIXES = ("image/", "audio/", "video/", "application/octet-stream")
_BINARY_TYPES = frozenset({
    "application/pdf", "application/zip", "application/gzip",
    "application/x-tar", "application/wasm",
})


def _is_binary_type(content_type: str) -> bool:
    """Return True if the content type indicates binary data."""
    ct = content_type.strip().lower()
    if any(ct.startswith(p) for p in _BINARY_PREFIXES):
        return True
    return ct in _BINARY_TYPES


def fetch_artifacts(tm: TokenManager, output_dir: Path,
                    checkpoint: dict) -> int:
    """Download all artifacts preserving directory structure."""
    print(f"\n  {BOLD}Downloading artifacts...{RESET}")

    all_files = list_artifacts_recursive(tm)
    if not all_files:
        print(f"  {DIM}No artifacts found.{RESET}")
        return 0

    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Resume support: skip already-downloaded paths
    done_paths: set[str] = set(checkpoint.get("artifacts_done", []))
    if done_paths:
        print(f"  {DIM}Resuming ({len(done_paths)}/{len(all_files)} already done){RESET}")

    downloaded = len(done_paths)

    for i, file_path in enumerate(all_files):
        progress_bar(i + 1, len(all_files), label=Path(file_path).name)

        if file_path in done_paths:
            continue

        status, data = tm.api_get(
            f"/api/v2/artifacts?path={urllib.parse.quote(file_path)}",
        )
        if status != 200:
            continue

        content = ""
        content_type = ""
        if isinstance(data, dict):
            inner = data.get("data", {})
            if isinstance(inner, dict):
                content = inner.get("content", "")
                content_type = inner.get("content_type", "")
            else:
                content = data.get("content", "")
                content_type = data.get("content_type", "")

        # Determine if binary from API content_type or file extension
        if not content_type:
            content_type = mimetypes.guess_type(file_path)[0] or ""
        binary = _is_binary_type(content_type)

        safe_path = _safe_artifact_path(file_path)
        if not safe_path:
            continue

        dest = artifacts_dir / safe_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        if binary and isinstance(content, str):
            try:
                dest.write_bytes(base64.b64decode(content))
            except Exception:
                print(f"\n  {YELLOW}Warning: could not decode binary "
                      f"content for {file_path}, saving as text{RESET}")
                dest.write_text(content, encoding="utf-8")
        else:
            dest.write_text(content, encoding="utf-8")

        downloaded += 1
        done_paths.add(file_path)
        checkpoint["artifacts_done"] = list(done_paths)
        save_checkpoint(output_dir, checkpoint)

    return downloaded


# ---------------------------------------------------------------------------
# Backup: Documents
# ---------------------------------------------------------------------------
def fetch_documents(tm: TokenManager, output_dir: Path,
                    checkpoint: dict) -> int:
    """Download all documents (original uploaded files) as binary."""
    print(f"\n  {BOLD}Downloading documents...{RESET}")

    # List all documents (paginated)
    all_docs: list[dict] = []
    page = 1

    while True:
        status, data = tm.api_get(f"/api/v2/documents?page={page}&per_page=100")
        if status != 200:
            break
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        items = payload.get("items", data.get("items", []) if isinstance(data, dict) else [])
        all_docs.extend(items)
        has_next = payload.get("pagination", {}).get("has_next", False)
        if not has_next or not items:
            break
        page += 1

    if not all_docs:
        print(f"  {DIM}No documents found.{RESET}")
        return 0

    documents_dir = output_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)

    # Write metadata index
    doc_meta = [{
        "id": d.get("id"),
        "filename": d.get("filename"),
        "content_type": d.get("content_type"),
        "size": d.get("size"),
        "created_at": d.get("created_at"),
    } for d in all_docs]
    (documents_dir / "documents.json").write_text(
        json.dumps(doc_meta, indent=2, ensure_ascii=False),
    )

    # Resume support: track by document ID
    done_ids: set[str] = set(checkpoint.get("documents_done", []))
    if done_ids:
        print(f"  {DIM}Resuming ({len(done_ids)}/{len(all_docs)} already done){RESET}")

    # Build filename map (deterministic)
    seen_names: set[str] = set()
    filenames: list[str] = []
    for i, doc in enumerate(all_docs):
        filename = doc.get("filename") or doc.get("id", f"document_{i}")
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
        base_name = safe_name
        counter = 1
        while safe_name in seen_names:
            stem = Path(base_name).stem
            ext = Path(base_name).suffix
            counter += 1
            safe_name = f"{stem}-{counter}{ext}"
        seen_names.add(safe_name)
        filenames.append(safe_name)

    downloaded = len(done_ids)

    for i, doc in enumerate(all_docs):
        safe_name = filenames[i]
        doc_id = str(doc.get("id", ""))

        progress_bar(i + 1, len(all_docs), label=safe_name)

        if doc_id in done_ids:
            continue

        dest = documents_dir / safe_name
        ok = tm.download(
            f"{API_BASE}/api/v2/documents/{doc_id}/download",
            str(dest),
        )
        if not ok:
            continue

        downloaded += 1
        done_ids.add(doc_id)
        checkpoint["documents_done"] = list(done_ids)
        save_checkpoint(output_dir, checkpoint)

    return downloaded


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_backup(output_dir: Path, expected_memories: int,
                  expected_artifacts: int, expected_documents: int) -> bool:
    """Verify the backup wrote what we expected. Returns True if clean."""
    issues: list[str] = []

    # Check memories.json
    mem_path = output_dir / "memories.json"
    if mem_path.exists():
        try:
            data = json.loads(mem_path.read_text())
            actual = len(data.get("memories", []))
            if actual != expected_memories:
                issues.append(
                    f"memories.json has {actual} memories, expected {expected_memories}")
        except Exception as e:
            issues.append(f"memories.json is not valid JSON: {e}")
    else:
        issues.append("memories.json is missing")

    # Count artifact files on disk
    artifacts_dir = output_dir / "artifacts"
    if artifacts_dir.exists():
        actual_artifacts = sum(1 for _ in artifacts_dir.rglob("*") if _.is_file())
        if actual_artifacts != expected_artifacts:
            issues.append(
                f"artifacts/ has {actual_artifacts} files, expected {expected_artifacts}")

    # Count document files on disk (minus documents.json)
    documents_dir = output_dir / "documents"
    if documents_dir.exists():
        actual_docs = sum(1 for f in documents_dir.iterdir()
                          if f.is_file() and f.name != "documents.json")
        if actual_docs != expected_documents:
            issues.append(
                f"documents/ has {actual_docs} files, expected {expected_documents}")

    # Check manifest
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        issues.append("manifest.json is missing")

    if issues:
        print(f"\n  {YELLOW}{BOLD}⚠ Verification warnings:{RESET}")
        for issue in issues:
            print(f"  {YELLOW}  - {issue}{RESET}")
        return False

    print(f"\n  {GREEN}✓ Verification passed{RESET}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="penfield-backup",
        description="Download all your Penfield data in one command.",
        epilog=f"Credentials cached in: {_auth_path()}",
    )
    parser.add_argument(
        "-o", "--output",
        default="penfield-backup",
        help="output folder (default: ./penfield-backup)",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="remove cached credentials and exit",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an interrupted backup",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


def main() -> None:
    banner()
    args = parse_args()

    if args.logout:
        clear_auth()
        print(f"  {GREEN}✓ Logged out.{RESET} Cached credentials removed.\n")
        return

    output_dir = Path(args.output)

    # Step 1: Authenticate (returns TokenManager with auto-refresh)
    tm = authenticate()

    # Step 2: Load checkpoint if resuming
    checkpoint: dict = {}
    if args.resume:
        checkpoint = load_checkpoint(output_dir)
        if checkpoint:
            print(f"\n  {DIM}Resuming previous backup...{RESET}")
        else:
            print(f"\n  {DIM}No checkpoint found, starting fresh.{RESET}")

    # Step 3: Backup
    print(f"\n  {BOLD}Backing up your data{RESET}")
    print(f"  Saving to: {BLUE}{output_dir.resolve()}{RESET}")
    output_dir.mkdir(parents=True, exist_ok=True)

    memories = fetch_memories(tm, output_dir, checkpoint)
    artifact_count = fetch_artifacts(tm, output_dir, checkpoint)
    document_count = fetch_documents(tm, output_dir, checkpoint)

    # Write manifest
    manifest = {
        "penfield_backup_version": VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": {
            "memories": len(memories),
            "artifacts": artifact_count,
            "documents": document_count,
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Step 4: Verify
    if verify_backup(output_dir, len(memories), artifact_count, document_count):
        clear_checkpoint(output_dir)

    # Summary
    print(f"\n  {GREEN}{BOLD}✓ Backup complete!{RESET}\n")
    print(f"  {BOLD}{len(memories)}{RESET} memories")
    print(f"  {BOLD}{artifact_count}{RESET} artifacts")
    print(f"  {BOLD}{document_count}{RESET} documents")
    print(f"\n  Saved to: {BLUE}{output_dir.resolve()}{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {DIM}Cancelled. Run with --resume to continue.{RESET}\n")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n  {RED}Unexpected error: {type(e).__name__}: {e}{RESET}\n", file=sys.stderr)
        sys.exit(1)
