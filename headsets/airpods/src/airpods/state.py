"""Daemon state file shared with ``airpodsctl status`` and status bars."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import default_state_path

STATE_VERSION = 1


def write_state(data: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Atomically write *data* and return the path used."""
    target = Path(path) if path else default_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["version"] = STATE_VERSION
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)
    return target


def read_state(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    target = Path(path) if path else default_state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def clear_state(path: Optional[Path] = None) -> None:
    target = Path(path) if path else default_state_path()
    try:
        target.unlink()
    except OSError:
        # Missing or read-only: cleanup is best effort and must not mask a
        # shutdown path.
        pass
