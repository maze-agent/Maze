from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Dict


ARTIFACT_URI_PREFIX = "maze://artifacts/sha256/"


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

    def blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256.lower()):
            raise ValueError(f"Invalid sha256: {sha256}")
        return self.blobs_dir / sha256[:2] / sha256[2:4] / sha256

    def exists(self, sha256: str) -> bool:
        return self.blob_path(sha256).is_file()

    def put_file(self, source: Path) -> Dict[str, Any]:
        source = Path(source)
        sha = file_sha256(source)
        target = self.blob_path(sha)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
        return {
            "artifact_id": f"sha256:{sha}",
            "sha256": sha,
            "size": source.stat().st_size,
            "storage_uri": artifact_uri(sha),
        }

    def put_bytes(self, sha256: str, data: bytes) -> Dict[str, Any]:
        actual = sha256_bytes(data)
        if actual != sha256:
            raise ValueError(f"Artifact checksum mismatch: expected {sha256}, got {actual}")
        target = self.blob_path(sha256)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return {
            "artifact_id": f"sha256:{sha256}",
            "sha256": sha256,
            "size": len(data),
            "storage_uri": artifact_uri(sha256),
        }

    def get_file(self, sha256: str, target: Path) -> Path:
        source = self.blob_path(sha256)
        if not source.exists():
            raise FileNotFoundError(f"Artifact not found: {sha256}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        actual = file_sha256(target)
        if actual != sha256:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Artifact checksum mismatch after copy: expected {sha256}, got {actual}")
        return target

    def metadata(self, sha256: str) -> Dict[str, Any]:
        path = self.blob_path(sha256)
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {sha256}")
        return {
            "artifact_id": f"sha256:{sha256}",
            "sha256": sha256,
            "size": path.stat().st_size,
            "storage_uri": artifact_uri(sha256),
        }
