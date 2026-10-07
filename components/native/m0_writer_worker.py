"""Persistent, privilege-downgraded M0 writer worker (NYA-AUDIT-005).

Why
---
The M37 bridge appended to M0 by starting a fresh Python interpreter per call:
one for the user's inbound turn and one for the assistant's delivered reply, so
every turn created at least two interpreters. The expensive part is not the
write, it is the interpreter; the security-relevant part is that the process
performing the write runs as the M0 store owner. This module keeps the second
property and removes the first, by making that process long-lived.

Privilege model (unchanged in mechanism)
----------------------------------------
The worker does **not** call setuid itself. The parent starts it with
``subprocess`` ``user=<owner>``/``group=<gid>``/``extra_groups=[]``, exactly as
the one-shot child was started, so the worker is exec'd as the store owner. After
binding its socket the worker *verifies* ``os.getuid()`` against the identity the
parent declared; if they differ it refuses to serve and exits non-zero. The
failure mode "privilege drop failed but the worker keeps running with the
parent's rights" is therefore unreachable rather than merely untested.

The parent never gains a way to write M0 directly: the only write path is one
``EvidenceStore.append`` call behind a three-operation protocol.

Protocol
--------
One ``AF_UNIX``/``SOCK_STREAM`` socket per (M0 path, writer uid, parent pid),
created in a runtime directory next to the store, mode ``0600``. Frames are a
4-byte big-endian length followed by UTF-8 JSON. Requests carry ``version``,
``op`` and ``request_id``; responses carry ``request_id``, ``status`` and either
``result`` or ``error``. The append path never parses stdout.

Socket location
---------------
``AF_UNIX`` addresses are limited by ``sun_path`` (108 bytes on Linux, including
its NUL), so a deep enough store directory cannot hold a bindable socket: the
worker child died in ``bind()`` and every append degraded into the retry spool.
The rule is now "the same file name, in the first directory that fits": the
readable directory next to the store while it fits, otherwise a short private
per-uid directory (see ``socket_path_candidates``). The name never changes, so
the parent and the worker always agree, and no path is ever truncated. An
explicit ``NYAIRO_M0_WRITER_SOCKET_DIR`` remains the operator's override (the
legacy ``CHIYO_M0_WRITER_SOCKET_DIR`` is still honoured) and is the only
candidate in that case.

Operations are a closed set - ``ping``, ``append_events``, ``shutdown`` - and the
event fields are a closed whitelist. There is no arbitrary SQL, no shell, no
subprocess, no caller-supplied path and no arbitrary module call: the worker
imports the M0 append API once at startup and can do nothing else.

Lifecycle
---------
One worker per (M0 path, writer uid) per process, held in ``_POOL``; the parent
that starts it owns stopping it. ``m37_m0_bridge.ConfiguredBridge.close()`` -
reached through ``Instance.close()`` - calls ``close_for()``, which asks the
worker to stop through the protocol, lets it exit on its own, reaps it, closes
the client socket and the child's pipes, and removes the socket file. Nothing
else stops it, so an owner that only ever appends would leave the child running
with its socket and pipes open; that is the leak ``close_for``/``close_all``
exist to prevent.

Idempotency
-----------
The worker calls the normal append API, so M0's existing unique index on
``(source_origin, primary_source_ref_id)`` still decides duplicates. If the
worker commits and then dies before responding, the caller's retry is answered
with ``duplicate`` and no second row is created.

Windows
-------
Native Windows has no Unix uid/gid semantics, so the downgraded persistent worker
is **unsupported** there (``available()`` returns False and the caller keeps using
the existing one-shot path). The uid boundary was not relaxed to make Windows
look supported.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger("chiyo.m0_writer_worker")

PROTOCOL_VERSION = 1
MODE_ENV = "CHIYO_M0_WRITER_MODE"
SOCKET_DIR_ENV = "CHIYO_M0_WRITER_SOCKET_DIR"
#: Public spelling of the socket-directory override. ``NYAIRO_*`` is the name users
#: are told to set and wins when both are present; the legacy ``CHIYO_*`` name keeps
#: working. This module reads the process environment itself (it is also importable
#: outside ``chiyo_bundle``), so it resolves the pair here instead of relying on a
#: caller having normalised anything.
PUBLIC_SOCKET_DIR_ENV = "NYAIRO_M0_WRITER_SOCKET_DIR"
#: test-only fault injection, default OFF (same pattern as the resolver's FAULT_ENV)
FAULT_ENV = "CHIYO_M0_WRITER_FAULT"

STARTUP_TIMEOUT_S = 15.0
REQUEST_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 5.0
#: how long a worker gets to exit on its own after the shutdown request, before
#: it is reaped; a healthy worker returns from serve() in milliseconds.
SHUTDOWN_GRACE_S = 2.0
MAX_FRAME_BYTES = 1 << 20
MAX_EVENTS_PER_REQUEST = 64

OPS = ("ping", "append_events", "shutdown")
EVENT_FIELDS = (
    "occurred_at", "created_at", "source_origin", "delivery_status",
    "epistemic_role", "speaker", "content", "conversation_id", "turn_id",
    "source_refs",
)

READY = "READY"
DEGRADED = "DEGRADED"
ERROR = "ERROR"
UNSUPPORTED = "UNSUPPORTED"


class WorkerError(RuntimeError):
    """The worker could not be used. Never means "the write succeeded"."""


def available() -> bool:
    """Whether the downgraded persistent worker exists on this platform.

    Requires POSIX uid/gid semantics. On native Windows the answer is a flat no;
    the caller keeps the one-shot path instead of pretending otherwise.
    """
    if not hasattr(os, "getuid") or not hasattr(socket, "AF_UNIX"):
        return False
    return os.environ.get(MODE_ENV, "persistent").strip().lower() != "off"


#: ``struct sockaddr_un.sun_path`` is 108 bytes on Linux and 104 on macOS/BSD, and
#: the name has to be NUL-terminated inside that buffer: a path of 108 bytes is
#: already un-bindable on Linux (measured: 107 binds, 108 fails with ENAMETOOLONG).
#: One conservative limit is used so a socket that works here is not silently
#: unusable on another POSIX host; vendor/hermes/gateway/control_socket.py makes
#: the same choice for its own socket.
SUN_PATH_SAFE_LIMIT = 100

#: Short, private, per-uid socket directory, used when the store's own directory
#: is too deep for sun_path.
PRIVATE_SOCKET_DIR = "nyairo-m0-writer-%d"


def _uid() -> int:
    getter = getattr(os, "getuid", None)
    return int(getter()) if callable(getter) else 0


def _fits_sun_path(path: Path) -> bool:
    """Whether ``path`` can be bound as an AF_UNIX address, with a safety margin."""
    return len(os.fsencode(str(path))) <= SUN_PATH_SAFE_LIMIT


def _ensure_private_dir(path: Path) -> Path:
    """Create one socket directory, private to the writer, and return it."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def socket_dir_override() -> str | None:
    """The configured socket directory, public name first, else the legacy one.

    ``NYAIRO_M0_WRITER_SOCKET_DIR`` (the documented spelling) wins when it is set
    to a non-empty value; ``CHIYO_M0_WRITER_SOCKET_DIR`` still works unchanged.
    """
    env = os.environ
    public = str(env.get(PUBLIC_SOCKET_DIR_ENV) or "").strip()
    if public:
        return public
    legacy = env.get(SOCKET_DIR_ENV)
    return legacy if legacy else None


