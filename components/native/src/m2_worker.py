from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from app.m1 import M0EvidenceReader
from app.m2 import (
    M2_GENERATOR_VERSION,
    M1ReadOnlyStore,
    M2Store,
    M2Worker,
    DirectM2ProposalProvider,
)


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Chiyo Native M2 understanding shadow worker")
    parser.add_argument("--config", default="/etc/chiyo/m2-shadow.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("chiyo-native-m2-shadow")
    evidence = M0EvidenceReader(config["evidence_db"])
    episodes = M1ReadOnlyStore(config["episode_db"])
    store = M2Store(config["understanding_db"])
    proposer = DirectM2ProposalProvider(config)
    worker = M2Worker(
        evidence,
        episodes,
        store,
        proposer,
        generator_version=str(config.get("generator_version", M2_GENERATOR_VERSION)),
        logger=logger,
        max_episodes=int(config.get("max_episodes", 40)),
        enable_cross=bool(config.get("enable_cross", True)),
    )

    if args.once:
        result = worker.process_once()
        logger.info("m2.once.result=%s", result)
        return 0

    interval = max(0.5, float(config.get("poll_seconds", 5.0)))
    logger.info(
        "m2.worker.started generator_version=%s evidence_db=%s episode_db=%s understanding_db=%s",
        worker.generator_version,
        config["evidence_db"],
        config["episode_db"],
        config["understanding_db"],
    )
    while True:
        try:
            worker.process_once()
        except Exception as exc:
            logger.error("m2.worker.loop_failed error_class=%s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
