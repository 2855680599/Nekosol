"""Original Instance Continuity Bridge for Chiyo Native Runtime.
Integrates:
1. Canonical Persona & Relationship (USER.md, MEMORY.md, relationship.yaml)
2. Historical Conversation Reader (read-only from ./data/persona/state.db)
3. Production M37 Memory Resolver (./native/)
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("chiyo.original_integration")

HERMES_HOME = Path("./data/persona")
M37_ROOT = Path("./native")

class CanonicalPersonaProvider:
    """Renders the true prompt components without modifying files."""
    def __init__(self, hermes_home: Path = HERMES_HOME, *, include_legacy: bool = False):
        self.hermes_home = hermes_home
        self.include_legacy = include_legacy

    def load_system_prompt(self) -> str:
        blocks = []
        # Base identity
        blocks.append("你是千代，18岁。")
        
        # Canonical Relationship
        rel_file = self.hermes_home / "canonical" / "relationship.yaml"
        if rel_file.exists():
            blocks.append("【关系事实】\n" + rel_file.read_text(encoding="utf-8").strip())
            
        # MEMORY.md
        mem_file = self.hermes_home / "memories" / "MEMORY.md"
        if self.include_legacy and mem_file.exists():
            blocks.append("【历史互动约定】\n" + mem_file.read_text(encoding="utf-8").strip())

        # USER.md
        user_file = self.hermes_home / "memories" / "USER.md"
        if self.include_legacy and user_file.exists():
            blocks.append("【对话对象（用户/Owner）事实】\n" + user_file.read_text(encoding="utf-8").strip())

        return "\n\n".join(blocks)

class HermesHistoryReader:
    """Reads historical turns read-only from Hermes state.db."""
    def __init__(self, db_path: Path = HERMES_HOME / "state.db"):
        self.db_path = db_path

    def get_recent_turns(self, user_id: str = "42", limit: int = 10) -> list[dict[str, str]]:
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            # find latest session for this user
            sess = con.execute("SELECT id FROM sessions WHERE user_id=? ORDER BY last_activity_at DESC LIMIT 1", (user_id,)).fetchone()
            if not sess:
                return []
            session_id = sess["id"]
            msgs = con.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND role IN ('user', 'assistant') "
                "ORDER BY id DESC LIMIT ?", (session_id, limit)
            ).fetchall()
            turns = []
            for r in reversed(msgs):
                c = r["content"] or ""
                # filter out system annotations if prefixed
                if c.startswith("[") and "\n\n" in c:
                    c = c.split("\n\n", 1)[1]
                turns.append({"role": r["role"], "content": c})
            return turns
        finally:
            con.close()

class ProductionM37Bridge:
    """Invokes M37 ProductionNativeMemoryResolver if available."""
    def __init__(self, runtime_root: Path = M37_ROOT):
        self.runtime_root = runtime_root
        self.resolver = None
        if str(runtime_root) not in sys.path:
            sys.path.insert(0, str(runtime_root))
        try:
            from memory_runtime_v1.production_resolver import ProductionNativeMemoryResolver
            self.resolver = ProductionNativeMemoryResolver(
                wiring_path="./config/native/memory-context-wiring.json"
            )
            LOGGER.info("M37 Production resolver loaded successfully")
        except Exception as e:
            LOGGER.warning("M37 Resolver load skipped or failed: %s", e)

    def resolve(self, user_id: str, cue_text: str) -> str | None:
        if not self.resolver:
            return None
        # M37 scope check
        if user_id != "42":
            return None
        try:
            # Call resolver
            # M37 resolver takes native_conversation_id or call directly
            res = self.resolver.resolve_current_turn("tg-3e105e2172d0b4fd45ee54c0")
            return res
        except Exception as e:
            LOGGER.error("M37 resolve exception: %s", e)
            return None
