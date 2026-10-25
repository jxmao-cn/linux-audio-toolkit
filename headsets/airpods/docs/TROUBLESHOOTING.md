# Troubleshooting

Start here:

```bash
airpodsctl doctor
```

The report tells you whether the problem is BlueZ, the audio server, rfkill, a
config issue or simply that no AirPods have been paired yet. The sections below
follow the same order.

## Quick triage

```bash
# Is the controller alive and powered?
bluetoothctl show

# Is the audio server alive?
pactl info

# Does PulseAudio see Bluetooth cards and modules?
pactl list cards short
pactl list modules short | grep bluetooth

# What does BlueZ think happened?
journalctl -u bluetooth -b --no-pager -n 100

# What does airpodsd think happened?
journalctl --user -u airpodsd -b --no-pager -n 100
```

## AirPods are not discovered

1. Both buds must be **inside the case**; open the lid.
2. Put them in pairing mode:
   - **AirPods 4 / 4 ANC have no physical button.** Double-tap the **front of the
     case, just below the LED**, until the light flashes **white**. If nothing
     happens, double-tap again; the case must be charged.
   - **AirPods 1–3, Pro, Max:** press and hold the **case button** until the light
     flashes **white**.
   A green flash means the case is charged; amber means it is charging.
3. Make sure they are not already connected to an iPhone or another computer.
   AirPods connect to one host at a time and stop advertising once connected.
   Turn Bluetooth off on the iPhone temporarily, or choose "Disconnect" in the
   iPhone's Bluetooth settings.
4. Run a longer, unfiltered scan:

   ```bash
   airpodsctl scan -t 30 --all
   ```

   AirPods should appear with a name like `AirPods Pro`. If they appear as a
   bare MAC address, the name has not resolved yet; scan again.
5. Check for rfkill blocks:

   ```bash
   rfkill list bluetooth
   sudo rfkill unblock bluetooth
   ```

6. If `ControllerMode = le` is set in `/etc/bluetooth/main.conf`, Classic
   Bluetooth (which A2DP needs) is disabled:

   ```ini
   [General]
   ControllerMode = dual
   ```

   Then `sudo systemctl restart bluetooth`.

## AirPods are listed but pairing fails on Ubuntu 22.04

