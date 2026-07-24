"""Generation-aware PID lock that cannot remove another Controller's lock."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import IO


@dataclass(frozen=True, slots=True)
class ProcessLockIdentity:
    pid: int
    generation: str
    process_start_ticks: int

    @property
    def controller_generation(self) -> str:
        return self.generation


class _GenerationProcessLock:
    def __init__(
        self,
        path: Path,
        *,
        generation: str,
        generation_key: str,
        owner_name: str,
    ) -> None:
        if not path.is_absolute():
            raise ValueError("PID lock path must be absolute")
        if not generation:
            raise ValueError(f"{generation_key} is required")
        self.path = path
        self.generation_key = generation_key
        self.owner_name = owner_name
        self.identity = ProcessLockIdentity(
            pid=os.getpid(),
            generation=generation,
            process_start_ticks=_process_start_ticks(os.getpid()),
        )
        self._file: IO[str] | None = None
        self._inode: int | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        file = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            file.close()
            raise RuntimeError(
                f"another {self.owner_name} owns PID lock: {self.path}"
            ) from exc
        file.seek(0)
        file.truncate()
        json.dump(
            {
                "schema_version": 1,
                "pid": self.identity.pid,
                self.generation_key: self.identity.generation,
                "process_start_ticks": self.identity.process_start_ticks,
            },
            file,
            sort_keys=True,
            separators=(",", ":"),
        )
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
        self._file = file
        self._inode = os.fstat(file.fileno()).st_ino

    def close(self) -> None:
        file = self._file
        if file is None:
            return
        self._file = None
        try:
            info = self.path.stat()
        except FileNotFoundError:
            info = None
        if info is not None and info.st_ino == self._inode and stat.S_ISREG(info.st_mode):
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get(self.generation_key) == self.identity.generation
            ):
                self.path.unlink(missing_ok=True)
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()

    def __enter__(self) -> "_GenerationProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class ControllerProcessLock(_GenerationProcessLock):
    def __init__(self, path: Path, *, controller_generation: str) -> None:
        super().__init__(
            path,
            generation=controller_generation,
            generation_key="controller_generation",
            owner_name="Controller",
        )


class NodeProcessLock(_GenerationProcessLock):
    def __init__(self, path: Path, *, node_generation: str) -> None:
        super().__init__(
            path,
            generation=node_generation,
            generation_key="node_generation",
            owner_name="Node",
        )


def _process_start_ticks(pid: int) -> int:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError(f"cannot identify process generation for PID {pid}") from exc
