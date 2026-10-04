"""Narrow durable-IO seam for the CT0 sandbox durable layer.

Only *IO failure points* are injectable.  The durable algorithm itself is never
replaced by a fake store: every durable decision (what to append, what to
verify, what to fail closed on) still runs in the real implementation.

`RealDurableFileOps` is the default and the only production path; it performs
real Python/OS IO.  Test code may substitute an implementation that raises at a
chosen step (`write`/`flush`/`fsync`/`replace`) so that storage faults can be
qualified without ever claiming a real device or power-loss event.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DurableFileOps",
    "DurableIOError",
    "RealDurableFileOps",
    "atomic_write_bytes",
    "IO_FAULT_KINDS",
]

IO_FAULT_KINDS = (
    "OPEN_FAILURE",
    "SHORT_WRITE",
    "ENOSPC",
    "FLUSH_FAILURE",
    "FSYNC_FAILURE",
    "REPLACE_FAILURE",
    "CLOSE_FAILURE",
)


class DurableIOError(OSError):
    """A durable-IO step failed; `kind` names the exact step that failed."""

    def __init__(self, message: str, *, kind: str, partial: bool = False) -> None:
        if kind not in IO_FAULT_KINDS:
            raise ValueError("unsupported durable IO fault kind")
        super().__init__(message)
        self.kind = kind
        self.partial = bool(partial)


@runtime_checkable
class DurableFileOps(Protocol):
    """The only IO surface the durable layer is allowed to depend on."""

    def open_for_write(self, path: Path) -> Any:  # pragma: no cover - protocol
        ...

    def write(self, handle: Any, data: bytes) -> None:  # pragma: no cover - protocol
        ...

    def flush(self, handle: Any) -> None:  # pragma: no cover - protocol
        ...

    def fsync(self, handle: Any) -> None:  # pragma: no cover - protocol
        ...

    def replace(self, source: Path, destination: Path) -> None:  # pragma: no cover - protocol
        ...

    def close(self, handle: Any) -> None:  # pragma: no cover - protocol
        ...


class RealDurableFileOps:
    """Real OS-backed IO: write -> flush -> fsync -> atomic replace."""

    def open_for_write(self, path: Path) -> Any:
        return open(path, "wb")

    def write(self, handle: Any, data: bytes) -> None:
        written = handle.write(data)
        if written is not None and written != len(data):
            raise DurableIOError("short write", kind="SHORT_WRITE", partial=True)

    def flush(self, handle: Any) -> None:
        handle.flush()

    def fsync(self, handle: Any) -> None:
        os.fsync(handle.fileno())

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(str(source), str(destination))

    def close(self, handle: Any) -> None:
        handle.close()


def atomic_write_bytes(path: Path | str, data: bytes, *,
                       ops: DurableFileOps | None = None) -> dict[str, Any]:
    """Atomically publish `data` at `path` using the durable IO seam.

    Ordering contract: write temp -> flush -> fsync -> atomic replace.  A failure
    at any step leaves the destination untouched and removes the temp file, so a
    caller's previously committed state stays valid.  A failure is reported as
    `DurableIOError` with the exact failing step in `kind`.
    """
    ops = ops or RealDurableFileOps()
    target = Path(path)
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}-{id(data):x}")
    handle = None
    try:
        handle = ops.open_for_write(temporary)
    except OSError as exc:
        if isinstance(exc, DurableIOError):
            raise
        raise DurableIOError(f"cannot open temp file: {exc}", kind="OPEN_FAILURE") from exc
    try:
        try:
            ops.write(handle, data)
        except DurableIOError:
            raise
        except OSError as exc:
            kind = "ENOSPC" if getattr(exc, "errno", None) == 28 else "SHORT_WRITE"
            raise DurableIOError(f"write failed: {exc}", kind=kind, partial=True) from exc
        try:
            ops.flush(handle)
        except OSError as exc:
            raise DurableIOError(f"flush failed: {exc}", kind="FLUSH_FAILURE",
                                 partial=True) from exc
        try:
            ops.fsync(handle)
        except OSError as exc:
            raise DurableIOError(f"fsync failed: {exc}", kind="FSYNC_FAILURE",
                                 partial=True) from exc
    except BaseException:
        try:
            ops.close(handle)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real failure
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        ops.close(handle)
    except OSError as exc:
        raise DurableIOError(f"close failed: {exc}", kind="CLOSE_FAILURE",
                             partial=True) from exc
    try:
        ops.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DurableIOError(f"replace failed: {exc}", kind="REPLACE_FAILURE") from exc
    return {"published": True, "temp_removed": not temporary.exists(), "bytes": len(data)}
