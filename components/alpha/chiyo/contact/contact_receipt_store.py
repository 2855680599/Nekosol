"""Thread-serialized append-only in-memory receipt journal and read model."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Iterable

from contact_receipt_model import ContactTransportReceiptEvidence, ReceiptSupersession

@dataclass(frozen=True, slots=True)
class ReceiptJournalEntry:
    sequence: int
    evidence: ContactTransportReceiptEvidence

class ContactReceiptStore:
    """A deterministic process-memory journal; not durable persistence."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._journal: list[ReceiptJournalEntry] = []
        self._evidence: dict[str, ContactTransportReceiptEvidence] = {}
        self._actions: dict[str, list[str]] = {}
        self._submissions: dict[str, list[str]] = {}
        self._providers: dict[str, str] = {}
        self._supersessions: list[ReceiptSupersession] = []
        self.append_count = 0
        self.replay_count = 0

    @property
    def journal(self) -> tuple[ReceiptJournalEntry, ...]:
        with self._lock:
            return tuple(self._journal)

    @property
    def evidence_count(self) -> int:
        with self._lock:
            return len(self._evidence)

    @property
    def supersessions(self) -> tuple[ReceiptSupersession, ...]:
        with self._lock:
            return tuple(self._supersessions)

    def append(self, evidence: ContactTransportReceiptEvidence, *,
               crash_after_journal_append: bool = False,
               assume_indexed: bool = False) -> bool:
        """Append one evidence row.

        `assume_indexed=True` states that the caller already rebuilt the read
        model from the canonical journal, so the defensive read-model-vs-journal
        scan is skipped. It changes no semantics: the same uniqueness checks and
        the same duplicate/collision errors still apply, only the O(n) history
        scan is omitted.
        """
        if not isinstance(evidence, ContactTransportReceiptEvidence):
            raise TypeError("only frozen ContactTransportReceiptEvidence can be appended")
        with self._lock:
            prior = self._evidence.get(evidence.evidence_ref)
            if prior is not None:
                if prior.semantic_fingerprint != evidence.semantic_fingerprint:
                    raise ValueError("semantic evidence identity collision")
                return False
            if not assume_indexed:
                # A prior append may have reached the WAL before a crash skipped
                # the read-model index; only the non-authoritative path needs it.
                journal_prior = next((entry.evidence for entry in self._journal
                                      if entry.evidence.evidence_ref == evidence.evidence_ref), None)
                if journal_prior is not None:
                    if journal_prior.semantic_fingerprint != evidence.semantic_fingerprint:
                        raise ValueError("journal evidence identity collision")
                    self.recover()
                    return False
            self._check_unique_locked(evidence)
            self._journal.append(ReceiptJournalEntry(len(self._journal) + 1, evidence))
            self.append_count += 1
            if crash_after_journal_append:
                raise RuntimeError("synthetic crash after journal append before read-model update")
            self._index_locked(evidence)
            return True

    def _check_unique_locked(self, evidence: ContactTransportReceiptEvidence) -> None:
        for prior_ref in self._submissions.get(evidence.submission_key, ()):
            if self._evidence[prior_ref].action_ref != evidence.action_ref:
                raise ValueError("submission key is already bound to a different action")
        if evidence.provider_message_ref is not None:
            prior_ref = self._providers.get(evidence.provider_message_ref)
            if prior_ref is not None and self._evidence[prior_ref].action_ref != evidence.action_ref:
                raise ValueError("provider message reference collides across actions")

    def validate_unique(self, evidence: ContactTransportReceiptEvidence) -> None:
        """Pure uniqueness pre-flight for `evidence`; mutates nothing.

        Costs O(refs already bound to the same submission key or provider ref)
        instead of scanning the whole receipt history, and raises exactly the
        errors `append` would raise for a genuine collision.
        """
        if not isinstance(evidence, ContactTransportReceiptEvidence):
            raise TypeError("only frozen ContactTransportReceiptEvidence can be appended")
        with self._lock:
            self._check_unique_locked(evidence)

    def _index_locked(self, evidence: ContactTransportReceiptEvidence) -> None:
        if evidence.evidence_ref in self._evidence:
            return
        self._evidence[evidence.evidence_ref] = evidence
        self._actions.setdefault(evidence.action_ref, []).append(evidence.evidence_ref)
        self._submissions.setdefault(evidence.submission_key, []).append(evidence.evidence_ref)
        if evidence.provider_message_ref is not None:
            self._providers[evidence.provider_message_ref] = evidence.evidence_ref

    def add_supersession(self, item: ReceiptSupersession) -> bool:
        if not isinstance(item, ReceiptSupersession):
            raise TypeError("supersession must be typed and frozen")
        with self._lock:
            if item in self._supersessions:
                return False
            if item.old_evidence_ref not in self._evidence or item.new_evidence_ref not in self._evidence:
                raise ValueError("supersession evidence must already be journaled")
            old, new = self._evidence[item.old_evidence_ref], self._evidence[item.new_evidence_ref]
            if old.action_ref != item.action_ref or new.action_ref != item.action_ref:
                raise ValueError("supersession evidence must belong to its action")
            self._supersessions.append(item)
            return True

    def load(self, entries: Iterable[ReceiptJournalEntry], supersessions: Iterable[ReceiptSupersession] = ()) -> None:
        """Rebuild indexes from immutable entries, rejecting gaps or tampering."""
        with self._lock:
            self._clear_locked()
            for expected, entry in enumerate(entries, 1):
                if not isinstance(entry, ReceiptJournalEntry) or entry.sequence != expected:
                    raise ValueError("receipt journal sequence is invalid")
                self._check_unique_locked(entry.evidence)
                if entry.evidence.evidence_ref in self._evidence:
                    raise ValueError("duplicate evidence row in journal")
                self._journal.append(entry)
                self._index_locked(entry.evidence)
            for item in supersessions:
                self.add_supersession(item)
            self.replay_count += 1

    def replay(self) -> tuple[ReceiptJournalEntry, ...]:
        with self._lock:
            self.replay_count += 1
            return tuple(self._journal)

    def recover(self) -> None:
        with self._lock:
            # Journal is the write-ahead truth; preserve post-append crash entries while rebuilding.
            entries, supersessions = tuple(self._journal), tuple(self._supersessions)
            self._evidence.clear()
            self._actions.clear()
            self._submissions.clear()
            self._providers.clear()
            self._supersessions.clear()
            for expected, entry in enumerate(entries, 1):
                if entry.sequence != expected:
                    raise ValueError("receipt journal sequence is invalid")
                self._check_unique_locked(entry.evidence)
                self._index_locked(entry.evidence)
            for item in supersessions:
                self.add_supersession(item)
            self.replay_count += 1

    def _clear_locked(self) -> None:
        self._journal.clear()
        self._evidence.clear()
        self._actions.clear()
        self._submissions.clear()
        self._providers.clear()
        self._supersessions.clear()
        self.append_count = 0

    def find(self, evidence_ref: str) -> ContactTransportReceiptEvidence | None:
        with self._lock:
            return self._evidence.get(evidence_ref)

    def by_action(self, action_ref: str) -> tuple[ContactTransportReceiptEvidence, ...]:
        with self._lock:
            return tuple(self._evidence[ref] for ref in self._actions.get(action_ref, ()))

    def by_submission(self, submission_key: str) -> tuple[ContactTransportReceiptEvidence, ...]:
        with self._lock:
            return tuple(self._evidence[ref] for ref in self._submissions.get(submission_key, ()))

    def find_provider(self, provider_message_ref: str) -> ContactTransportReceiptEvidence | None:
        with self._lock:
            ref = self._providers.get(provider_message_ref)
            return self._evidence.get(ref) if ref else None

__all__ = ["ContactReceiptStore", "ReceiptJournalEntry"]
