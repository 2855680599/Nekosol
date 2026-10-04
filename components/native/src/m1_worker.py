from __future__ import annotations

import argparse
import logging
import time

from app.m1 import (
    M1_ALGORITHM_VERSION,
    EpisodeStore,
    EpisodeWorker,
    M0EvidenceReader,
    build_judge,
    load_m1_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Chiyo Native M1 shadow worker")
    parser.add_argument("--config", default="/etc/chiyo/m1-shadow.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    config = load_m1_config(args.config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("chiyo-native-m1-shadow")
    evidence = M0EvidenceReader(config["evidence_db"])
    store = EpisodeStore(
        config["episode_db"],
        str(config.get("algorithm_version", M1_ALGORITHM_VERSION)),
    )
    if args.rebuild:
        store.clear_derived()
        logger.info("m1.rebuild.cleared algorithm_version=%s", store.algorithm_version)
    judge = build_judge(config)
    worker = EpisodeWorker(evidence, store, judge, logger)
    if args.once:
        logger.info("m1.once.result=%s", worker.process_once())
        return 0

    interval = max(0.2, float(config.get("poll_seconds", 1.0)))
    logger.info(
        "m1.worker.started algorithm_version=%s evidence_db=%s episode_db=%s",
        store.algorithm_version,
        config["evidence_db"],
        config["episode_db"],
    )
    while True:
        try:
            worker.process_once()
        except Exception as exc:
            logger.error("m1.worker.loop_failed error_class=%s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