def _native_socket_dir(m0_db: Path) -> Path:
    """The preferred, readable directory: an explicit override, else the store's own."""
    override = socket_dir_override()
    if override:
        return Path(override)
    return Path(m0_db).resolve().parent / "m0-writer-runtime"


def _fallback_socket_dirs() -> list[Path]:
    """Short private directories for stores whose own directory does not fit.

    ``XDG_RUNTIME_DIR`` first (the per-user runtime location with the right
    permissions by construction), then the process temp directory, then ``/tmp``
    as a last resort. None of them depends on the store's depth, so all of them
    are short enough for sun_path in practice; the caller still checks.
    """
    name = PRIVATE_SOCKET_DIR % _uid()
    directories: list[Path] = []
    for root in (os.environ.get("XDG_RUNTIME_DIR"), tempfile.gettempdir(), "/tmp"):
        if not root:
            continue
        candidate = Path(root) / name
        if candidate not in directories:
            directories.append(candidate)
    return directories


def socket_name_for(m0_db: Path, *, pid: int | None = None) -> str:
    """The socket file name for one (store, writer uid, parent process).

    SHA-256 of the resolved store path and the writer uid: deterministic, never
    Python's randomised ``hash()``, and different stores cannot collide even when
    they share one fallback directory. The name is the same in every candidate
    directory, so only the location ever changes.
    """
    tag = hashlib.sha256(
        ("%s|%s" % (Path(m0_db).resolve(), _uid())).encode("utf-8")
    ).hexdigest()[:16]
    return "m0-writer-%s-%d.sock" % (tag, os.getpid() if pid is None else pid)


