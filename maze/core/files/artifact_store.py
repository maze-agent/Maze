from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict


ARTIFACT_URI_PREFIX = "maze://artifacts/sha256/"
MANAGED_BLOB_PROVENANCE_MIGRATION = "managed_blob_provenance_v1"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        # Windows has no portable directory fsync equivalent.
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def default_artifact_root() -> Path:
    configured = os.environ.get("MAZE_ARTIFACT_STORE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".maze" / "artifacts").resolve()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_uri(sha256: str) -> str:
    return f"{ARTIFACT_URI_PREFIX}{sha256}"


class LocalCASArtifactStore:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).expanduser().resolve() if root else default_artifact_root()
        self.blobs_dir = self.root / "blobs"
        self.access_db_path = self.root / "access.sqlite3"

    def _connect_access_db(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        connection = sqlite3.connect(self.access_db_path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS capabilities (
                capability_hash TEXT PRIMARY KEY,
                created_time REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_access (
                sha256 TEXT PRIMARY KEY,
                private INTEGER NOT NULL CHECK (private IN (0, 1)),
                managed_blob INTEGER NOT NULL DEFAULT 0 CHECK (managed_blob IN (0, 1)),
                updated_time REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_grants (
                sha256 TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                created_time REAL NOT NULL,
                PRIMARY KEY (sha256, capability_hash),
                FOREIGN KEY (sha256) REFERENCES artifact_access(sha256) ON DELETE CASCADE,
                FOREIGN KEY (capability_hash) REFERENCES capabilities(capability_hash) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS capability_owners (
                owner_id TEXT NOT NULL,
                capability_hash TEXT NOT NULL,
                created_time REAL NOT NULL,
                PRIMARY KEY (owner_id, capability_hash),
                FOREIGN KEY (capability_hash) REFERENCES capabilities(capability_hash) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS artifact_store_migrations (
                name TEXT PRIMARY KEY,
                applied_time REAL NOT NULL
            );
            """
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            access_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(artifact_access)"
                ).fetchall()
            }
            migration_applied = connection.execute(
                "SELECT 1 FROM artifact_store_migrations WHERE name = ?",
                (MANAGED_BLOB_PROVENANCE_MIGRATION,),
            ).fetchone()
            if migration_applied is None:
                if "managed_blob" not in access_columns:
                    connection.execute(
                        "ALTER TABLE artifact_access "
                        "ADD COLUMN managed_blob INTEGER NOT NULL DEFAULT 0"
                    )
                # Legacy private rows have no trustworthy provenance. Treat
                # them as managed so revocation can never expose their bytes.
                connection.execute(
                    "UPDATE artifact_access SET managed_blob = 1 "
                    "WHERE private = 1 AND managed_blob = 0"
                )
                connection.execute(
                    "INSERT INTO artifact_store_migrations (name, applied_time) "
                    "VALUES (?, ?)",
                    (MANAGED_BLOB_PROVENANCE_MIGRATION, time.time()),
                )
            elif "managed_blob" not in access_columns:
                raise RuntimeError(
                    "Artifact access schema is missing managed blob provenance"
                )
            # Callers may immediately open another explicit write transaction.
            connection.commit()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        with contextlib.suppress(OSError):
            os.chmod(self.access_db_path, 0o600)
        return connection

    @staticmethod
    def _capability_hash(capability: str) -> str:
        if not isinstance(capability, str) or not capability:
            raise PermissionError("A valid artifact capability is required")
        return hashlib.sha256(capability.encode("utf-8")).hexdigest()

    def create_capability(self, *, owner_id: str | None = None) -> str:
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id):
            raise ValueError("Artifact capability owner_id must be a non-empty string")
        capability = secrets.token_urlsafe(32)
        capability_hash = self._capability_hash(capability)
        with self._connect_access_db() as connection:
            connection.execute(
                "INSERT INTO capabilities (capability_hash, created_time) VALUES (?, ?)",
                (capability_hash, time.time()),
            )
            if owner_id is not None:
                connection.execute(
                    """
                    INSERT INTO capability_owners (owner_id, capability_hash, created_time)
                    VALUES (?, ?, ?)
                    """,
                    (owner_id, capability_hash, time.time()),
                )
        return capability

    def revoke_owner_capabilities(self, owner_id: str) -> int:
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Artifact capability owner_id must be a non-empty string")
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capability_rows = connection.execute(
                "SELECT capability_hash FROM capability_owners WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
            capability_hashes = [row[0] for row in capability_rows]
            artifact_rows = connection.execute(
                """
                SELECT DISTINCT grants.sha256, access.managed_blob
                FROM capability_owners AS owners
                JOIN artifact_grants AS grants
                  ON grants.capability_hash = owners.capability_hash
                JOIN artifact_access AS access
                  ON access.sha256 = grants.sha256
                WHERE owners.owner_id = ?
                """,
                (owner_id,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM capabilities WHERE capability_hash = ?",
                [(capability_hash,) for capability_hash in capability_hashes],
            )
            for sha256, managed_blob in artifact_rows:
                remaining_grant = connection.execute(
                    "SELECT 1 FROM artifact_grants WHERE sha256 = ? LIMIT 1",
                    (sha256,),
                ).fetchone()
                if remaining_grant is not None:
                    continue
                if not managed_blob:
                    # The bytes predated the private reservation. Removing the ACL
                    # restores their original public state without deleting them.
                    connection.execute(
                        "DELETE FROM artifact_access WHERE sha256 = ?",
                        (sha256,),
                    )

        self._reap_revoked_managed_blobs()
        return len(capability_hashes)

    def _reap_revoked_managed_blobs(self) -> None:
        while True:
            with self._connect_access_db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT access.sha256
                    FROM artifact_access AS access
                    WHERE access.private = 1
                      AND access.managed_blob = 1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM artifact_grants AS grants
                          WHERE grants.sha256 = access.sha256
                      )
                    ORDER BY access.sha256
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return

                sha256 = row[0]
                path = self.blob_path(sha256)
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    # The ACL tombstone is removed only after the unlink is
                    # durable. A crash before the DB commit leaves a retryable,
                    # deny-by-default managed orphan.
                    _fsync_directory(path.parent)
                connection.execute(
                    """
                    DELETE FROM artifact_access
                    WHERE sha256 = ?
                      AND private = 1
                      AND managed_blob = 1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM artifact_grants
                          WHERE artifact_grants.sha256 = artifact_access.sha256
                      )
                    """,
                    (sha256,),
                )

    def require_upload_capability(self, capability: str | None) -> str:
        capability_hash = self._capability_hash(capability or "")
        with self._connect_access_db() as connection:
            row = connection.execute(
                "SELECT 1 FROM capabilities WHERE capability_hash = ?",
                (capability_hash,),
            ).fetchone()
        if row is None:
            raise PermissionError("Artifact capability is invalid")
        return capability_hash

    def is_private(self, sha256: str) -> bool:
        self.blob_path(sha256)
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT private FROM artifact_access WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
        return bool(row and row[0])

    def can_read(self, sha256: str, capability: str | None = None) -> bool:
        self.blob_path(sha256)
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            access_row = connection.execute(
                "SELECT private FROM artifact_access WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if access_row is None or not access_row[0]:
                return True
            if not capability:
                return False
            capability_hash = self._capability_hash(capability)
            row = connection.execute(
                """
                SELECT 1
                FROM artifact_grants
                WHERE sha256 = ? AND capability_hash = ?
                """,
                (sha256, capability_hash),
            ).fetchone()
        return row is not None

    def require_read(self, sha256: str, capability: str | None = None):
        if not self.can_read(sha256, capability):
            raise PermissionError("Artifact capability is required")

    def _mark_private(
        self,
        sha256: str,
        capability: str,
    ):
        now = time.time()
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            capability_hash = self._capability_hash(capability)
            capability_row = connection.execute(
                "SELECT 1 FROM capabilities WHERE capability_hash = ?",
                (capability_hash,),
            ).fetchone()
            if capability_row is None:
                raise PermissionError("Artifact capability is invalid")
            # This check shares the same write lock used by revoke and public
            # uploads, so a blob recreated after an orphan unlink remains managed.
            managed_blob = not self.blob_path(sha256).exists()
            connection.execute(
                """
                INSERT INTO artifact_access (
                    sha256, private, managed_blob, updated_time
                )
                VALUES (?, 1, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    private = 1,
                    managed_blob = MAX(
                        artifact_access.managed_blob,
                        excluded.managed_blob
                    ),
                    updated_time = excluded.updated_time
                """,
                (sha256, int(managed_blob), now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_grants (sha256, capability_hash, created_time)
                VALUES (?, ?, ?)
                """,
                (sha256, capability_hash, now),
            )
        return capability_hash

    def _put_private_blob(
        self,
        sha256: str,
        capability_hash: str,
        write_blob,
    ) -> None:
        target = self.blob_path(sha256)
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            grant = connection.execute(
                """
                SELECT 1
                FROM artifact_access AS access
                JOIN artifact_grants AS grants
                  ON grants.sha256 = access.sha256
                JOIN capabilities
                  ON capabilities.capability_hash = grants.capability_hash
                WHERE access.sha256 = ?
                  AND access.private = 1
                  AND grants.capability_hash = ?
                """,
                (sha256, capability_hash),
            ).fetchone()
            if grant is None:
                raise PermissionError("Artifact capability is invalid")
            if not target.exists():
                write_blob(target)

    def _put_public_or_authorized_blob(
        self,
        sha256: str,
        capability: str | None,
        write_blob,
    ) -> None:
        target = self.blob_path(sha256)
        with self._connect_access_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            access_row = connection.execute(
                "SELECT private FROM artifact_access WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            if access_row is not None and access_row[0]:
                if not capability:
                    raise PermissionError("Artifact capability is required")
                capability_hash = self._capability_hash(capability)
                grant = connection.execute(
                    """
                    SELECT 1
                    FROM artifact_grants
                    JOIN capabilities USING (capability_hash)
                    WHERE sha256 = ? AND capability_hash = ?
                    """,
                    (sha256, capability_hash),
                ).fetchone()
                if grant is None:
                    raise PermissionError("Artifact capability is required")
            if not target.exists():
                write_blob(target)

    @staticmethod
    def _write_file_blob(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _write_bytes_blob(data: bytes, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256.lower()):
            raise ValueError(f"Invalid sha256: {sha256}")
        return self.blobs_dir / sha256[:2] / sha256[2:4] / sha256

    def exists(self, sha256: str) -> bool:
        return self.blob_path(sha256).is_file()

    def put_file(
        self,
        source: Path,
        *,
        private: bool = False,
        capability: str | None = None,
    ) -> Dict[str, Any]:
        source = Path(source)
        sha = file_sha256(source)
        if private:
            capability_hash = self._mark_private(
                sha,
                capability or "",
            )
            self._put_private_blob(
                sha,
                capability_hash,
                lambda destination: self._write_file_blob(source, destination),
            )
        else:
            self._put_public_or_authorized_blob(
                sha,
                capability,
                lambda destination: self._write_file_blob(source, destination),
            )
        return {
            "artifact_id": f"sha256:{sha}",
            "sha256": sha,
            "size": source.stat().st_size,
            "storage_uri": artifact_uri(sha),
            "private": self.is_private(sha),
        }

    def put_bytes(
        self,
        sha256: str,
        data: bytes,
        *,
        private: bool = False,
        capability: str | None = None,
    ) -> Dict[str, Any]:
        actual = sha256_bytes(data)
        if actual != sha256:
            raise ValueError(f"Artifact checksum mismatch: expected {sha256}, got {actual}")
        if private:
            capability_hash = self._mark_private(
                sha256,
                capability or "",
            )
            self._put_private_blob(
                sha256,
                capability_hash,
                lambda destination: self._write_bytes_blob(data, destination),
            )
        else:
            self._put_public_or_authorized_blob(
                sha256,
                capability,
                lambda destination: self._write_bytes_blob(data, destination),
            )
        return {
            "artifact_id": f"sha256:{sha256}",
            "sha256": sha256,
            "size": len(data),
            "storage_uri": artifact_uri(sha256),
            "private": self.is_private(sha256),
        }

    def get_file(
        self,
        sha256: str,
        target: Path,
        *,
        capability: str | None = None,
    ) -> Path:
        source = self.blob_path(sha256)
        if not source.exists():
            raise FileNotFoundError(f"Artifact not found: {sha256}")
        self.require_read(sha256, capability)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        actual = file_sha256(target)
        if actual != sha256:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Artifact checksum mismatch after copy: expected {sha256}, got {actual}")
        return target

    def metadata(self, sha256: str, capability: str | None = None) -> Dict[str, Any]:
        path = self.blob_path(sha256)
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {sha256}")
        self.require_read(sha256, capability)
        return {
            "artifact_id": f"sha256:{sha256}",
            "sha256": sha256,
            "size": path.stat().st_size,
            "storage_uri": artifact_uri(sha256),
            "private": self.is_private(sha256),
        }

    def iter_blobs(self):
        if not self.blobs_dir.exists():
            return
        for path in self.blobs_dir.rglob("*"):
            if path.is_file() and len(path.name) == 64:
                yield path

    def cleanup(
        self,
        *,
        referenced_sha256: set[str] | list[str] | None = None,
        older_than_seconds: int | float | None = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        referenced = set(referenced_sha256 or [])
        cutoff = None if older_than_seconds is None else time.time() - float(older_than_seconds)
        candidates = []

        for path in self.iter_blobs() or []:
            sha = path.name
            if sha in referenced:
                continue
            # Private blobs are never collected by an unauthenticated local sweep.
            # Their lifecycle is owned by the Core reference/ACL layer.
            if self.is_private(sha):
                continue
            stat = path.stat()
            if cutoff is not None and stat.st_mtime > cutoff:
                continue
            candidates.append({
                "sha256": sha,
                "size": stat.st_size,
                "path": str(path),
                "storage_uri": artifact_uri(sha),
            })

        deleted_sha256 = []
        if not dry_run:
            for item in candidates:
                blob_path = Path(item["path"])
                blob_path.unlink(missing_ok=True)
                deleted_sha256.append(item["sha256"])

        return {
            "dry_run": dry_run,
            "matched_count": len(candidates),
            "deleted_count": len(deleted_sha256),
            "artifacts": candidates,
            "deleted_sha256": deleted_sha256,
        }
