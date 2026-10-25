"""Self-diagnostics: explain why AirPods cannot connect on this machine."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .audio import AudioController
from .backend import Backend
from .config import Config
from .constants import APP_NAME
from .errors import AirPodsError

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def bad(self) -> bool:
        return self.status == "fail"

    @property
    def warn(self) -> bool:
        return self.status == "warn"


@dataclass
class DoctorReport:
    checks: list[Check]

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status == "warn"]

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def _run(command: Sequence[str], timeout: float = 6.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, env=env, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def parse_controller_mode(text: str) -> Optional[str]:
    """Return the ``ControllerMode`` from a BlueZ ``main.conf``."""
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "General":
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "controllermode":
            return value.strip().lower()
    return None


def parse_rfkill(text: str) -> tuple[bool, bool]:
    """Return ``(soft_blocked, hard_blocked)`` from ``rfkill list`` output."""
    soft = bool(re.search(r"Soft blocked:\s*yes", text, re.IGNORECASE))
    hard = bool(re.search(r"Hard blocked:\s*yes", text, re.IGNORECASE))
    return soft, hard


def _check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 9):
        return Check("Python", "ok", version)
    return Check("Python", "fail", f"{version} is too old", "install Python 3.9 or newer")


def _check_bindings() -> Check:
    missing = [
        module for module in ("dbus", "gi") if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return Check("Python D-Bus bindings", "ok", "dbus-python and PyGObject are available")
    return Check(
        "Python D-Bus bindings",
        "fail",
        f"missing module(s): {', '.join(missing)}",
        "sudo apt install python3-dbus python3-gi",
    )


def _check_service() -> Check:
    if shutil.which("systemctl") is None:
        return Check("Bluetooth service", "skip", "systemctl not available")
    result = _run(["systemctl", "is-active", "bluetooth"])
    state = (result.stdout or result.stderr).strip().splitlines()
    value = state[0] if state else "unknown"
    if result.returncode == 0 and value == "active":
        return Check("Bluetooth service", "ok", "active")
    return Check(
        "Bluetooth service",
        "fail",
        f"not active ({value})",
        "sudo systemctl enable --now bluetooth",
    )


def _check_bluez_version() -> Check:
    binary = shutil.which("bluetoothd") or "/usr/libexec/bluetooth/bluetoothd"
    if not os.path.exists(binary):
        return Check("BlueZ", "skip", "bluetoothd binary not found")
    result = _run([binary, "--version"])
    output = (result.stdout or result.stderr).strip()
    match = re.search(r"(\d+)\.(\d+)", output)
    if not match:
        return Check("BlueZ", "skip", output or "unknown version")
    major, minor = int(match.group(1)), int(match.group(2))
    version = f"{major}.{minor}"
    if (major, minor) >= (5, 55):
        return Check("BlueZ", "ok", version)
    return Check(
        "BlueZ",
        "warn",
        f"{version} is old; AirPods work best with 5.55+",
        "upgrade BlueZ or your distribution",
    )


def _check_adapter(backend: Optional[Backend]) -> Check:
    if backend is None:
        return Check("Bluetooth adapter", "skip", "backend unavailable")
    try:
        adapter = backend.adapter()
    except AirPodsError as exc:
        return Check("Bluetooth adapter", "fail", exc.message, exc.hint or "")
    if not adapter.powered:
        return Check(
            "Bluetooth adapter",
            "fail",
            f"{adapter.id} is powered off",
            "bluetoothctl power on  (or enable Bluetooth in Settings)",
        )
    detail = f"{adapter.id} ({adapter.address})"
    if adapter.discovering:
        detail += ", discovering"
    return Check("Bluetooth adapter", "ok", detail)


def _check_rfkill() -> Check:
    if shutil.which("rfkill") is None:
        return Check("rfkill", "skip", "rfkill not installed")
    result = _run(["rfkill", "list", "bluetooth"])
    if result.returncode != 0:
        return Check("rfkill", "skip", (result.stderr or "").strip() or "no Bluetooth rfkill")
    soft, hard = parse_rfkill(result.stdout)
    if hard:
        return Check(
            "rfkill",
            "fail",
            "Bluetooth is hard-blocked",
            "turn on the wireless switch or check the BIOS",
        )
    if soft:
        return Check("rfkill", "fail", "Bluetooth is soft-blocked", "sudo rfkill unblock bluetooth")
    return Check("rfkill", "ok", "no blocks")


def _check_controller_mode() -> Check:
    path = Path("/etc/bluetooth/main.conf")
    if not path.exists():
        return Check("Controller mode", "skip", f"{path} not found")
    try:
        mode = parse_controller_mode(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return Check("Controller mode", "skip", "cannot read main.conf")
    if mode is None:
        return Check("Controller mode", "ok", "default (dual)")
    if mode == "le":
        return Check(
            "Controller mode",
            "fail",
            "ControllerMode = le disables Classic Bluetooth, which AirPods need",
            "set ControllerMode = dual in /etc/bluetooth/main.conf and restart bluetooth",
        )
    if mode in ("dual", "bredr"):
        return Check("Controller mode", "ok", mode)
    return Check("Controller mode", "skip", f"unknown value {mode!r}")


def _check_audio_server(audio: AudioController) -> Check:
    summary = audio.server_summary()
    if not summary:
        return Check(
            "Audio server",
            "fail",
            "pactl cannot reach PulseAudio/PipeWire",
            "start the audio server or log in to a desktop session",
        )
    return Check("Audio server", "ok", summary)


def _check_pulse_modules(audio: AudioController, summary: str) -> Check:
    if "pipewire" in summary.lower():
        return Check("Bluetooth audio modules", "skip", "PipeWire manages Bluetooth natively")
    try:
        modules = audio.module_names()
    except AirPodsError as exc:
        return Check("Bluetooth audio modules", "skip", exc.message)
    if not modules:
        return Check("Bluetooth audio modules", "skip", "cannot list PulseAudio modules")
    if "module-bluetooth-discover" not in modules:
        return Check(
            "Bluetooth audio modules",
            "fail",
            "module-bluetooth-discover is not loaded",
            "add `load-module module-bluetooth-discover` to /etc/pulse/default.pa "
            "and run `systemctl --user restart pulseaudio`",
        )
    if "module-bluetooth-policy" not in modules:
        return Check(
            "Bluetooth audio modules",
            "warn",
            "module-bluetooth-policy is not loaded",
            "add `load-module module-bluetooth-policy` for automatic A2DP/HFP switching",
        )
    return Check("Bluetooth audio modules", "ok", "discover and policy modules are loaded")


def _check_a2dp_endpoints() -> Check:
    if shutil.which("journalctl") is None:
        return Check("A2DP endpoints", "skip", "journalctl not available")
    result = _run(
        ["journalctl", "-u", "bluetooth", "-b", "--no-pager", "-n", "500"], timeout=8.0
    )
    if result.returncode != 0:
        return Check("A2DP endpoints", "skip", "cannot read the Bluetooth journal")
    text = result.stdout or ""
    if re.search(r"Endpoint registered:.*A2DPSink", text):
        return Check("A2DP endpoints", "ok", "the audio server registered an A2DP sink")
    return Check(
        "A2DP endpoints",
        "warn",
        "no A2DP sink endpoint found in the current boot's Bluetooth log",
        "restart the audio server, then `sudo systemctl restart bluetooth`",
    )


def _check_paired(backend: Optional[Backend]) -> Check:
    if backend is None:
        return Check("Paired AirPods", "skip", "backend unavailable")
    try:
        devices = [device for device in backend.devices() if device.is_airpods]
    except AirPodsError as exc:
        return Check("Paired AirPods", "skip", exc.message)
    paired = [device for device in devices if device.paired]
    if not paired:
        if devices:
            return Check(
                "Paired AirPods",
                "warn",
                f"found {len(devices)} AirPods device(s), but none are paired",
                "run `airpodsctl pair` to finish pairing",
            )
        return Check(
            "Paired AirPods",
            "warn",
            "no AirPods are paired yet",
            "put the AirPods in pairing mode and run `airpodsctl pair`",
        )
    connected = [device for device in paired if device.connected]
    return Check(
        "Paired AirPods", "ok", f"{len(paired)} paired, {len(connected)} connected"
    )


def _check_config(config: Config) -> Check:
    parent = config.path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("Configuration", "fail", f"cannot create {parent}: {exc}")
    if not os.access(parent, os.W_OK):
        return Check("Configuration", "fail", f"{parent} is not writable")
    return Check("Configuration", "ok", str(config.path))


def run_doctor(
    backend: Optional[Backend] = None,
    audio: Optional[AudioController] = None,
    config: Optional[Config] = None,
) -> DoctorReport:
    """Run every check and return a report (never raises for check failures)."""
    audio = audio or AudioController()
    config = config or Config.load()
    checks = [
        _check_python(),
        _check_bindings(),
        _check_service(),
        _check_bluez_version(),
        _check_adapter(backend),
        _check_rfkill(),
        _check_controller_mode(),
    ]
    audio_check = _check_audio_server(audio)
    checks.append(audio_check)
    if audio_check.status == "ok":
        checks.append(_check_pulse_modules(audio, audio_check.detail))
        checks.append(_check_a2dp_endpoints())
    else:
        checks.append(Check("Bluetooth audio modules", "skip", "audio server unavailable"))
        checks.append(Check("A2DP endpoints", "skip", "audio server unavailable"))
    checks.append(_check_paired(backend))
    checks.append(_check_config(config))
    return DoctorReport(checks)


def format_report(report: DoctorReport) -> str:
    symbols = {
        "ok": "[ OK ]",
        "warn": "[WARN]",
        "fail": "[FAIL]",
        "skip": "[SKIP]",
        "info": "[INFO]",
    }
    lines = [f"{APP_NAME} doctor", ""]
    for check in report.checks:
        label = symbols.get(check.status, "[????]")
        detail = f" — {check.detail}" if check.detail else ""
        lines.append(f"{label} {check.name}{detail}")
        if check.hint:
            lines.append(f"       → {check.hint}")
    failures = len(report.failures)
    warnings = len(report.warnings)
    summary = f"{failures} failure(s), {warnings} warning(s)"
    lines.extend(["", summary])
    if failures:
        lines.append("Fix the failures above, then run `airpodsctl doctor` again.")
    elif warnings:
        lines.append("Warnings do not necessarily prevent pairing; see docs/TROUBLESHOOTING.md.")
    else:
        lines.append("Everything looks good. Try `airpodsctl scan` or `airpodsctl pair`.")
    return "\n".join(lines)