Ubuntu 22.04 ships BlueZ 5.64 (2022). AirPods advertise over **BR/EDR** and also
advertise in an unconnectable **LE** mode with the same public address. Older
BlueZ versions sometimes choose the LE bearer for pairing, which fails. BlueZ
fixed this in 2024 with an `plugins/airpods.c` adapter plugin
([patch discussion](https://marc.info/?m=171216200112192&q=mbox)).

If `airpodsctl scan --all` sees the AirPods but `airpodsctl pair` fails with an
authentication or page timeout, first remove the half-paired entry and force
BR/EDR discovery from the tool itself:

```bash
airpodsctl forget -a AC:12:34:56:78:90
# put the AirPods back in pairing mode first
airpodsctl pair --bredr -a AC:12:34:56:78:90 -t 60
```

If BR/EDR discovery does not find them, use one of the controller-level
workarounds below.

### Temporarily force BR/EDR in main.conf

```bash
sudo cp /etc/bluetooth/main.conf /etc/bluetooth/main.conf.airpodsd-backup
sudoedit /etc/bluetooth/main.conf
```

Set (uncomment) under `[General]`:

```ini
ControllerMode = bredr
```

Then restart Bluetooth, pair, and restore the original file:

```bash
sudo systemctl restart bluetooth
airpodsctl pair
sudo mv /etc/bluetooth/main.conf.airpodsd-backup /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
```

`bredr` disables LE, so battery/GATT is unavailable until you restore `dual`.

### Or turn LE off temporarily

```bash
sudo btmgmt le off
airpodsctl pair
sudo btmgmt le on
```

Reboot or re-enable LE afterwards if the setting persists.

A newer BlueZ (one that includes the AirPods plugin) avoids the workaround.

## Pairing fails

Common D-Bus errors and what they mean:

| Error | Meaning | Fix |
|---|---|---|
| `org.bluez.Error.AuthenticationFailed` | AirPods rejected the link key or timed out | Remove the device and pair again |
| `org.bluez.Error.AuthenticationCanceled` | The pairing window closed | Put the buds back in pairing mode and retry (AirPods 4: double-tap the front of the case) |
| `org.bluez.Error.AlreadyExists` | Already paired | Continue with `airpodsctl connect` |
| `org.bluez.Error.Failed: Page Timeout` | Device asleep, out of range, or connected elsewhere | Take buds out of the case, move closer, disconnect from iPhone |
| `org.bluez.Error.InProgress` | Another pairing/connection is running | Wait for it to finish, then retry |

If a previous attempt left a half-paired entry:

```bash
airpodsctl forget          # or: bluetoothctl remove AC:12:34:56:78:90
airpodsctl pair
```

Another Bluetooth agent can interfere. If `airpodsctl` warns that another agent
is already the default, stop the other program (some desktop environments run
their own agent) or pair through the GNOME Bluetooth panel once, then use
`airpodsctl connect` afterwards.

## Connected, but there is no sound

```bash
airpodsctl status
airpodsctl audio a2dp
pactl list cards
pactl list sinks short
```

Check:

- The active profile is `a2dp-sink`, not `off` or `headset-head-unit`.
- A sink named `bluez_output.<MAC>.1` exists.
- The sink is not muted and its volume is not zero.
- The application is playing to that sink (not to HDMI or USB):

  ```bash
  pactl set-default-sink bluez_output.AC_12_34_56_78_90.1
  ```

If the card does not exist at all, the audio server has not seen the device:

```bash
pactl load-module module-bluetooth-discover
sudo systemctl restart bluetooth
```

On a fresh Ubuntu install, `module-bluetooth-discover` and
`module-bluetooth-policy` should already be loaded. If they are missing, add
them to `/etc/pulse/default.pa`:

```text
load-module module-bluetooth-policy
load-module module-bluetooth-discover
```

Then:

```bash
systemctl --user restart pulseaudio
```

## The microphone does not work

AirPods cannot play high-quality audio and use the microphone at the same time.
Switch the card to HFP:

```bash
airpodsctl audio hfp
```

Then select the AirPods microphone in your application or in the desktop sound
settings. Check that the source exists:

```bash
pactl list sources short | grep bluez
pactl set-default-source bluez_input.AC_12_34_56_78_90
```

To go back to music quality:

```bash
airpodsctl audio a2dp
```

Note that PulseAudio's `module-bluetooth-policy` may switch to HFP on its own
when an application starts recording. That is expected; use `airpodsctl audio
a2dp` to switch back.

## Battery is unavailable

`airpodsctl battery` needs the GATT battery service. If it fails:

1. Take the buds **out of the case** and wait a few seconds.
2. Make sure `airpodsctl status` says `connected`, not just `paired`.
3. Retry with verbose logging:

   ```bash
   airpodsctl --verbose battery
   ```

4. Check whether BlueZ exposes a battery object:

   ```bash
   busctl tree org.bluez | grep -i -A2 battery
   ```

Some firmware revisions only publish battery after the device has been
connected for a while. If you see three cells but the labels look wrong, set
`battery_labels` in `config.json` to the correct order.

## Audio is choppy or has high latency

- Move the computer and the AirPods closer; 2.4 GHz Wi-Fi and USB 3.0 hubs are
  common interferers.
- Try the XQ profile if your card offers it:

  ```bash
  pactl set-card-profile bluez_card.AC_12_34_56_78_90 a2dp-sink-sbc_xq
  ```

- Check for packet loss in the Bluetooth log:

  ```bash
  journalctl -u bluetooth -b --no-pager | grep -i -E "error|fail|timeout"
  ```

- USB Bluetooth dongles on a busy hub often perform worse than the built-in
  controller. Try another port.

## Auto-reconnect does not work

```bash
systemctl --user status airpodsd
journalctl --user -u airpodsd -b --no-pager -n 100
```

- The daemon must be running.
- The AirPods must be **paired** and **trusted**. `airpodsctl pair` sets both.
- AirPods only advertise when they are out of the case or in pairing mode. If
  they are in the case and asleep, no scan can find them; opening the lid wakes
  them.
- The daemon scans every `scan_interval` seconds (default 120). For testing, set
  it to `30` in `config.json` and restart the service.
- If the AirPods are still connected to an iPhone, they will not accept a
  connection from Linux. Disable "Automatically connect" on the iPhone.

Do not run two daemons at once; the second one will compete for scans and
connections. Check with:

```bash
pgrep -a airpodsd
```

## The iPhone keeps taking the AirPods back

AirPods prefer the last host and Apple devices reconnect aggressively. Options:

- On the iPhone: Settings → Bluetooth → AirPods → turn off "Automatically
  Connect" (naming varies by iOS version), or turn Bluetooth off while using
  Linux.
- On Linux: keep `airpodsd` running; it reconnects when the AirPods become
  available again.
- For a permanent Linux-only setup, "Forget This Device" on the iPhone. You can
  pair it again later.

## Collecting a useful bug report

```bash
airpodsctl doctor > doctor.txt 2>&1
airpodsctl --verbose status > status.txt 2>&1
pactl info > pactl.txt 2>&1
pactl list cards >> pactl.txt 2>&1
journalctl -u bluetooth -b --no-pager -n 200 > bluetooth.log 2>&1
```

Include your distribution, kernel, BlueZ and PulseAudio/PipeWire versions, and
the exact AirPods model. Redact MAC addresses if you prefer.

## Reset everything

```bash
systemctl --user stop airpodsd
airpodsctl forget              # falls back to bluetoothctl remove if needed
sudo systemctl restart bluetooth
systemctl --user restart pulseaudio    # or wireplumber
airpodsctl pair
```

If that fails, reboot with the AirPods in the case and the lid open. A full
power cycle of the case (close the lid, wait 10 seconds, open it) also helps.

### Resetting AirPods 4

AirPods 4 use a capacitive area rather than a button. With the buds in the case
and the lid open, double-tap the front below the LED: the first double-tap
enters pairing mode (white flash), a second one makes it flash faster, and a
third one flashes amber then white, which resets them. Then double-tap once
more to pair.

## Distribution notes

### Ubuntu 22.04 (PulseAudio 15.99)

Stock PulseAudio supports SBC and SBC-XQ for A2DP. AAC is not available, so
AirPods will use SBC. This is a codec limitation, not a connection problem.

### PipeWire

`pactl` works through `pipewire-pulse`. Card and profile names are the same.
Check the services:

```bash
systemctl --user status pipewire pipewire-pulse wireplumber
```

If `pactl info` reports `PulseAudio (on PipeWire ...)`, the doctor check for
PulseAudio modules is skipped automatically.

### Fedora / Arch

BlueZ and PipeWire are usually newer and may offer AAC. The commands are
identical; install `python3-dbus` / `python-gobject` and `pulseaudio-utils`
(or `libpulse`) using your package manager.
