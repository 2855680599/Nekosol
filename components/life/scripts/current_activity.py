"""Read-only Activity projection compatibility surface; legacy writes reject."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Optional
from activity_compat import ActivityError, ActivityTransitionError, now, _iso, _parse, _compact_value, BJT, ActivityLockError, CompletionEvidenceError

def _read_canonical_projection() -> dict[str, Any]:
    root = os.environ.get("M16F1R2_CANONICAL_STORE_ROOT")
    if not root:
        raise ActivityError("CANONICAL_ACTIVITY_STORE_ROOT_REQUIRED")
    resolved_root = Path(root).expanduser().resolve()
    candidate_root = Path("/dev/shm/m16f1r7-20260928T200000Z/candidate").resolve()
    try:
        resolved_root.relative_to(candidate_root)
    except ValueError as exc:
        raise ActivityError("PROJECTION_ROOT_OUTSIDE_ISOLATED_CANDIDATE") from exc
    from activity_continuity import CanonicalActivityStore, ActivityReadService
    service = ActivityReadService(CanonicalActivityStore(root))
    view = service.get_current_life_view()
    activity_id = view.get("foreground_activity_ref")
    activity = view.get("activities", {}).get(activity_id) if activity_id else None
    if activity is None:
        return {"activity_id": None, "activity": "", "status": "idle", "is_projection": True, "source_revision": view.get("revision", 0)}
    return {"activity_id": activity.get("activity_id"), "activity": activity.get("title", ""), "status": str(activity.get("status", "idle")).lower(), "started_at": activity.get("occurred_at"), "is_projection": True, "source_revision": view.get("revision", 0)}

def _assert_legacy_v2_not_production_canonical(target_dir):
    path = Path(target_dir).expanduser().resolve()
    for production_root in (Path("./data/world").resolve(), Path("./data/persona").resolve(), Path("/root/.hermes-world-hk"), Path("/root/.hermes-stock"), Path("/root/.hermes")):
        try:
            path.relative_to(production_root)
        except ValueError:
            continue
        raise ActivityError("legacy_v2_production_write_denied")

def show() -> dict[str, Any]:
    return _read_canonical_projection()

def load_state() -> dict[str, Any]:
    return _read_canonical_projection()

def duration_seconds(state: Optional[dict[str, Any]] = None, at=None) -> int:
    raise ActivityError("LEGACY_ACTIVITY_WRITE_REJECTED: use canonical read model")

def _reject(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise ActivityTransitionError("LEGACY_ACTIVITY_WRITE_REJECTED: use LR-2 ActivityCommandService")

# Legacy mutators are kept as explicit deny stubs so callers fail deterministically.
start = _reject
pause = _reject
resume = _reject
finish = _reject
idle = _reject
interrupt = _reject
sync_for_tick = _reject
_persist_unlocked = _reject
_append_log_unlocked = _reject

def main(argv=None) -> int:
    import argparse
    parser=argparse.ArgumentParser(description="read-only canonical Activity projection")
    parser.add_argument("command",choices=("show","start","pause","resume","complete","abandon","idle"))
    args=parser.parse_args(argv)
    if args.command != "show":
        _reject()
    print(show())
    return 0
