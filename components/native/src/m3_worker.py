from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from app.m3 import M3_AVAILABILITY_VERSION, M3_GENERATOR_VERSION, M3Store, M3Worker, SourceReader


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("M3 config must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Chiyo Native M3 Recall / Availability shadow worker")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if bool(config.get("backfill", False)):
        raise ValueError("M3 backfill is disabled; set a deployment-time initial user cursor")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("chiyo.m3")
    store = M3Store(config["m3_db"])
    initial_cursor = str(config["initial_user_event_id"])
    store.ensure_cursor(initial_cursor)
    reader = SourceReader(config["m0_db"], config["m1_db"], config["m2_db"], config.get("epoch_path"))
    worker = M3Worker(
        reader,
        store,
        generator_version=str(config.get("generator_version", M3_GENERATOR_VERSION)),
        availability_version=str(config.get("availability_version", M3_AVAILABILITY_VERSION)),
        max_candidates=int(config.get("max_candidates", 200)),
        logger=logger,
    )
    poll_seconds = max(1.0, float(config.get("poll_seconds", 5.0)))
    logger.info("m3.worker.started generator_version=%s availability_version=%s initial_cursor=%s", worker.generator_version, worker.availability_version, initial_cursor)
    while True:
        counts = worker.process_once(initial_cursor)
        if counts["evaluated"] or counts["duplicate"] or counts["failed_closed"]:
            logger.info("m3.worker.poll evaluated=%s duplicate=%s skipped_non_user=%s failed_closed=%s", counts["evaluated"], counts["duplicate"], counts["skipped_non_user"], counts["failed_closed"])
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
