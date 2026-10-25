"""airpodsd -- a userspace AirPods driver for Linux.

The package is intentionally importable without ``dbus-python`` installed:
the real BlueZ backend imports its D-Bus dependencies lazily, so the CLI can
still run ``--mock`` mode and the test suite on machines without a system bus.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.5"
