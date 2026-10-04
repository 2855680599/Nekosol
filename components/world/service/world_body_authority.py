#!/usr/bin/env python3
"""Single-writer authority lease for the standalone World/Body service (M13A).

Ticket sections 13 / 14:

* a second writer must **fail closed**, never "win the race";
* the authority *marker* on disk is evidence, not truth.

So the arbiter is a kernel ``flock`` on ``<world_home>/run/world_body_authority.lock``.
The JSON marker next to it is written for humans and for diagnostics, and every
report from this module says so explicitly.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_AUTHORITY = "world.body.service.authority.v1"
KIND_AUTHORITY = "world_body_authority"

LOCK_NAME = "world_body_authority.lock"
MARKER_NAME = "authority.json"

#: The marker is never the source of truth.  This string travels with it.
MARKER_DISCLAIMER = "marker is evidence only; authority is decided by the flock"


class AuthorityError(RuntimeError):
    """Authority could not be established."""


class AuthorityConflict(AuthorityError):
    """Another live instance already owns this home."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    return Path("/proc/%d" % pid).exists()


def read_marker(run_dir: Path | str) -> Optional[dict[str, Any]]:
    """Read the advisory marker.  Returns None when absent or unreadable."""

    path = Path(run_dir) / MARKER_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lock_is_held(run_dir: Path | str) -> bool:
    """Probe the lease without owning it.

    A separate file description is opened, so a lock held by *this* process is
    reported as held too -- which is what a status probe should say.
    """

    path = Path(run_dir) / LOCK_NAME
    if not path.exists():
        return False
    try:
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


class AuthorityLease:
    """Exclusive, kernel-enforced, single-writer lease over one World/Body home."""

    def __init__(
        self,
        run_dir: Path | str,
        *,
        service_name: str = "chiyo-world-body.service",
        instance_id: Optional[str] = None,
        world_home: Optional[Path | str] = None,
        pid: Optional[int] = None,
        now: Optional[datetime] = None,
    ):
        self.run_dir = Path(run_dir)
        self.service_name = service_name
        self.world_home = Path(world_home) if world_home is not None else None
        self.pid = int(pid if pid is not None else os.getpid())
        self.instance_id = instance_id or "%s:%d" % (service_name, self.pid)
        self.now = now
        self.lock_path = self.run_dir / LOCK_NAME
        self.marker_path = self.run_dir / MARKER_NAME
        self._handle: Optional[Any] = None
        self.reclaimed_from: Optional[dict[str, Any]] = None

    # ---- state -----------------------------------------------------------
    @property
    def held(self) -> bool:
        return self._handle is not None

    def _moment(self) -> datetime:
        return self.now if self.now is not None else datetime.now(timezone.utc)

    # ---- acquire / release ----------------------------------------------
    def acquire(self) -> dict[str, Any]:
        """Take the lease or raise :class:`AuthorityConflict`."""

        if self.held:
            return self.status()

        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise AuthorityError("authority lock failed: %s" % error) from error
            other = read_marker(self.run_dir)
            raise AuthorityConflict(
                "another live instance owns %s (holder=%s)"
                % (self, (other or {}).get("instance_id", "unknown"))
            ) from error

        previous = read_marker(self.run_dir)
        if previous is not None and previous.get("instance_id") != self.instance_id:
            # We hold the lock, so no live process owns it.  The marker is stale
            # leftovers from a crash -- record it rather than erase it silently.
            self.reclaimed_from = previous

        self._handle = handle
        self._write_marker()
        return self.status()

    def _write_marker(self) -> dict[str, Any]:
        marker = {
            "kind": KIND_AUTHORITY,
            "schema_version": SCHEMA_AUTHORITY,
            "service_name": self.service_name,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "world_home": str(self.world_home) if self.world_home else None,
            "acquired_at": _iso(self._moment()),
            "lock_path": str(self.lock_path),
            "marker_is_truth": False,
            "disclaimer": MARKER_DISCLAIMER,
            "reclaimed_from": (self.reclaimed_from or {}).get("instance_id"),
        }
        temporary = self.marker_path.with_name(self.marker_path.name + ".tmp")
        temporary.write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.marker_path)
        return marker

    def release(self) -> dict[str, Any]:
        """Drop the lease.  Safe to call when not held."""

        if self._handle is None:
            return {"released": False, "reason": "not_held"}
        marker = read_marker(self.run_dir)
        try:
            if marker is not None and marker.get("instance_id") == self.instance_id:
                self.marker_path.unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None
        return {"released": True, "instance_id": self.instance_id}

    @contextmanager
    def held_lease(self) -> Iterator["AuthorityLease"]:
        self.acquire()
        try:
            yield self
        finally:
            self.release()

    # ---- reporting -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        marker = read_marker(self.run_dir)
        return {
            "kind": KIND_AUTHORITY,
            "schema_version": SCHEMA_AUTHORITY,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "held_by_this_instance": self.held,
            "lock_held_by_someone": lock_is_held(self.run_dir) if self.run_dir.exists() else False,
            "marker": marker,
            "marker_is_truth": False,
            "marker_disclaimer": MARKER_DISCLAIMER,
            "marker_pid_alive": _pid_alive((marker or {}).get("pid")),
            "reclaimed_from": self.reclaimed_from,
            "lock_path": str(self.lock_path),
        }
