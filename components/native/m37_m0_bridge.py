#!/usr/bin/env python3
"""CHIYO | Minimal Native -> M37 (NHE v0) compatibility bridge.

    one real event  ->  exactly one authoritative Evidence

Why this exists
---------------
The M37 formation chain reads exactly one M0 evidence store:

    M0  /var/lib/chiyo-native-v0/memory/evidence.sqlite      (m3-shadow.json: m0_db)
     -> M1  m1_worker.py   episodes
     -> M2  m2_worker.py   understandings
     -> M3  m3_worker.py   recall_shadow
     -> ProductionNativeMemoryResolver  (readonly_recall_adapter)

Before the original-instance cutover, live Hermes turns reached that store.
After the cutover the only Telegram poller is the Native runner, whose turns
land in its own data directory -- so M0 stopped receiving anything and the
whole chain froze on pre-cutover data.

This adapter does exactly one thing: it takes turns the Native runner has
already CONFIRMED as delivered and appends them to that same M0 store, under
the same conversation id the resolver derives from the native binding.

It deliberately does NOT
  * create a second memory store,
  * re-import history,
  * turn a model reply into a user fact (the reply is recorded as
    CHIYO_VISIBLE_OUTPUT, never as user evidence),
  * write a recall result back as evidence,
  * bypass the owner scope or the participant binding.

Idempotency
-----------
The M0 store is its own authority: ``evidence_events`` carries a UNIQUE index
on ``(source_origin, primary_source_ref_id)`` and ``append()`` returns
``duplicate`` for a source ref it already holds.  Restarting the bridge,
retrying a cycle or replaying the runner's ledger therefore cannot mint a
second row for the same real event.  Dedupe is by SOURCE IDENTITY, never by
content hash.

Privileges
----------
The runner's ledger lives in a root-only directory, so the poller runs as
root; the M0 store is owned by ``chiyo-native-v0``, so the write itself is
performed by a child process that drops to that user.  No production
permission is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

LOGGER = logging.getLogger("chiyo.original_m37_bridge")

INTEG_ROOT = Path(__file__).resolve().parent
NATIVE_V0_ROOT = Path(__file__).resolve().parent / "src"
NATIVE_TELEGRAM_ENV = Path(os.environ.get("CHIYO_IDENTITY_ENV", "./config/native/telegram.env"))
NATIVE_PARTICIPANTS = Path(os.environ.get("CHIYO_PARTICIPANTS_FILE", "./config/native/participants.json"))
NATIVE_M3_CONFIG = Path(os.environ.get("CHIYO_M3_CONFIG", "./native/config/m3-shadow.json"))
RUNNER_LEDGER = Path("./data/native/telegram/state.sqlite")
M0_WRITER_USER = os.environ.get("CHIYO_M0_WRITER_USER") or (os.getuid() if hasattr(os, "getuid") else None)
BRIDGE_STATE = INTEG_ROOT / "work" / "m37_bridge_state.json"
#: bounded so a locked store can never hold up a conversation turn
APPEND_TIMEOUT_S = 15.0

SCHEMA = "chiyo-original-m37-m0-bridge-v1"


# --------------------------------------------------------------------------- #
# binding (read, never invented)
# --------------------------------------------------------------------------- #
def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def conversation_id_for(namespace: str, chat_id: str) -> str:
    """Byte-for-byte the derivation the Native runtime and the resolver use."""
    return "tg-" + hashlib.sha256(
        (namespace + ":" + chat_id).encode("utf-8")).hexdigest()[:24]


def load_binding(*, telegram_env=None, participants_path=None) -> dict[str, str]:
    """Namespace + participant come from the live native config, not from us."""
    env = read_env(Path(telegram_env) if telegram_env is not None else NATIVE_TELEGRAM_ENV)
    participants: list[dict] = []
    try:
        participants = json.loads(
            (Path(participants_path) if participants_path is not None else NATIVE_PARTICIPANTS).read_text(encoding="utf-8")).get("participants", [])
    except Exception:
        participants = []
    match = next(
        (p for p in participants if str(p.get("transport")) == "telegram"), {}) or {}
    chat_id = str(match.get("transport_user_id")
                  or env.get("TELEGRAM_ALLOWED_USER_ID") or "")
    namespace = str(env.get("TELEGRAM_CONVERSATION_NAMESPACE") or "native-test-v1")
    if not chat_id:
        raise SystemExit("native telegram binding unresolved: no transport_user_id")
    return {
        "chat_id": chat_id,
        "namespace": namespace,
        "conversation_id": conversation_id_for(namespace, chat_id),
    }


def load_m0_path() -> Path:
    override = os.environ.get("CHIYO_ORIGINAL_M0_DB")
    if override:
        return Path(override)
    cfg = json.loads(NATIVE_M3_CONFIG.read_text(encoding="utf-8"))
    return Path(cfg["m0_db"])


# --------------------------------------------------------------------------- #
# child mode: the actual M0 append, executed as the store's owner
# --------------------------------------------------------------------------- #
def child_append() -> int:
    sys.path.insert(0, str(NATIVE_V0_ROOT))
    from app.evidence import EvidenceEvent, EvidenceStore, new_event_id  # noqa: E402

    payload = json.loads(sys.stdin.read())
    store = EvidenceStore(payload["m0_db"])
    results = []
    for raw in payload["events"]:
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
            results.append({"status": result.status, "event_id": result.event_id,
                            "ref": raw["source_refs"][0]["id"]})
        except Exception as exc:                      # conflict / lock / schema
            results.append({"status": "error", "error_class": type(exc).__name__,
                            "detail": "append rejected",
                            "ref": raw["source_refs"][0]["id"]})
    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# parent mode: read the confirmed ledger, build events, hand them to the child
# --------------------------------------------------------------------------- #
def confirmed_turns(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    connection = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT update_id, chat_id, message_id, turn_id, user_text, raw_content, "
            "telegram_message_id, created_at, updated_at "
            "FROM adapter_updates WHERE state IN ('DELIVERED','TELEGRAM_CONFIRMED') "
            "AND telegram_message_id IS NOT NULL "
            "AND user_text IS NOT NULL AND raw_content IS NOT NULL "
            "ORDER BY update_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def build_events(turn: dict, binding: dict) -> list[dict]:
    """A confirmed turn -> the two M0 events that describe it truthfully."""
    chat_id = str(turn["chat_id"])
    conversation_id = binding["conversation_id"]
    turn_id = str(turn["turn_id"])
    received_at = str(turn["created_at"])
    delivered_at = str(turn["updated_at"] or turn["created_at"])
    user_ref = f"telegram:{chat_id}:{turn['message_id']}"
    chiyo_ref = f"telegram:{chat_id}:{turn['telegram_message_id']}"
    return [
        {
            "occurred_at": received_at,
            "created_at": received_at,
            "source_origin": "USER_VISIBLE_INPUT",
            "delivery_status": "RECEIVED",
            "epistemic_role": "OBSERVED_EXTERNAL_EXPRESSION",
            "speaker": "user",
            "content": str(turn["user_text"]),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "source_refs": [
                {"kind": "native_telegram_input", "id": user_ref},
                {"kind": "native_turn", "id": turn_id},
            ],
        },
        {
            "occurred_at": delivered_at,
            "created_at": delivered_at,
            "source_origin": "CHIYO_VISIBLE_OUTPUT",
            "delivery_status": "DELIVERED",
            "epistemic_role": "SELF_EXPRESSION",
            "speaker": "chiyo",
            "content": str(turn["raw_content"]),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "source_refs": [
                {"kind": "native_telegram_output", "id": chiyo_ref},
                {"kind": "native_turn", "id": turn_id},
            ],
        },
    ]


def already_present(m0_db: Path, events: list[dict]) -> set[str]:
    """Source refs the M0 store already holds.  Read-only, best effort."""
    if not m0_db.exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{m0_db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return set()
    try:
        found = set()
        for event in events:
            ref = event["source_refs"][0]["id"]
            row = connection.execute(
                "SELECT 1 FROM evidence_events WHERE source_origin=? "
                "AND primary_source_ref_id=?",
                (event["source_origin"], ref),
            ).fetchone()
            if row:
                found.add(ref)
        return found
    except sqlite3.Error:
        return set()
    finally:
        connection.close()


def append_via_child(m0_db: Path, events: list[dict], *, writer_user=M0_WRITER_USER) -> list[dict]:
    """Append events to M0 through a child process that runs as the store's owner."""
    if not events:
        return []
    payload = json.dumps({"m0_db": str(m0_db), "events": events}, ensure_ascii=False)
    identity = {}
    if writer_user is not None:
        import pwd
        target = int(writer_user) if isinstance(writer_user, int) or str(writer_user).isdigit() else pwd.getpwnam(str(writer_user)).pw_uid
        if target != os.getuid():
            if os.geteuid() != 0:
                raise PermissionError("only root can hand off to a different M0 writer")
            identity = dict(user=target, group=os.environ.get("CHIYO_M0_WRITER_GROUP") or pwd.getpwuid(target).pw_gid, extra_groups=[])
    proc = subprocess.run(
        [sys.executable, "-S", str(Path(__file__).resolve()), "--child-append"],
        input=payload, capture_output=True, text=True, timeout=APPEND_TIMEOUT_S,
        **identity,
    )
    if proc.returncode != 0:
        raise RuntimeError("m0 child append failed rc=%s" % proc.returncode)
    return json.loads(proc.stdout)["results"]


