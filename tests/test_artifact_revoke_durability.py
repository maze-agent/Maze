import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from maze.core.files import artifact_store as artifact_store_module
from maze.core.files.artifact_store import (
    MANAGED_BLOB_PROVENANCE_MIGRATION,
    LocalCASArtifactStore,
    sha256_bytes,
)


def _access_counts(store: LocalCASArtifactStore):
    with store._connect_access_db() as connection:
        return {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "capabilities",
                "capability_owners",
                "artifact_grants",
                "artifact_access",
            )
        }


def _private_blob(store: LocalCASArtifactStore, owner_id: str, payload: bytes):
    capability = store.create_capability(owner_id=owner_id)
    sha256 = sha256_bytes(payload)
    store.put_bytes(
        sha256,
        payload,
        private=True,
        capability=capability,
    )
    return sha256, capability


def test_legacy_access_schema_migration_commits_before_write_lock(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    access_db = root / "access.sqlite3"
    with sqlite3.connect(access_db) as connection:
        connection.execute(
            """
            CREATE TABLE artifact_access (
                sha256 TEXT PRIMARY KEY,
                private INTEGER NOT NULL CHECK (private IN (0, 1)),
                updated_time REAL NOT NULL
            )
            """
        )

    store = LocalCASArtifactStore(root)
    assert not store.is_private("a" * 64)
    with sqlite3.connect(access_db) as connection:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(artifact_access)"
            ).fetchall()
        }
    assert "managed_blob" in columns


