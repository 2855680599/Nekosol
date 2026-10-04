#!/usr/bin/env python3
"""Read-only V0 bridge from injected World State to bounded Self context."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from world_foundation import (
    WorldStateStore,
    project_perception,
    world_local_time,
)


BRIDGE_SCHEMA_VERSION = "world.perception.bridge.v0"
_OUTPUT_FIELDS = frozenset(
    {"schema_version", "world_id", "world_time", "location", "scene", "perceived_facts"}
)


class WorldPerceptionBridge:
    """Project one explicitly configured World State read into bounded context.

    The path and observer facts are injected by the caller.  Construction and
    import never read production state; ``read`` performs one fail-closed
    projection and never calls ``WorldStateStore.save``.
    """

    def __init__(
        self,
        world_store_path: Path | str,
        *,
        observer_location_id: str,
        available_channels: Sequence[str],
        attention_budget: int,
    ) -> None:
        self.world_store_path = Path(world_store_path)
        self.observer_location_id = observer_location_id
        self.available_channels = available_channels
        self.attention_budget = attention_budget

    def read(self, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Return a bounded factual context, or ``None`` when unavailable."""

        try:
            state = WorldStateStore(self.world_store_path).load()
            if state is None:
                return None
            local_time = world_local_time(state, now)
            projection = project_perception(
                state,
                observer_location_id=self.observer_location_id,
                available_channels=self.available_channels,
                attention_budget=self.attention_budget,
            )
            result = {
                "schema_version": BRIDGE_SCHEMA_VERSION,
                "world_id": state["world_id"],
                "world_time": local_time.isoformat(timespec="seconds"),
                "location": {
                    "place_id": state["location"]["place_id"],
                    "area_id": state["location"]["area_id"],
                },
                "scene": {
                    "scene_id": state["scene"]["scene_id"],
                    "revision": state["scene"]["revision"],
                },
                "perceived_facts": projection["facts"],
            }
            if set(result) != _OUTPUT_FIELDS:
                return None
            return result
        except Exception:
            # A perception read is optional context.  It must not turn a
            # missing/corrupt world into a lifecycle or choice transition.
            return None

    def get_context(self, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Compatibility spelling for callers that name the result context."""

        return self.read(now=now)


def build_world_perception_context(
    world_store_path: Path | str,
    *,
    observer_location_id: str,
    available_channels: Sequence[str],
    attention_budget: int,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Build one read-only context from an explicitly injected World path."""

    return WorldPerceptionBridge(
        world_store_path,
        observer_location_id=observer_location_id,
        available_channels=available_channels,
        attention_budget=attention_budget,
    ).read(now=now)


__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "WorldPerceptionBridge",
    "build_world_perception_context",
]
