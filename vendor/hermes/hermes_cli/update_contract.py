"""Image-managed install refusal contract.

A refusal prints the real update command for the deployment kind, records a ``refused`` receipt (so
fleet tooling sees "this install cannot self-update, use <command>" instead of a silent non-update),
and exits 2 on CLI surfaces.
"""

from __future__ import annotations

import logging
import json
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateRefusal:
    """Why an in-place update is refused, and what to run instead."""

    code: str              # image-marker | image-marker-invalid | docker | nix | apt
    message: str           # full user-facing text (multi-line ok)
    update_command: str    # the one-line remediation command


def _refusal(code: str, method: str, message: Optional[Callable[[str], str]] = None) -> UpdateRefusal:
    """Refusal for ``method``: ``message(command)`` if given, else docker's full message / the bare command."""
    from hermes_cli.config import format_docker_update_message, recommended_update_command_for_method

    command = recommended_update_command_for_method(method)
    if message is not None:
        text = message(command)
    else:
        text = format_docker_update_message() if method == "docker" else command
    return UpdateRefusal(code=code, message=text, update_command=command)


def evaluate_update_admission(project_root: Path) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when in-place update must not run.

    ``None`` means the install is eligible for in-place update (git checkout or unknown-but-
    mutable). Never raises; on any internal error it falls back to the heuristic layer only.
    """
    # A code-scoped distribution owns its dependencies as a single release. This
    # is independent of personal config and must precede mutable-checkout heuristics.
    marker = Path(project_root) / '.distribution.json'
    try:
        observed = marker.lstat()
    except FileNotFoundError:
        observed = None
    except OSError:
        return UpdateRefusal('distribution-invalid', 'Distribution marker cannot be read; in-place update is disabled.', '')
    if observed is not None:
        try:
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError('marker must be a regular file')
            data = json.loads(marker.read_text(encoding='utf8'))
            if not isinstance(data, dict):
                raise ValueError('distribution marker must be an object')
            if type(data.get('schema')) is not int or data['schema'] != 1:
                raise ValueError('unsupported distribution schema')
            script = data['update_script']
            if not isinstance(script, str) or Path(script).is_absolute():
                raise ValueError('update script must be relative to the code root')
            target = (Path(project_root) / script).resolve(strict=True)
            if not target.is_file():
                raise ValueError('missing updater')
            command = shlex.join(['bash', str(target)])
            manager = data['manager']
            if not isinstance(manager, str) or not manager.strip():
                raise ValueError('missing distribution manager')
            return UpdateRefusal('distribution', f'This Hermes belongs to {manager}. Update the complete distribution:\n  {command}\nYour HERMES_HOME remains in use.', command)
        except (OSError, ValueError, KeyError, TypeError):
            return UpdateRefusal('distribution-invalid', 'Distribution marker is invalid; in-place Hermes update is disabled. Reinstall a complete distribution release.', '')

    # Layer 1: baked provenance marker — authoritative when present.
    try:
        from hermes_cli.image_provenance import read_image_provenance

        provenance = read_image_provenance()
        if provenance is not None:
            if not provenance.valid:
                # Present but malformed: still image-managed — an integrity defect is never
                # permission to mutate the image in place.
                return _refusal("image-marker-invalid", "docker", lambda command: (
                    "✗ This install is image-managed, but its provenance "
                    f"marker is invalid ({provenance.error}).\n"
                    "  In-place update is disabled. Update by pulling a "
                    f"new image:\n    {command}"
                ))
            return _refusal("image-marker", provenance.manager)
    except Exception as exc:
        logger.debug("Image provenance check failed (using heuristics): %s", exc)

    # Layer 2: pre-existing filesystem heuristics, verbatim semantics.
    try:
        from hermes_cli.config import detect_install_method, is_nix_install_method

        method = detect_install_method(project_root)
        if method == "docker":
            return _refusal("docker", method)
        if is_nix_install_method(method) or method == "apt":
            return _refusal(method if method == "apt" else "nix", method)
    except Exception as exc:
        logger.debug("Install-method admission check failed: %s", exc)
    return None


def record_refusal_receipt(refusal: UpdateRefusal) -> None:
    """Write a minimal ``refused`` receipt for a blocked update attempt.

    Gives fleet tooling a durable record that an update was ATTEMPTED and refused ("not updatable in
    place, use <command>") instead of a silent nothing. Best-effort; never raises.
    """
    try:
        from hermes_cli.update_receipt import begin_update_receipt, finalize_update_receipt, record_step

        begin_update_receipt()
        record_step("admission", False, f"not updatable in place ({refusal.code}); use: {refusal.update_command}")
        finalize_update_receipt("refused", stop_reason=refusal.code)
    except Exception as exc:
        logger.debug("Could not record refusal receipt: %s", exc)
