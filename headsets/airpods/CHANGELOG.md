# Monorepo integration (2026-10-25)

- Include the latest local PulseAudio profile aliases and HFP default-input retries.
- Keep unit installation separate from explicit service activation; require a backup before replacing a different unit.
- Add isolated installer regression tests and root CI coverage.
- Exclude personal Bluetooth configuration, state, logs and unrelated working files.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.5] - 2026-10-18

### Fixed

- `pair --bredr` could match a cached LE / "Find My" device object that was
  never seen during the BR/EDR scan, then fail with "no Bluetooth device is
  known". The stale unpaired entry is now removed before the BR/EDR scan.


## [0.1.4] - 2026-10-18

### Added

- `airpodsctl pair --bredr` and `airpodsctl scan --bredr`. Forcing classic
  discovery avoids the old-BlueZ failure where AirPods are seen over the
  unconnectable LE bearer and pairing times out.

### Changed

- Pairing error messages now mention the AirPods 4 gesture and the BR/EDR /
  LE bearer workaround instead of only saying "authentication rejected".


## [0.1.3] - 2026-10-18

### Fixed

- AirPods whose name starts with a non-ASCII character (for example
  `俊翔的AirPods`) were skipped by `scan`/`pair`. Python's `\b` treats Chinese
  characters as word characters, so the old `\bairpods\b` regex did not match
  them. Detection is now a plain case-insensitive substring match.


## [0.1.2] - 2026-10-18

### Added

- AirPods 4 / AirPods 4 (ANC) model detection.
- `airpodsctl pair --address` now scans all Bluetooth devices when the address
  is not known yet, so it also works before the AirPods name has resolved.
- Scan output marks Apple devices and uses the default BlueZ discovery mode
  (BR/EDR + LE) instead of setting a transport filter.

### Fixed

- Documented the correct pairing gesture for AirPods 4: they have **no physical
  button**; double-tap the front of the case below the LED. All CLI hints,
  READMEs and troubleshooting docs now distinguish AirPods 4 from older models.


## [0.1.1] - 2026-10-18

### Fixed

- **Packaging on systems with an old system setuptools.** The pip build
  environment on Ubuntu 22.04 can shadow its downloaded setuptools with the
  system 59.6, which ignores `[project]` metadata and produces an `UNKNOWN`
  wheel with no console scripts. Package metadata now lives in `setup.cfg`,
  which both old and new setuptools understand.
- `install.sh` retries without build isolation when the isolated build cannot
  download its build dependencies, and verifies that `airpods` is importable.
- Removed a stale `UNKNOWN` distribution after a successful install.


## [0.1.0] - 2026-10-18

### Added

- `airpodsctl scan/pair/connect/disconnect/forget/status/battery/audio/doctor`
- A pairing agent with a constrained confirmation policy
- Auto-reconnect daemon (`airpodsd`) with systemd user unit
- GATT battery reading (best effort: per-cell when exposed, aggregate otherwise)
- A2DP/HFP profile switching through PulseAudio or PipeWire
- Low-battery desktop notifications
- `airpodsctl doctor` diagnostics for BlueZ, rfkill, controller mode,
  PulseAudio modules and A2DP endpoints
- `./install.sh --no-install` for venv/PEP 668 setups
- Mock backend (`--mock`) for demos and hardware-free tests
- English and Chinese documentation
