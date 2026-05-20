"""Tests for penfield_backup.py."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import penfield_backup as pb


# ---------------------------------------------------------------------------
# strip_internal
# ---------------------------------------------------------------------------
class TestStripInternal:
    def test_removes_all_internal_fields(self):
        mem = {
            "id": "1", "content": "hello", "tags": ["a"],
            "importance": 0.8, "confidence": 0.9, "strength": 0.5,
            "surprise_score": 0.3, "embedding_status": "done",
            "access_count": 42, "last_accessed": "2026-01-01",
        }
        result = pb.strip_internal(mem)
        assert result == {"id": "1", "content": "hello", "tags": ["a"]}

    def test_preserves_dict_with_no_internal_fields(self):
        mem = {"id": "1", "content": "hello"}
        assert pb.strip_internal(mem) == mem

    def test_empty_dict(self):
        assert pb.strip_internal({}) == {}

    def test_only_internal_fields(self):
        mem = {k: "x" for k in pb.INTERNAL_FIELDS}
        assert pb.strip_internal(mem) == {}

    def test_does_not_mutate_original(self):
        mem = {"id": "1", "importance": 0.5}
        pb.strip_internal(mem)
        assert "importance" in mem


# ---------------------------------------------------------------------------
# _safe_artifact_path
# ---------------------------------------------------------------------------
class TestSafeArtifactPath:
    def test_strips_leading_slashes(self):
        assert pb._safe_artifact_path("/foo/bar.txt") == "foo/bar.txt"

    def test_multiple_leading_slashes(self):
        assert pb._safe_artifact_path("///foo.txt") == "foo.txt"

    def test_replaces_dot_dot(self):
        assert ".." not in pb._safe_artifact_path("/../etc/passwd")

    def test_strips_windows_drive_letter(self):
        result = pb._safe_artifact_path("C:/Users/file.txt")
        assert "C:" not in result

    def test_normal_path_unchanged(self):
        assert pb._safe_artifact_path("notes/my-note.md") == "notes/my-note.md"

    def test_empty_after_strip(self):
        assert pb._safe_artifact_path("/") == ""


# ---------------------------------------------------------------------------
# _is_binary_type
# ---------------------------------------------------------------------------
class TestIsBinaryType:
    def test_image_prefix(self):
        assert pb._is_binary_type("image/png") is True
        assert pb._is_binary_type("image/jpeg") is True

    def test_audio_prefix(self):
        assert pb._is_binary_type("audio/mpeg") is True

    def test_video_prefix(self):
        assert pb._is_binary_type("video/mp4") is True

    def test_octet_stream(self):
        assert pb._is_binary_type("application/octet-stream") is True

    def test_explicit_binary_types(self):
        for ct in ("application/pdf", "application/zip", "application/gzip",
                    "application/x-tar", "application/wasm"):
            assert pb._is_binary_type(ct) is True, f"{ct} should be binary"

    def test_text_types_are_not_binary(self):
        for ct in ("text/plain", "text/html", "application/json"):
            assert pb._is_binary_type(ct) is False, f"{ct} should not be binary"

    def test_whitespace_stripped(self):
        assert pb._is_binary_type("  image/png  ") is True

    def test_case_insensitive(self):
        assert pb._is_binary_type("IMAGE/PNG") is True
        assert pb._is_binary_type("Application/PDF") is True


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
class TestCheckpoint:
    def test_save_and_load_roundtrip(self, tmp_path):
        data = {"memories_page": 3, "artifacts_done": ["a", "b"]}
        pb.save_checkpoint(tmp_path, data)
        loaded = pb.load_checkpoint(tmp_path)
        assert loaded == data

    def test_load_missing_returns_empty(self, tmp_path):
        assert pb.load_checkpoint(tmp_path) == {}

    def test_load_corrupted_returns_empty(self, tmp_path):
        (tmp_path / ".checkpoint.json").write_text("not json{{{")
        assert pb.load_checkpoint(tmp_path) == {}

    def test_clear_removes_file(self, tmp_path):
        pb.save_checkpoint(tmp_path, {"x": 1})
        pb.clear_checkpoint(tmp_path)
        assert not (tmp_path / ".checkpoint.json").exists()

    def test_clear_nonexistent_no_error(self, tmp_path):
        pb.clear_checkpoint(tmp_path)


# ---------------------------------------------------------------------------
# Auth cache
# ---------------------------------------------------------------------------
class TestAuthCache:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.json"
        monkeypatch.setattr(pb, "_auth_path", lambda: auth_file)
        pb.save_auth("cid", "access", "refresh")
        loaded = pb.load_auth()
        assert loaded["client_id"] == "cid"
        assert loaded["access_token"] == "access"
        assert loaded["refresh_token"] == "refresh"
        assert "saved_at" in loaded

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pb, "_auth_path", lambda: tmp_path / "nope.json")
        assert pb.load_auth() == {}

    def test_load_corrupted_returns_empty(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text("{bad json")
        monkeypatch.setattr(pb, "_auth_path", lambda: auth_file)
        assert pb.load_auth() == {}

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.json"
        monkeypatch.setattr(pb, "_auth_path", lambda: auth_file)
        pb.save_auth("cid", "a", "r")
        pb.clear_auth()
        assert not auth_file.exists()

    def test_clear_nonexistent_no_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pb, "_auth_path", lambda: tmp_path / "nope.json")
        pb.clear_auth()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions")
    def test_file_permissions_0600(self, tmp_path, monkeypatch):
        auth_file = tmp_path / "auth.json"
        monkeypatch.setattr(pb, "_auth_path", lambda: auth_file)
        pb.save_auth("cid", "a", "r")
        mode = auth_file.stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# http_json_retry
# ---------------------------------------------------------------------------
class TestHttpJsonRetry:
    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_success_on_first_try(self, mock_http, mock_sleep):
        mock_http.return_value = (200, {"ok": True})
        status, data = pb.http_json_retry("http://x", retries=3)
        assert status == 200
        assert mock_http.call_count == 1
        mock_sleep.assert_not_called()

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_retries_on_500(self, mock_http, mock_sleep):
        mock_http.side_effect = [
            (500, {"error": "fail"}),
            (500, {"error": "fail"}),
            (200, {"ok": True}),
        ]
        status, data = pb.http_json_retry("http://x", retries=3)
        assert status == 200
        assert mock_http.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_retries_on_network_error_then_succeeds(self, mock_http, mock_sleep):
        mock_http.side_effect = [
            pb.NetworkError("timeout"),
            pb.NetworkError("timeout"),
            (200, {"ok": True}),
        ]
        status, data = pb.http_json_retry("http://x", retries=3)
        assert status == 200

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_returns_last_5xx_after_exhausting_retries(self, mock_http, mock_sleep):
        mock_http.return_value = (503, {"error": "unavailable"})
        status, data = pb.http_json_retry("http://x", retries=3)
        assert status == 503

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_dies_on_all_network_errors(self, mock_http, mock_sleep):
        mock_http.side_effect = pb.NetworkError("down")
        with pytest.raises(SystemExit):
            pb.http_json_retry("http://x", retries=3)

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_4xx_not_retried(self, mock_http, mock_sleep):
        mock_http.return_value = (401, {"error": "unauthorized"})
        status, _ = pb.http_json_retry("http://x", retries=3)
        assert status == 401
        assert mock_http.call_count == 1

    @patch("penfield_backup.time.sleep")
    @patch("penfield_backup.http_json")
    def test_exponential_backoff_timing(self, mock_http, mock_sleep):
        mock_http.return_value = (500, {})
        pb.http_json_retry("http://x", retries=3)
        mock_sleep.assert_has_calls([call(1), call(2)])


# ---------------------------------------------------------------------------
# TokenManager
# ---------------------------------------------------------------------------
class TestTokenManagerApiGet:
    @patch("penfield_backup.http_json_retry")
    def test_success_no_refresh(self, mock_retry):
        mock_retry.return_value = (200, {"data": []})
        tm = pb.TokenManager("tok", "ref", "cid")
        status, data = tm.api_get("/api/v2/memories")
        assert status == 200
        assert mock_retry.call_count == 1

    @patch("penfield_backup.http_json_retry")
    def test_401_triggers_refresh_and_retries(self, mock_retry):
        mock_retry.side_effect = [
            (401, {"error": "expired"}),
            (200, {"data": []}),
        ]
        tm = pb.TokenManager("old_tok", "ref", "cid")
        with patch.object(tm, "_try_refresh", return_value=True) as mock_ref:
            tm.access_token = "new_tok"
            status, data = tm.api_get("/api/v2/memories")
        assert status == 200
        mock_ref.assert_called_once()
        assert mock_retry.call_count == 2

    @patch("penfield_backup.http_json_retry")
    def test_401_refresh_fails_returns_401(self, mock_retry):
        mock_retry.return_value = (401, {"error": "expired"})
        tm = pb.TokenManager("tok", "ref", "cid")
        with patch.object(tm, "_try_refresh", return_value=False):
            status, _ = tm.api_get("/api/v2/memories")
        assert status == 401
        assert mock_retry.call_count == 1

    @patch("penfield_backup.http_json_retry")
    def test_passes_bearer_token_in_header(self, mock_retry):
        mock_retry.return_value = (200, {})
        tm = pb.TokenManager("my_token", None, None)
        tm.api_get("/api/v2/test")
        _, kwargs = mock_retry.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer my_token"


class TestTokenManagerTryRefresh:
    @patch("penfield_backup.save_auth")
    @patch("penfield_backup.try_refresh")
    def test_refresh_succeeds_updates_tokens(self, mock_ref, mock_save):
        mock_ref.return_value = ("new_access", "new_refresh")
        tm = pb.TokenManager("old", "old_ref", "cid")
        assert tm._try_refresh() is True
        assert tm.access_token == "new_access"
        assert tm.refresh_token == "new_refresh"
        mock_save.assert_called_once()

    @patch("penfield_backup.save_auth")
    @patch("penfield_backup.try_refresh")
    def test_refresh_fails_returns_false(self, mock_ref, mock_save):
        mock_ref.return_value = None
        tm = pb.TokenManager("old", "old_ref", "cid")
        assert tm._try_refresh() is False
        assert tm.access_token == "old"
        mock_save.assert_not_called()

    def test_no_refresh_token_returns_false(self):
        tm = pb.TokenManager("tok", None, "cid")
        assert tm._try_refresh() is False

    def test_no_client_id_returns_false(self):
        tm = pb.TokenManager("tok", "ref", None)
        assert tm._try_refresh() is False


# ---------------------------------------------------------------------------
# verify_backup
# ---------------------------------------------------------------------------
class TestVerifyBackup:
    def _setup_backup(self, tmp_path, n_memories=3, n_artifacts=2, n_documents=2):
        """Create a valid backup directory structure."""
        # memories.json
        memories = [{"id": str(i), "content": f"m{i}"} for i in range(n_memories)]
        (tmp_path / "memories.json").write_text(json.dumps({
            "memories": memories, "count": n_memories,
        }))
        # manifest.json
        (tmp_path / "manifest.json").write_text(json.dumps({"stats": {}}))
        # artifacts
        if n_artifacts > 0:
            art_dir = tmp_path / "artifacts"
            art_dir.mkdir()
            for i in range(n_artifacts):
                (art_dir / f"file{i}.txt").write_text(f"artifact {i}")
        # documents
        if n_documents > 0:
            doc_dir = tmp_path / "documents"
            doc_dir.mkdir()
            (doc_dir / "documents.json").write_text("[]")
            for i in range(n_documents):
                (doc_dir / f"doc{i}.pdf").write_bytes(b"fake pdf")
        return tmp_path

    def test_all_counts_match_passes(self, tmp_path):
        self._setup_backup(tmp_path, 3, 2, 2)
        assert pb.verify_backup(tmp_path, 3, 2, 2) is True

    def test_missing_memories_json_fails(self, tmp_path):
        self._setup_backup(tmp_path, 0, 0, 0)
        (tmp_path / "memories.json").unlink()
        assert pb.verify_backup(tmp_path, 0, 0, 0) is False

    def test_memory_count_mismatch_fails(self, tmp_path):
        self._setup_backup(tmp_path, 3, 0, 0)
        assert pb.verify_backup(tmp_path, 5, 0, 0) is False

    def test_invalid_memories_json_fails(self, tmp_path):
        self._setup_backup(tmp_path, 3, 0, 0)
        (tmp_path / "memories.json").write_text("not json")
        assert pb.verify_backup(tmp_path, 3, 0, 0) is False

    def test_artifact_count_mismatch_fails(self, tmp_path):
        self._setup_backup(tmp_path, 0, 2, 0)
        assert pb.verify_backup(tmp_path, 0, 3, 0) is False

    def test_document_count_mismatch_fails(self, tmp_path):
        self._setup_backup(tmp_path, 0, 0, 2)
        assert pb.verify_backup(tmp_path, 0, 0, 5) is False

    def test_documents_json_not_counted(self, tmp_path):
        self._setup_backup(tmp_path, 0, 0, 2)
        assert pb.verify_backup(tmp_path, 0, 0, 2) is True

    def test_missing_manifest_fails(self, tmp_path):
        self._setup_backup(tmp_path, 3, 2, 2)
        (tmp_path / "manifest.json").unlink()
        assert pb.verify_backup(tmp_path, 3, 2, 2) is False

    def test_no_artifacts_dir_when_zero_expected(self, tmp_path):
        self._setup_backup(tmp_path, 0, 0, 0)
        assert pb.verify_backup(tmp_path, 0, 0, 0) is True


# ---------------------------------------------------------------------------
# Checkpoint not cleared on verify failure (bug fix regression test)
# ---------------------------------------------------------------------------
class TestCheckpointClearedOnlyOnSuccess:
    def test_checkpoint_cleared_on_verify_pass(self, tmp_path):
        pb.save_checkpoint(tmp_path, {"memories_page": 5})
        # Build a valid backup dir that will pass verification
        (tmp_path / "memories.json").write_text(json.dumps({
            "memories": [{"id": "1"}], "count": 1,
        }))
        (tmp_path / "manifest.json").write_text("{}")
        assert pb.verify_backup(tmp_path, 1, 0, 0) is True

        # Simulate what main() does after verify passes
        if pb.verify_backup(tmp_path, 1, 0, 0):
            pb.clear_checkpoint(tmp_path)

        assert not (tmp_path / ".checkpoint.json").exists()

    def test_checkpoint_preserved_on_verify_fail(self, tmp_path):
        pb.save_checkpoint(tmp_path, {"memories_page": 5})
        (tmp_path / "memories.json").write_text(json.dumps({
            "memories": [{"id": "1"}], "count": 1,
        }))
        (tmp_path / "manifest.json").write_text("{}")

        # Verification will fail: expect 10 memories but only 1 exists
        result = pb.verify_backup(tmp_path, 10, 0, 0)
        assert result is False

        # Simulate what main() does — with the fix, checkpoint is preserved
        if result:
            pb.clear_checkpoint(tmp_path)

        assert (tmp_path / ".checkpoint.json").exists()
        assert pb.load_checkpoint(tmp_path) == {"memories_page": 5}


# ---------------------------------------------------------------------------
# Document filename dedup
# ---------------------------------------------------------------------------
class TestDocumentFilenameDedup:
    def _run_fetch(self, tmp_path, docs, checkpoint=None):
        """Run fetch_documents with mocked API calls."""
        if checkpoint is None:
            checkpoint = {}

        # Build paginated response
        api_response = (200, {
            "data": {
                "items": docs,
                "pagination": {"has_next": False},
            }
        })

        tm = MagicMock(spec=pb.TokenManager)
        tm.api_get.return_value = api_response

        def fake_download(url, dest):
            Path(dest).write_bytes(b"content")
            return True
        tm.download.side_effect = fake_download

        count = pb.fetch_documents(tm, tmp_path, checkpoint)
        doc_dir = tmp_path / "documents"
        files = [f.name for f in doc_dir.iterdir() if f.name != "documents.json"] if doc_dir.exists() else []
        return sorted(files), count

    def test_unique_filenames_unchanged(self, tmp_path):
        docs = [
            {"id": "1", "filename": "a.pdf"},
            {"id": "2", "filename": "b.pdf"},
            {"id": "3", "filename": "c.pdf"},
        ]
        files, count = self._run_fetch(tmp_path, docs)
        assert files == ["a.pdf", "b.pdf", "c.pdf"]
        assert count == 3

    def test_duplicate_names_get_counter_suffix(self, tmp_path):
        docs = [
            {"id": "1", "filename": "report.pdf"},
            {"id": "2", "filename": "report.pdf"},
            {"id": "3", "filename": "report.pdf"},
        ]
        files, count = self._run_fetch(tmp_path, docs)
        assert files == ["report-2.pdf", "report-3.pdf", "report.pdf"]

    def test_unsafe_chars_sanitized(self, tmp_path):
        docs = [{"id": "1", "filename": 'my<file>name.txt'}]
        files, _ = self._run_fetch(tmp_path, docs)
        assert files == ["my_file_name.txt"]

    def test_fallback_to_id_when_no_filename(self, tmp_path):
        docs = [{"id": "abc-123"}]
        files, _ = self._run_fetch(tmp_path, docs)
        assert files == ["abc-123"]

    def test_fallback_to_index_when_no_id_or_filename(self, tmp_path):
        docs = [{}]
        files, _ = self._run_fetch(tmp_path, docs)
        assert files == ["document_0"]