@pytest.mark.parametrize("managed_column_exists", [False, True])
def test_legacy_private_rows_migrate_fail_closed_and_revoke_retry(
    tmp_path,
    monkeypatch,
    managed_column_exists,
):
    root = tmp_path / "artifacts"
    root.mkdir()
    payload = b"legacy private artifact"
    sha256 = sha256_bytes(payload)
    capability = "legacy-private-capability"
    capability_hash = LocalCASArtifactStore._capability_hash(capability)
    blob_path = root / "blobs" / sha256[:2] / sha256[2:4] / sha256
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(payload)
    access_db = root / "access.sqlite3"
    managed_column = (
        ", managed_blob INTEGER NOT NULL DEFAULT 0 "
        "CHECK (managed_blob IN (0, 1))"
        if managed_column_exists
        else ""
    )
    with sqlite3.connect(access_db) as connection:
        connection.executescript(
            f"""
            CREATE TABLE capabilities (
                capability_hash TEXT PRIMARY KEY,
                created_time REAL NOT NULL
            );
            CREATE TABLE artifact_access (
                sha256 TEXT PRIMARY KEY,
                private INTEGER NOT NULL CHECK (private IN (0, 1)),
                updated_time REAL NOT NULL
                {managed_column}
            );
            CREATE TABLE artifact_grants (
                sha256 TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                created_time REAL NOT NULL,
                PRIMARY KEY (sha256, capability_hash),
                FOREIGN KEY (sha256) REFERENCES artifact_access(sha256) ON DELETE CASCADE,
                FOREIGN KEY (capability_hash) REFERENCES capabilities(capability_hash) ON DELETE CASCADE
            );
            CREATE TABLE capability_owners (
                owner_id TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                created_time REAL NOT NULL,
                PRIMARY KEY (owner_id, capability_hash),
                FOREIGN KEY (capability_hash) REFERENCES capabilities(capability_hash) ON DELETE CASCADE
            );
            """
        )
        connection.execute(
            "INSERT INTO capabilities VALUES (?, ?)",
            (capability_hash, 1.0),
        )
        if managed_column_exists:
            connection.execute(
                "INSERT INTO artifact_access VALUES (?, 1, ?, 0)",
                (sha256, 1.0),
            )
        else:
            connection.execute(
                "INSERT INTO artifact_access VALUES (?, 1, ?)",
                (sha256, 1.0),
            )
        connection.execute(
            "INSERT INTO artifact_grants VALUES (?, ?, ?)",
            (sha256, capability_hash, 1.0),
        )
        connection.execute(
            "INSERT INTO capability_owners VALUES (?, ?, ?)",
            ("legacy-run", capability_hash, 1.0),
        )

    store = LocalCASArtifactStore(root)
    assert store.is_private(sha256)
    assert not store.can_read(sha256)
    assert store.can_read(sha256, capability)
    with sqlite3.connect(access_db) as connection:
        assert connection.execute(
            "SELECT managed_blob FROM artifact_access WHERE sha256 = ?",
            (sha256,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT 1 FROM artifact_store_migrations WHERE name = ?",
            (MANAGED_BLOB_PROVENANCE_MIGRATION,),
        ).fetchone() == (1,)

    real_unlink = Path.unlink
    failed_once = False

    def fail_first_blob_unlink(path, *args, **kwargs):
        nonlocal failed_once
        if path == blob_path and not failed_once:
            failed_once = True
            raise OSError("injected legacy blob unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_blob_unlink)
    with pytest.raises(OSError, match="legacy blob unlink failure"):
        store.revoke_owner_capabilities("legacy-run")
    assert store.exists(sha256)
    assert store.is_private(sha256)
    assert not store.can_read(sha256)
    assert not store.can_read(sha256, capability)

    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = LocalCASArtifactStore(root)
    assert restarted.revoke_owner_capabilities("legacy-run") == 0
    assert not restarted.exists(sha256)
    assert not restarted.is_private(sha256)
    assert _access_counts(restarted)["artifact_access"] == 0


def test_unlink_failure_keeps_private_tombstone_and_retry_reaps_it(
    tmp_path,
    monkeypatch,
):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    sha256, capability = _private_blob(store, "run-1", b"private")
    blob_path = store.blob_path(sha256)
    real_unlink = Path.unlink

    def fail_blob_unlink(path, *args, **kwargs):
        if path == blob_path:
            raise OSError("injected blob unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_blob_unlink)
    with pytest.raises(OSError, match="injected blob unlink failure"):
        store.revoke_owner_capabilities("run-1")

    assert store.exists(sha256)
    assert store.is_private(sha256)
    assert not store.can_read(sha256)
    assert not store.can_read(sha256, capability)
    with pytest.raises(PermissionError):
        store.get_file(sha256, tmp_path / "anonymous-copy")
    assert _access_counts(store) == {
        "capabilities": 0,
        "capability_owners": 0,
        "artifact_grants": 0,
        "artifact_access": 1,
    }

    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = LocalCASArtifactStore(store.root)
    assert restarted.revoke_owner_capabilities("run-1") == 0
    assert not restarted.exists(sha256)
    assert _access_counts(restarted) == {
        "capabilities": 0,
        "capability_owners": 0,
        "artifact_grants": 0,
        "artifact_access": 0,
    }


def test_crash_after_unlink_keeps_retryable_private_tombstone(
    tmp_path,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    store = LocalCASArtifactStore(tmp_path / "artifacts")
    sha256, _ = _private_blob(store, "run-1", b"private")
    blob_path = store.blob_path(sha256)
    real_unlink = Path.unlink

    def unlink_then_crash(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == blob_path:
            raise SimulatedProcessCrash()
        return result

    monkeypatch.setattr(Path, "unlink", unlink_then_crash)
    with pytest.raises(SimulatedProcessCrash):
        store.revoke_owner_capabilities("run-1")

    assert not store.exists(sha256)
    assert store.is_private(sha256)
    assert not store.can_read(sha256)
    assert _access_counts(store)["artifact_access"] == 1

    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = LocalCASArtifactStore(store.root)
    assert restarted.revoke_owner_capabilities("run-1") == 0
    assert not restarted.is_private(sha256)
    assert _access_counts(restarted)["artifact_access"] == 0


def test_directory_fsync_failure_preserves_tombstone_until_retry(
    tmp_path,
    monkeypatch,
):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    sha256, _ = _private_blob(store, "run-1", b"private")
    blob_parent = store.blob_path(sha256).parent
    real_fsync_directory = artifact_store_module._fsync_directory

    def fail_blob_directory_fsync(path):
        if Path(path) == blob_parent:
            raise OSError("injected blob directory fsync failure")
        return real_fsync_directory(path)

    monkeypatch.setattr(
        artifact_store_module,
        "_fsync_directory",
        fail_blob_directory_fsync,
    )
    with pytest.raises(OSError, match="blob directory fsync failure"):
        store.revoke_owner_capabilities("run-1")

    assert not store.exists(sha256)
    assert store.is_private(sha256)
    assert _access_counts(store)["artifact_access"] == 1

    monkeypatch.setattr(
        artifact_store_module,
        "_fsync_directory",
        real_fsync_directory,
    )
    assert store.revoke_owner_capabilities("run-1") == 0
    assert not store.is_private(sha256)


def test_concurrent_new_grant_recreates_managed_shared_sha_after_reap(
    tmp_path,
    monkeypatch,
):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"shared private blob"
    sha256, first = _private_blob(store, "run-1", payload)
    second = store.create_capability(owner_id="run-2")
    blob_path = store.blob_path(sha256)
    unlink_entered = threading.Event()
    release_unlink = threading.Event()
    second_started = threading.Event()
    real_unlink = Path.unlink

    def blocked_unlink(path, *args, **kwargs):
        if path == blob_path:
            unlink_entered.set()
            assert release_unlink.wait(timeout=5)
        return real_unlink(path, *args, **kwargs)

    def add_second_grant():
        second_started.set()
        return store.put_bytes(
            sha256,
            payload,
            private=True,
            capability=second,
        )

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke = pool.submit(store.revoke_owner_capabilities, "run-1")
        assert unlink_entered.wait(timeout=5)
        upload = pool.submit(add_second_grant)
        assert second_started.wait(timeout=5)
        release_unlink.set()
        assert revoke.result(timeout=5) == 1
        upload.result(timeout=5)

    assert not store.can_read(sha256, first)
    assert store.can_read(sha256, second)
    assert store.exists(sha256)
    assert _access_counts(store) == {
        "capabilities": 1,
        "capability_owners": 1,
        "artifact_grants": 1,
        "artifact_access": 1,
    }
    assert store.revoke_owner_capabilities("run-2") == 1
    assert not store.exists(sha256)


def test_concurrent_private_claim_preserves_public_blob_ownership(
    tmp_path,
    monkeypatch,
):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"public wins publication race"
    sha256 = sha256_bytes(payload)
    blob_path = store.blob_path(sha256)
    capability = store.create_capability(owner_id="run-private")
    public_at_replace = threading.Event()
    release_public = threading.Event()
    private_started = threading.Event()
    real_replace = artifact_store_module.os.replace

    def blocked_public_replace(source, target):
        if (
            Path(target) == blob_path
            and threading.current_thread().name.startswith("public-upload")
        ):
            public_at_replace.set()
            assert release_public.wait(timeout=5)
        return real_replace(source, target)

    def private_upload():
        private_started.set()
        return store.put_bytes(
            sha256,
            payload,
            private=True,
            capability=capability,
        )

    monkeypatch.setattr(artifact_store_module.os, "replace", blocked_public_replace)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-upload") as public_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="private-upload") as private_pool,
    ):
        public = public_pool.submit(store.put_bytes, sha256, payload)
        assert public_at_replace.wait(timeout=5)
        private = private_pool.submit(private_upload)
        assert private_started.wait(timeout=5)
        release_public.set()
        public.result(timeout=5)
        private.result(timeout=5)

    assert store.is_private(sha256)
    assert store.revoke_owner_capabilities("run-private") == 1
    assert store.exists(sha256)
    assert not store.is_private(sha256)
    copied = tmp_path / "public-copy"
    store.get_file(sha256, copied)
    assert copied.read_bytes() == payload