def socket_path_candidates(m0_db: Path, *, pid: int | None = None) -> list[Path]:
    """Every path the socket may occupy, in priority order; no side effects.

    An explicit ``CHIYO_M0_WRITER_SOCKET_DIR`` is authoritative and is therefore
    the *only* candidate: a configuration that cannot bind then fails loudly
    instead of being moved somewhere the operator did not ask for. Without an
    override the readable directory next to the store wins whenever it fits, and
    the short private directories follow.
    """
    name = socket_name_for(m0_db, pid=pid)
    override = socket_dir_override()
    directories = ([Path(override)] if override
                   else [_native_socket_dir(Path(m0_db))] + _fallback_socket_dirs())
    return [directory / name for directory in directories]


def socket_path_for(m0_db: Path, *, pid: int | None = None) -> Path:
    """The deterministic socket path for a store, creating its private directory.

    Inside sun_path this is the readable runtime directory next to the store,
    unchanged; beyond it, the same file name in a short private directory, so a
    deep state keeps working with nothing configured.
    """
    candidates = socket_path_candidates(m0_db, pid=pid)
    chosen = next((path for path in candidates if _fits_sun_path(path)), None)
    if chosen is None:
        configured = (PUBLIC_SOCKET_DIR_ENV if str(os.environ.get(PUBLIC_SOCKET_DIR_ENV) or "").strip()
                      else SOCKET_DIR_ENV if socket_dir_override() else "")
        raise WorkerError(
            "no AF_UNIX socket path fits %d bytes for %s: %s"
            % (SUN_PATH_SAFE_LIMIT, Path(m0_db).resolve(),
               ", ".join(str(path) for path in candidates))
            + ("; shorten %s" % configured if configured else ""))
    _ensure_private_dir(chosen.parent)
    return chosen


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #
def send_frame(stream: socket.socket, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise WorkerError("frame exceeds limit")
    stream.sendall(struct.pack(">I", len(body)) + body)


def _recv_exactly(stream: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise WorkerError("connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(stream: socket.socket) -> dict:
    header = _recv_exactly(stream, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise WorkerError("frame exceeds limit")
    payload = json.loads(_recv_exactly(stream, length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise WorkerError("frame is not a JSON object")
    return payload


def validate_request(payload: dict) -> tuple[str, str]:
    """Whitelist a request. Returns (op, request_id). Raises on anything else."""
    if payload.get("version") != PROTOCOL_VERSION:
        raise WorkerError("unsupported protocol version")
    op = payload.get("op")
    if op not in OPS:
        raise WorkerError("unsupported op")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise WorkerError("bad request id")
    return op, request_id


def validate_events(raw) -> list[dict]:
    """Whitelist event payloads, reusing the existing M0 field set.

    Unknown keys are rejected rather than ignored, so a caller cannot smuggle a
    path, SQL fragment or extra field through the bridge.
    """
    if not isinstance(raw, list) or not raw:
        raise WorkerError("events must be a non-empty list")
    if len(raw) > MAX_EVENTS_PER_REQUEST:
        raise WorkerError("too many events in one request")
    events = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkerError("event must be an object")
        unknown = set(item) - set(EVENT_FIELDS)
        if unknown:
            raise WorkerError("unknown event fields")
        missing = set(EVENT_FIELDS) - set(item)
        if missing:
            raise WorkerError("missing event fields")
        refs = item["source_refs"]
        if (not isinstance(refs, list) or not refs
                or not isinstance(refs[0], dict) or not refs[0].get("id")):
            raise WorkerError("event needs a primary source ref")
        events.append({name: item[name] for name in EVENT_FIELDS})
    return events


# --------------------------------------------------------------------------- #
# worker side
# --------------------------------------------------------------------------- #
def _open_store(m0_db: Path):
    native_root = Path(__file__).resolve().parent / "src"
    if str(native_root) not in sys.path:
        sys.path.insert(0, str(native_root))
    from app.evidence import EvidenceEvent, EvidenceStore, new_event_id  # noqa: E402
    return EvidenceEvent, EvidenceStore(m0_db), new_event_id


def _append(store, EvidenceEvent, new_event_id, raw: dict) -> dict:
    event = EvidenceEvent(
        event_id=new_event_id(),
        occurred_at=raw["occurred_at"],
        memory_owner="chiyo",
        source_origin=raw["source_origin"],
        delivery_status=raw["delivery_status"],
        epistemic_role=raw["epistemic_role"],
        speaker=raw["speaker"],
        content=raw["content"],
        conversation_id=raw["conversation_id"],
        turn_id=raw["turn_id"],
        source_refs=raw["source_refs"],
        created_at=raw["created_at"],
    )
    try:
        result = store.append(event)
        return {"status": result.status, "event_id": result.event_id,
                "ref": raw["source_refs"][0]["id"]}
    except Exception as exc:  # conflict / lock / schema
        return {"status": "error", "error_class": type(exc).__name__,
                "detail": "append rejected", "ref": raw["source_refs"][0]["id"]}


def serve(m0_db: Path, socket_path: Path, expected_uid: int,
          fault: str = "") -> int:
    """Run the worker loop. Returns a process exit code."""
    if os.getuid() != expected_uid:
        # Refuse to serve rather than work with the parent's rights.
        print(json.dumps({"ready": False, "error": "uid_mismatch",
                          "uid": os.getuid(), "expected": expected_uid}), flush=True)
        return 2
    EvidenceEvent, store, new_event_id = _open_store(m0_db)

    if socket_path.exists():
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(8)
    server.settimeout(0.5)
    print(json.dumps({"ready": True, "uid": os.getuid(), "pid": os.getpid()}), flush=True)
    if fault == "before_request":
        # the parent sees a ready worker that dies before it can serve
        return 0
    stopping = False
    try:
        while not stopping:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if exc.errno in (errno.EINTR,):
                    continue
                raise
            with connection:
                connection.settimeout(REQUEST_TIMEOUT_S)
                # Keep the session open: one parent connection carries many
                # requests, so requests must be read in a loop rather than one
                # accept per request.
                while not stopping:
                    try:
                        payload = recv_frame(connection)
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                    # Read the correlation id before validating anything else, so
                    # a rejected request still comes back with its own id and the
                    # caller can match the reply to the request it sent.
                    request_id = payload.get("request_id")
                    if not isinstance(request_id, str) or not request_id \
                            or len(request_id) > 128:
                        request_id = None
                    try:
                        op, _ = validate_request(payload)
                        if op == "ping":
                            reply = {"status": "ok", "result": {"uid": os.getuid(),
                                                                "pid": os.getpid()}}
                        elif op == "shutdown":
                            reply = {"status": "ok", "result": {}}
                            stopping = True
                        else:
                            events = validate_events(payload.get("events"))
                            if fault == "during_request":
                                return 0
                            results = [_append(store, EvidenceEvent, new_event_id, raw)
                                       for raw in events]
                            if fault == "after_commit":
                                # committed, then dies before the reply is sent
                                return 0
                            reply = {"status": "ok", "result": {"results": results}}
                    except WorkerError as exc:
                        reply = {"status": "rejected", "error": str(exc)}
                    except Exception as exc:
                        reply = {"status": "error", "error": type(exc).__name__}
                    reply["request_id"] = request_id
                    try:
                        send_frame(connection, reply)
                    except Exception:
                        break
    finally:
        server.close()
        try:
            socket_path.unlink()
        except OSError:
            pass
    return 0


# --------------------------------------------------------------------------- #
# parent side
# --------------------------------------------------------------------------- #
class M0WriterWorker:
    """Parent-side handle to one long-lived, downgraded writer process."""

    def __init__(self, m0_db: Path, *, writer_user=None, fault: str | None = None):
        self.m0_db = Path(m0_db).resolve()
        self.writer_user = writer_user
        self.fault = (os.environ.get(FAULT_ENV, "") if fault is None else fault).strip().lower()
        self.socket_path = socket_path_for(self.m0_db)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._connection: socket.socket | None = None
        self._counter = 0
        self.starts = 0
        self.restarts = 0
        self.last_error: str | None = None
        self.state = DEGRADED

    # ------------------------------------------------------------ identity --- #
    def _identity(self) -> tuple[int, dict]:
        """Resolve the store owner and the privilege-drop kwargs, as before."""
        import pwd
        target = self.writer_user
        if target is None:
            target = os.getuid()
        target = (int(target) if isinstance(target, int) or str(target).isdigit()
                  else pwd.getpwnam(str(target)).pw_uid)
        if target == os.getuid():
            # already the owner: no handoff, exactly like the one-shot child
            return target, {}
        if os.geteuid() != 0:
            raise WorkerError("only root can hand off to a different M0 writer")
        group = os.environ.get("CHIYO_M0_WRITER_GROUP") or pwd.getpwuid(target).pw_gid
        return target, dict(user=target, group=group, extra_groups=[])

    # -------------------------------------------------------------- startup --- #
    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            target, identity = self._identity()
            if self.socket_path.exists():
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass
            command = [sys.executable, "-S", str(Path(__file__).resolve()),
                       "--serve", str(self.m0_db), str(self.socket_path), str(target)]
            if self.fault:
                command.append(self.fault)
            environment = dict(os.environ)
            environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
            try:
                self._proc = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, env=environment,
                    **identity)
            except Exception as exc:
                self._fail("spawn_failed:" + type(exc).__name__)
                raise WorkerError("could not start M0 writer worker") from exc
            self.starts += 1
            deadline = time.monotonic() + STARTUP_TIMEOUT_S
            while True:
                if self._proc.poll() is not None:
                    self._fail("worker_exited_during_startup")
                    raise WorkerError("M0 writer worker exited during startup")
                line = self._proc.stdout.readline() if self._proc.stdout else ""
                if line.strip():
                    try:
                        handshake = json.loads(line)
                    except json.JSONDecodeError:
                        self._fail("bad_handshake")
                        raise WorkerError("M0 writer worker handshake unreadable")
                    if not handshake.get("ready"):
                        self._fail("worker_refused:" + str(handshake.get("error")))
                        raise WorkerError("M0 writer worker refused to serve")
                    break
                if time.monotonic() > deadline:
                    self._terminate()
                    self._fail("startup_timeout")
                    raise WorkerError("M0 writer worker startup timed out")
            try:
                self._connect()
            except Exception:
                self._terminate()
                # keep a more specific reason if _connect already recorded one
                if self.last_error is None:
                    self._fail("connect_failed")
                raise
            self.state = READY
            self.last_error = None

    def _connect(self) -> None:
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                # the worker died between the handshake and the connection
                self._fail("worker_exited_during_startup")
                raise WorkerError("M0 writer worker exited during startup")
            try:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(REQUEST_TIMEOUT_S)
                connection.connect(str(self.socket_path))
                self._connection = connection
                return
            except Exception as exc:
                last = exc
                # close the failed attempt: otherwise every retry leaks a socket
                try:
                    connection.close()
                except Exception:
                    pass
                time.sleep(0.02)
        raise WorkerError("could not connect to M0 writer worker: %s"
                          % (type(last).__name__ if last else "timeout"))

    def _terminate(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
            # close the pipes too: otherwise the handle leaks two file objects
            for stream in (self._proc.stdout, self._proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        self._proc = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass

    def _fail(self, reason: str) -> None:
        self.last_error = reason
        self.state = ERROR
        LOGGER.error("m0.writer.degraded reason=%s", reason)

    # ------------------------------------------------------------- requests --- #
    def _request(self, op: str, **extra) -> dict:
        with self._lock:
            if self._connection is None:
                raise WorkerError("M0 writer worker is not connected")
            self._counter += 1
            request_id = "%d-%d" % (os.getpid(), self._counter)
            payload = {"version": PROTOCOL_VERSION, "op": op,
                       "request_id": request_id}
            payload.update(extra)
            try:
                send_frame(self._connection, payload)
                reply = recv_frame(self._connection)
            except Exception as exc:
                # a dead or wedged worker must never look like a success
                self._terminate()
                self._fail("request_failed:" + type(exc).__name__)
                raise WorkerError("M0 writer request failed") from exc
            if reply.get("request_id") != request_id:
                self._terminate()
                self._fail("response_mismatch")
                raise WorkerError("M0 writer response does not match the request")
            if reply.get("status") == "rejected":
                raise WorkerError("M0 writer rejected the request: %s" % reply.get("error"))
            if reply.get("status") != "ok":
                self._terminate()
                self._fail("worker_error:" + str(reply.get("error")))
                raise WorkerError("M0 writer reported an error")
            return reply.get("result") or {}

    def ping(self) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        return self._request("ping")

    def append_events(self, events: list[dict]) -> list[dict]:
        """One attempt. Callers retry after a restart via append_events_retrying."""
        result = self._request("append_events", events=events)
        return result.get("results") or []

    def append_events_retrying(self, events: list[dict]) -> list[dict]:
        """Restart once if the worker died, then retry.

        Safe because the append itself is idempotent on
        (source_origin, primary_source_ref_id): a retry after a lost response
        returns ``duplicate`` instead of creating a second row.
        """
        try:
            return self.append_events(events)
        except WorkerError:
            self.restarts += 1
            self.start()
            return self.append_events(events)

    def close(self) -> None:
        """Idempotent shutdown; safe to call repeatedly.

        The worker is first asked to stop through the protocol, exactly as
        before, and is then given a bounded grace period to leave on its own. It
        needs one: the shutdown reply is sent before ``serve()`` returns, so
        without this wait the parent killed the child microseconds after asking
        it to stop, the child never reached its own cleanup, and every close
        ended in a signal. ``_terminate()`` remains the fallback for a worker
        that is wedged and does not exit within the grace period.
        """
        with self._lock:
            if self._connection is not None and self._proc is not None \
                    and self._proc.poll() is None:
                try:
                    self._request("shutdown")
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=SHUTDOWN_GRACE_S)
                except Exception:
                    pass
            self._terminate()

    # --------------------------------------------------------------- status --- #
    def status(self) -> dict:
        if not available():
            return {"state": UNSUPPORTED, "pid": None, "starts": 0,
                    "restarts": 0, "error": "platform_without_uid_semantics"}
        alive = self._proc is not None and self._proc.poll() is None
        state = self.state if alive else (ERROR if self.last_error else DEGRADED)
        return {"state": state, "pid": self._proc.pid if alive else None,
                "starts": self.starts, "restarts": self.restarts,
                "error": self.last_error}


_POOL: dict[str, M0WriterWorker] = {}
_POOL_LOCK = threading.Lock()

# Bridge-level writer health. The worker module owns the *concept* "how healthy is
# writing to M0"; it does not own the retry spool, which stays in the parent /
# EvidenceWriter layer. The bridge reports outcomes here.
_HEALTH_LOCK = threading.Lock()
_HEALTH = {"state": READY, "last_error": None, "queued": 0, "recovered": 0}


def record_success() -> None:
    with _HEALTH_LOCK:
        if _HEALTH["state"] != READY:
            _HEALTH["recovered"] += 1
        _HEALTH["state"] = READY
        _HEALTH["last_error"] = None


def record_failure(error_class: str, *, queued: bool) -> None:
    """A failed append is DEGRADED when the evidence is safely queued for retry.

    It is only ERROR when there is no durable fallback, because "worker down but
    the authoritative evidence is waiting in the spool" is not the same failure
    as "the write is simply lost".
    """
    with _HEALTH_LOCK:
        _HEALTH["state"] = DEGRADED if queued else ERROR
        _HEALTH["last_error"] = error_class
        if queued:
            _HEALTH["queued"] += 1


def health() -> dict:
    """Sanitised writer health.

    Deliberately contains no socket path, home directory, payload, user text,
    prompt, key or uid/gid detail; status words and exception class names only.
    """
    if not available():
        return {"state": UNSUPPORTED, "restarts": 0, "queued": 0,
                "recovered": 0, "error": None}
    with _HEALTH_LOCK:
        state = _HEALTH["state"]
        error = _HEALTH["last_error"]
        queued = _HEALTH["queued"]
        recovered = _HEALTH["recovered"]
    with _POOL_LOCK:
        restarts = sum(worker.restarts for worker in _POOL.values())
    return {"state": state, "restarts": restarts, "queued": queued,
            "recovered": recovered, "error": error}


def reset_health() -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(state=READY, last_error=None, queued=0, recovered=0)


def key_for(m0_db, writer_user=None) -> str:
    """The pool key for one (M0 path, writer uid).

    One derivation, used by both the lookup and the release, so a worker can
    never be registered under one key and looked for under another.
    """
    return "%s|%s" % (Path(m0_db).resolve(), writer_user)


def worker_for(m0_db, *, writer_user=None) -> M0WriterWorker:
    """One worker per (M0 path, resolved owner) inside this process."""
    key = key_for(m0_db, writer_user)
    with _POOL_LOCK:
        worker = _POOL.get(key)
        if worker is None:
            worker = M0WriterWorker(Path(m0_db), writer_user=writer_user)
            _POOL[key] = worker
        return worker


def close_for(m0_db, *, writer_user=None) -> bool:
    """Close and forget the worker for one (M0 path, resolved owner).

    Returns whether a worker was actually released. Idempotent: a second call
    finds nothing and returns False. Scoped to one store, so releasing the writer
    of one instance can never stop the writer of another store in this process.
    """
    key = key_for(m0_db, writer_user)
    with _POOL_LOCK:
        worker = _POOL.pop(key, None)
    if worker is None:
        return False
    worker.close()
    return True


def close_all() -> None:
    with _POOL_LOCK:
        workers = list(_POOL.values())
        _POOL.clear()
    for worker in workers:
        worker.close()


def status_all() -> list[dict]:
    with _POOL_LOCK:
        return [worker.status() for worker in _POOL.values()]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--serve" in argv:
        index = argv.index("--serve")
        m0_db = Path(argv[index + 1])
        socket_path = Path(argv[index + 2])
        expected_uid = int(argv[index + 3])
        fault = argv[index + 4] if len(argv) > index + 4 else ""
        return serve(m0_db, socket_path, expected_uid, fault)
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
