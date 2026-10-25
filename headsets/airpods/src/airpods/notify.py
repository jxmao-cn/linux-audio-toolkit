"""Desktop notifications via ``notify-send`` (optional)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    """Best-effort freedesktop notifications.

    Missing ``notify-send`` (headless machines, minimal installs) is not an
    error; notifications are silently skipped.
    """

    def __init__(self, enabled: bool = True, binary: Optional[str] = None) -> None:
        self.enabled = enabled
        self._binary = binary if binary is not None else shutil.which("notify-send")

    def send(
        self,
        title: str,
        body: str = "",
        urgency: str = "normal",
        icon: str = "audio-headphones",
    ) -> bool:
        if not self.enabled or not self._binary:
            return False
        command = [
            self._binary,
            "--app-name=airpodsd",
            f"--icon={icon}",
            f"--urgency={urgency}",
            "--expire-time=5000",
            title,
            body,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("notify-send failed", exc_info=True)
            return False
        return result.returncode == 0
