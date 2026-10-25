# Security policy

`airpodsd` runs as an unprivileged user and talks to BlueZ over the system
D-Bus. It does not need root, does not install udev rules, and does not open
raw Bluetooth sockets.

## Pairing agent

The CLI registers a BlueZ pairing agent only for the duration of a `pair` or
`connect` command. It auto-confirms pairing **only** for the device address
the user selected; every other request is rejected. The daemon does not
register a default agent, so it cannot silently accept a new device.

If you find a way to make the agent confirm a device other than the selected
one, please report it privately.

## Reporting a vulnerability

Open a private security advisory on GitHub (Security → Advisories → Report a
vulnerability). Please do not open a public issue for an exploitable problem.

## Scope

The project controls Bluetooth audio devices and reads their battery
services. It does not handle credentials, firmware updates, or Apple account
data.
