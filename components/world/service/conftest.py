"""Path setup so `pytest service/` works from anywhere."""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / "scripts"):
    text = str(candidate)
    if text not in sys.path:
        sys.path.insert(0, text)
