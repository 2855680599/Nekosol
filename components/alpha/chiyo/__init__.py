"""CHIYO - a Persistent Digital Individual runtime (release candidate).

Three subsystems:

* **Life Runtime** - what she is currently engaged in (``chiyo.life_runtime``);
* **Action Reality** - what was actually executed and whether the result is
  proven; an ``UNKNOWN`` outcome stays unanswered (part of ``chiyo.life_runtime``);
* **Agency** - given candidates plus constraints, what she chooses
  (``chiyo.life_runtime`` + ``chiyo.contact``).

Plus the proactive **Contact pipeline** (``chiyo.contact``), which in this alpha
runs ISOLATED ONLY: no real Telegram send is ever possible.

The runtime is POSIX only (it uses ``fcntl``).  Run it on Linux or WSL.
"""

__version__ = "0.1.0a0"

__all__ = ["__version__"]
