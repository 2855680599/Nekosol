"""Tests for the bounded kanban init lock (issue #36644).

`connect()` wrapped its entire body in an unbounded blocking `flock(LOCK_EX)`
on every call. A single process stalled inside the critical section blocked the
long-lived gateway dispatcher's next-tick `connect()` forever — no timeout, no
recovery, board silently stops being worked.

Two fixes, both covered here:
1. Fast path: once a path is initialized in this process, `connect()` skips the
   cross-process init lock entirely (nothing left to serialize), so a held lock
   cannot block a steady-state connect.
2. Bounded acquire: even on first-init, `_cross_process_init_lock` retries a
   non-blocking acquire up to a deadline, then proceeds (with a WARNING) rather
   than hanging.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return home


def _hold_init_lock(db_path: Path):
    """Return (start_event, release_event, thread) holding the init lock."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with kbc._cross_process_init_lock(db_path):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holding.wait(timeout=5), "holder thread never acquired the lock"
    return release, t


def test_initialized_path_connect_skips_init_lock(kanban_home, monkeypatch):
    """A connect to an already-initialized path must not block on the init lock."""
    db_path = kb.kanban_db_path(board="default")
    # Initialize once.
    kbc.connect().close()
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

    # Hold the init lock; a fast-path connect must return promptly anyway.
    release, t = _hold_init_lock(db_path)
    try:
        def unexpected_init_lock(path):
            pytest.fail("initialized-path connect attempted the cross-process init lock")

        monkeypatch.setattr(kbc, "_cross_process_init_lock", unexpected_init_lock)
        kbc.connect().close()
        assert t.is_alive(), "holder released before the fast-path check"
    finally:
        release.set()
        t.join(timeout=5)
        assert not t.is_alive(), "holder did not stop after release"


def test_first_init_connect_is_bounded_when_lock_held(kanban_home, monkeypatch, caplog):
    """First-init connect must time out the cross-process lock and proceed,
    not hang forever, when another holder owns it."""
    monkeypatch.setattr(kbc, "_INIT_LOCK_TIMEOUT_SECONDS", 0.6)
    db_path = kb.kanban_db_path(board="default")

    release, t = _hold_init_lock(db_path)
    try:
        # Exercise the real non-blocking file lock and real schema initialization,
        # but advance only the acquire deadline with a controlled clock. Timing
        # connect() also measured filesystem syncs and made this fail under load.
        clock = [100.0]
        sleeps = []
        attempts = []
        real_try_lock = kbc._try_lock_nb

        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        def try_lock(handle):
            acquired = real_try_lock(handle)
            attempts.append(acquired)
            return acquired

        monkeypatch.setattr(kbc, "time", SimpleNamespace(
            monotonic=lambda: clock[0], sleep=advance,
        ))
        monkeypatch.setattr(kbc, "_try_lock_nb", try_lock)
        conn = kbc.connect()  # path NOT yet initialized — must take the bounded path
        try:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        finally:
            conn.close()
        assert t.is_alive(), "holder released before the bounded-acquire check"
        assert attempts and not any(attempts), "connect acquired the held lock"
        assert sleeps and all(s == kbc._INIT_LOCK_POLL_SECONDS for s in sleeps)
        elapsed = clock[0] - 100.0
        assert 0.6 <= elapsed <= 0.6 + kbc._INIT_LOCK_POLL_SECONDS + 1e-9
        assert "not acquired within" in caplog.text
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS
    finally:
        release.set()
        t.join(timeout=5)
        assert not t.is_alive(), "holder did not stop after release"
