# Contributing to airpodsd

Thanks for helping make AirPods work better on Linux. Bug reports from real
hardware are especially valuable because the maintainers cannot test every
AirPods generation.

## Development setup

Requirements: Python 3.9+, `python3-dbus`, `python3-gi`, BlueZ and
PulseAudio or PipeWire. No third-party Python packages are needed to run the
tool.

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit/headsets/airpods

# Run the tests without installing anything:
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v

# Or install in editable mode:
python3 -m pip install --user -e .
```

The test suite uses the mock backend and needs neither Bluetooth hardware nor
an audio server.

## Trying changes without AirPods

Every read-only command works with a simulated pair of AirPods:

```bash
PYTHONPATH=src python3 -m airpods --mock status
PYTHONPATH=src python3 -m airpods --mock battery
PYTHONPATH=src python3 -m airpods --mock doctor
```

## What to test on real hardware

Please mention your AirPods model and distribution when reporting results:

- first-time pairing (`airpodsctl pair`)
- reconnect after the AirPods were connected to an iPhone
- `airpodsctl battery` with one, two and three cells
- switching between `airpodsctl audio a2dp` and `airpodsctl audio hfp`
- battery and auto-reconnect behaviour of `airpodsd`

## Code style

- Python 3.9 compatible; use `from __future__ import annotations`.
- Keep the runtime dependency-free; `dbus-python` is imported lazily.
- Run `make test` and `make lint` before opening a pull request.
- Formatting is enforced with Ruff (line length 100).
- Prefer small, focused functions and add a unit test for parsing or state
  machine changes.

## Commit messages

Short imperative subject lines, for example:

```
battery: fall back to org.bluez.Battery1
pair: wait for ServicesResolved before reading battery
```

## Pull requests

- Describe the user-visible change and the hardware you tested with.
- Include the output of `airpodsctl doctor` if the change affects pairing.
- Update `README.md` / `docs/TROUBLESHOOTING.md` when behaviour changes.
- Add an entry to `CHANGELOG.md` under `Unreleased`.

By contributing you agree that your changes are licensed under the MIT
license.