def write_user_event(conversation_id: str, user_text: str, source_ref: str,
                     turn_id: str, occurred_at: str, source_kind: str = "native_telegram_input", source_context: str | None = None,
                     m0_db=None, writer_user=M0_WRITER_USER) -> dict:
    """Record the user's inbound turn in M0 *at receipt*.

    The resolver anchors recall on the newest persisted user turn, so the inbound
    event has to exist before the reply is generated; waiting for the delivery
    cycle would anchor recall on the previous turn instead.  The M0 store's
    unique index on (source_origin, primary_source_ref_id) means the delivery
    cycle can re-offer this same event without ever creating a second row.
    """
    event = {
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "source_origin": "USER_VISIBLE_INPUT",
        "delivery_status": "RECEIVED",
        "epistemic_role": "OBSERVED_EXTERNAL_EXPRESSION",
        "speaker": "user",
        "content": str(user_text),
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "source_refs": [
            {"kind": source_kind, "id": source_ref},
            {"kind": "native_turn", "id": turn_id},
        ],
    }
    if source_context:event["source_refs"].append({"kind":"hermes_personal_scope","id":source_context})
    results = append_via_child(load_m0_path() if m0_db is None else Path(m0_db), [event], writer_user=writer_user)
    return results[0] if results else {}


def run_cycle(m0_db: Path, binding: dict) -> dict:
    turns = confirmed_turns(RUNNER_LEDGER)
    pending: list[dict] = []
    skipped = 0
    for turn in turns:
        if str(turn['chat_id']) != str(binding['chat_id']):
            continue
        events = build_events(turn, binding)
        present = already_present(m0_db, events)
        kept = [e for e in events if e["source_refs"][0]["id"] not in present]
        skipped += len(events) - len(kept)
        pending.extend(kept)
    if not pending:
        return {"turns": len(turns), "appended": 0, "skipped_present": skipped,
                "errors": 0, "m0_db": str(m0_db),
                "conversation_id": binding["conversation_id"]}
    try:
        results = append_via_child(m0_db, pending)
    except Exception as exc:
        return {"turns": len(turns), "appended": 0, "skipped_present": skipped,
                "errors": len(pending), "m0_db": str(m0_db),
                "child_error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    inserted = sum(1 for r in results if r["status"] == "inserted")
    duplicates = sum(1 for r in results if r["status"] == "duplicate")
    errors = [r for r in results if r["status"] == "error"]
    for err in errors:
        LOGGER.error("m0.append.error ref_hash=%s class=%s",
                     hashlib.sha256(str(err.get("ref")).encode()).hexdigest()[:24], err.get("error_class"))
    return {"turns": len(turns), "appended": inserted, "duplicate_after_check": duplicates,
            "skipped_present": skipped, "errors": len(errors),
            "m0_db": str(m0_db), "conversation_id": binding["conversation_id"]}


def write_assistant_event(*, conversation_id: str, content: str, source_ref: str,
                          turn_id: str, occurred_at: str, binding=None, m0_db=None, writer_user=M0_WRITER_USER) -> dict:
    binding = load_binding() if binding is None else binding
    if conversation_id != binding['conversation_id'] or not source_ref.startswith('telegram:' + str(binding['chat_id']) + ':'):
        raise ValueError('confirmed output binding mismatch')
    event = {'occurred_at': occurred_at, 'created_at': occurred_at,
        'source_origin': 'CHIYO_VISIBLE_OUTPUT', 'delivery_status': 'DELIVERED',
        'epistemic_role': 'SELF_EXPRESSION', 'speaker': 'chiyo', 'content': content,
        'conversation_id': conversation_id, 'turn_id': turn_id,
        'source_refs': [{'kind': 'native_telegram_output', 'id': source_ref},
                        {'kind': 'native_turn', 'id': turn_id}]}
    results = append_via_child(load_m0_path() if m0_db is None else Path(m0_db), [event], writer_user=writer_user)
    return results[0] if results else {'status': 'failed'}


class ConfiguredBridge:
    """Instance-owned paths and writer identity; no process environment mutation."""
    def __init__(self, environment):
        self.environment = dict(environment)

    def load_binding(self):
        return load_binding(telegram_env=self.environment.get('CHIYO_IDENTITY_ENV'),
                            participants_path=self.environment.get('CHIYO_PARTICIPANTS_FILE'))

    def load_m0_path(self):
        override = self.environment.get('CHIYO_ORIGINAL_M0_DB')
        if override:
            return Path(override)
        config = Path(self.environment.get('CHIYO_M3_CONFIG', str(NATIVE_M3_CONFIG)))
        return Path(json.loads(config.read_text(encoding='utf8'))['m0_db'])

    def write_user_event(self, **kwargs):
        return write_user_event(**kwargs, m0_db=self.load_m0_path(),
                                writer_user=self.environment.get('CHIYO_M0_WRITER_USER', os.getuid() if hasattr(os,'getuid') else None))

    def write_assistant_event(self, **kwargs):
        return write_assistant_event(**kwargs, binding=self.load_binding(), m0_db=self.load_m0_path(),
                                     writer_user=self.environment.get('CHIYO_M0_WRITER_USER', os.getuid() if hasattr(os,'getuid') else None))


def main() -> int:
    parser = argparse.ArgumentParser(description="Native -> M37 M0 compatibility bridge")
    parser.add_argument("--child-append", action="store_true",
                        help="internal: append stdin payload to M0 (runs as store owner)")
    parser.add_argument("--once", action="store_true", help="single cycle, then exit")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--status", action="store_true", help="report state and exit")
    args = parser.parse_args()

    if args.child_append:
        return child_append()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    binding = load_binding()
    m0_db = load_m0_path()

    if args.status:
        print(json.dumps({
            "schema": SCHEMA,
            "runner_ledger": str(RUNNER_LEDGER),
            "runner_ledger_exists": RUNNER_LEDGER.exists(),
            "m0_db": str(m0_db),
            "m0_db_exists": m0_db.exists(),
            "m0_writer_user": M0_WRITER_USER,
            "binding": binding,
            "confirmed_turns": len(confirmed_turns(RUNNER_LEDGER)),
        }, indent=2, ensure_ascii=False))
        return 0

    LOGGER.info("bridge.ready m0_db=%s conversation_id=%s ledger=%s",
                m0_db, binding["conversation_id"], RUNNER_LEDGER)
    while True:
        try:
            report = run_cycle(m0_db, binding)
            if report.get("appended") or report.get("errors"):
                LOGGER.info("bridge.cycle %s", json.dumps(report, ensure_ascii=False))
            BRIDGE_STATE.parent.mkdir(parents=True, exist_ok=True)
            BRIDGE_STATE.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
        except Exception as exc:                       # never die on one bad cycle
            LOGGER.error("bridge.cycle_failed class=%s detail=%s",
                         type(exc).__name__, str(exc)[:200])
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
