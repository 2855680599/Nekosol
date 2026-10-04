"""Owner-scoped logical forgetting, projected onto recall and conversation context.

M0 remains append-only. Every control refers to existing evidence, and corrections
require another direct user event. The ledger is re-read per request; a damaged or
missing configured ledger excludes all historical context instead of reviving it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def readonly(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    return c


@dataclass
class MemoryProjection:
    blocked_events: set[str] = field(default_factory=set)
    blocked_turns: set[str] = field(default_factory=set)
    replacements: dict[str, Any] = field(default_factory=dict)
    corrected_events: dict[str, str] = field(default_factory=dict)
    healthy: bool = True
    controls: int = 0
    history_after: str | None = None

    def recalls(self, objects: Any) -> tuple[list[Any], list[str]]:
        if not self.healthy:
            return [], []
        allowed = []
        corrections = []
        for obj in objects or ():
            ids = {str(x) for x in getattr(obj, 'source_event_ids', ())}
            if self.controls and not ids:
                continue
            denied = ids & self.blocked_events
            if denied:
                for event_id in sorted(denied):
                    replacement = self.replacements.get(event_id)
                    if replacement and replacement['event_id'] not in self.blocked_events:
                        corrections.append(replacement['content'])
                continue
            rewritten = ids & self.corrected_events.keys()
            if rewritten:
                corrections.extend(self.corrected_events[event_id] for event_id in sorted(rewritten))
                continue
            allowed.append(obj)
        return allowed, list(dict.fromkeys(corrections))

    def active_corrections(self) -> list[str]:
        if not self.healthy:return []
        return list(dict.fromkeys(text for event_id,text in self.corrected_events.items()
            if event_id not in self.blocked_events))[-10:]

    def history(self, records: list[dict]) -> list[dict]:
        if not self.healthy:
            return []
        kept = []
        cutoff = datetime.fromisoformat(self.history_after) if self.history_after else None
        for r in records:
            if str(r.get('turn_id', '')) in self.blocked_turns:
                continue
            if cutoff:
                try:
                    timestamp = datetime.fromisoformat(str(r['timestamp']).replace('Z', '+00:00'))
                    if timestamp <= cutoff:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
            kept.append(r)
        return kept


class MemoryControlLedger:
    def __init__(self, path: Path | str, evidence: Path | str, *, owner: str, conversation: str):
        if not owner or not conversation:
            raise ValueError('explicit owner and evidence conversation are required')
        self.path, self.evidence = Path(path), Path(evidence)
        self.owner, self.conversation = str(owner), str(conversation)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path)
        try:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS memory_controls(
                operation_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                conversation TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('DELETE','CORRECT')),
                target_event TEXT NOT NULL, replacement_event TEXT,
                fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS controls_no_update BEFORE UPDATE ON memory_controls
              BEGIN SELECT RAISE(ABORT,'memory controls are append-only'); END;
              CREATE TRIGGER IF NOT EXISTS controls_no_delete BEFORE DELETE ON memory_controls
              BEGIN SELECT RAISE(ABORT,'memory controls are append-only'); END;
            ''')
            c.commit()
        finally:
            c.close()
        os.chmod(self.path, 0o600)

    def _event(self, c: sqlite3.Connection, event_id: str) -> dict:
        row = c.execute('SELECT * FROM evidence_events WHERE event_id=? AND conversation_id=?',
                        (event_id, self.conversation)).fetchone()
        if row is None:
            raise ValueError('evidence absent or outside owner conversation')
        return dict(row)

    def apply(self, *, operation_id: str, kind: str, target: str,
              replacement: str | None = None, actor: str) -> str:
        if actor != self.owner:
            raise PermissionError('memory control owner mismatch')
        if kind not in ('DELETE', 'CORRECT') or not operation_id or not target:
            raise ValueError('invalid memory control')
        if kind == 'DELETE' and replacement is not None:
            raise ValueError('deletion cannot carry a replacement')
        if kind == 'CORRECT' and (not replacement or replacement == target):
            raise ValueError('a distinct direct user correction event is required')
        c = readonly(self.evidence)
        try:
            self._event(c, target)
            if replacement:
                event = self._event(c, replacement)
                if event['speaker'] != 'user' or event['source_origin'] != 'USER_VISIBLE_INPUT':
                    raise ValueError('correction must come from direct user input')
        finally:
            c.close()
        fingerprint = hashlib.sha256(json.dumps([self.owner, self.conversation, kind,
                                      target, replacement], separators=(',', ':')).encode()).hexdigest()
        c = sqlite3.connect(self.path, timeout=3)
        try:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT fingerprint FROM memory_controls WHERE operation_id=?',
                            (operation_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError('operation identity collision')
                return 'DUPLICATE'
            c.execute('INSERT INTO memory_controls VALUES(?,?,?,?,?,?,?,?)',
                      (operation_id, self.owner, self.conversation, kind, target, replacement,
                       fingerprint, datetime.now(timezone.utc).isoformat()))
            c.commit()
            return 'APPLIED'
        finally:
            c.close()

    def command(self, text: str, source_ref: str) -> str:
        parts = text.split(maxsplit=3)
        if parts == ['/memory', 'list']:
            projection = self.projection()
            if not projection.healthy:
                raise ValueError('control ledger unavailable')
            c = readonly(self.evidence)
            try:
                rows = c.execute('SELECT event_id,content FROM evidence_events WHERE conversation_id=? '
                    'AND speaker=? AND source_origin=? ORDER BY rowid DESC LIMIT 100',
                    (self.conversation, 'user', 'USER_VISIBLE_INPUT')).fetchall()
            finally:
                c.close()
            visible = [r for r in rows if r['event_id'] not in projection.blocked_events
                       and not r['content'].startswith('/memory ')]
            return '\n\n'.join(str(r['event_id']) + '\n' + r['content'][:120] for r in visible[:10]) or '目前没有可列出的记忆。'
        if len(parts) < 3 or parts[1] not in ('delete', 'correct') or (parts[1] == 'correct' and len(parts) != 4):
            return '先用 /memory list 查看记忆ID；删除用 /memory delete 记忆ID；纠正用 /memory correct 记忆ID 正确内容。'
        c = readonly(self.evidence)
        try:
            row = c.execute('SELECT event_id FROM evidence_events WHERE conversation_id=? '
                            'AND primary_source_ref_id=? AND speaker=? AND source_origin=?',
                            (self.conversation, source_ref, 'user', 'USER_VISIBLE_INPUT')).fetchone()
            if row is None:
                raise ValueError('current direct user evidence unavailable')
            event_id = str(row[0])
        finally:
            c.close()
        self.apply(operation_id='user-control:' + event_id, kind=parts[1].upper(),
                   target=parts[2], replacement=event_id if parts[1] == 'correct' else None,
                   actor=self.owner)
        return '已停止使用这条记忆。' if parts[1] == 'delete' else '已停止使用旧记忆，后续以你的纠正为准。'

    def projection(self) -> MemoryProjection:
        out = MemoryProjection()
        try:
            c = readonly(self.path)
            try:
                if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('memory controls integrity failure')
                rows = c.execute('SELECT * FROM memory_controls WHERE owner=? AND conversation=? '
                                 'ORDER BY rowid', (self.owner, self.conversation)).fetchall()
            finally:
                c.close()
            e = readonly(self.evidence)
            try:
                for row in rows:
                    event = self._event(e, row['target_event'])
                    siblings = e.execute('SELECT event_id,turn_id FROM evidence_events '
                                         'WHERE conversation_id=? AND turn_id=?',
                                         (self.conversation, event['turn_id'])).fetchall()
                    out.blocked_events.update(str(s['event_id']) for s in siblings)
                    out.blocked_turns.add(str(event['turn_id']))
                    out.controls += 1
                    if row['kind'] == 'CORRECT':
                        replacement = self._event(e, row['replacement_event'])
                        command = replacement['content'].split(maxsplit=3)
                        if len(command) == 4 and command[:2] == ['/memory', 'correct'] and command[2] == row['target_event']:
                            replacement['content'] = command[3]
                        out.corrected_events[replacement['event_id']] = replacement['content']
                        for sibling in siblings:
                            out.replacements[str(sibling['event_id'])] = replacement
                    else:
                        for sibling in siblings:
                            out.replacements.pop(str(sibling['event_id']), None)
                # Historical output may repeat a forgotten fact. Once a control exists,
                # project from a fresh conversation epoch; raw stored records stay intact.
                out.history_after = rows[-1]['created_at'] if rows else None
                if rows:
                    # Assistant history is not independent user authority and may repeat a
                    # forgotten fact across turns. Exclude older self-history as a whole.
                    prior = e.execute('SELECT event_id,content,speaker FROM evidence_events '
                        'WHERE conversation_id=? AND created_at<=?',
                        (self.conversation, out.history_after)).fetchall()
                    out.blocked_events.update(str(r['event_id']) for r in prior if r['speaker'] == 'chiyo'
                        or (r['content'].startswith('/memory ') and r['event_id'] not in out.corrected_events))
            finally:
                e.close()
            return out
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return MemoryProjection(healthy=False)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger', required=True); p.add_argument('--evidence', required=True)
    p.add_argument('--owner', required=True); p.add_argument('--conversation', required=True)
    p.add_argument('--operation-id', required=True); p.add_argument('--kind', choices=['DELETE','CORRECT'], required=True)
    p.add_argument('--target', required=True); p.add_argument('--replacement')
    a = p.parse_args()
    ledger = MemoryControlLedger(a.ledger, a.evidence, owner=a.owner, conversation=a.conversation)
    print(json.dumps({'status': ledger.apply(operation_id=a.operation_id, kind=a.kind,
                       target=a.target, replacement=a.replacement, actor=a.owner)}))


if __name__ == '__main__':
    main()
