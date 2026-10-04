"""Atomic JSON receipts with a private temporary file per writer."""
import json
import os
from pathlib import Path
import tempfile


def write_json(target, document):
    target = Path(target)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8',
                dir=target.parent, prefix=target.stem+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
