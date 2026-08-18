---
description: Dump full real BrewPi Arduino state (mode, setpoint, State, installed devices) safely, without racing the daemon's own serial connection.
---

Query the real BrewPi Arduino at `brewpi.local` and report its complete
current state. Follow these steps exactly, in order — do not skip the
daemon stop/restart even if it seems unnecessary; that's the part that
makes the read trustworthy.

## Why the daemon dance matters

`krauken-daemon` holds a persistent connection to the Arduino's serial
port (`/dev/ttyACM0`) whenever it's running. Opening a second, independent
connection to that same port *while the daemon is also reading it* is not
just risky in theory — it produced real, confirmed-garbled readings during
live troubleshooting on 2026-08-16 (`v`/`x` fields on an actuator device
showing impossible values like `3`, `4`, `5` instead of the only legal
values, `0`/`1`). Always get exclusive access first.

## Steps

1. Check whether the daemon is running:
   ```
   ssh doug@brewpi.local "systemctl is-active krauken-daemon"
   ```
2. If it printed `active`, stop it and remember to restart it at the very
   end, no matter what happens in between (even on error):
   ```
   ssh doug@brewpi.local "sudo systemctl stop krauken-daemon"
   ```
   If it printed `inactive` (or anything else), leave it alone — don't
   start a stopped daemon just to run this command, and don't restart it
   at the end either in that case.
3. Run this exact script on the Pi, via its own venv Python (has
   `pyserial` installed), over SSH — do not modify the protocol details,
   they're confirmed against the real firmware:

   ```python
   import serial, time, re, json

   LOG_FRAGMENT_RE = re.compile(r"D:\{[^{}]*\}")

   ser = serial.Serial("/dev/ttyACM0", 57600, timeout=0.2)
   time.sleep(1)
   ser.reset_input_buffer()

   def query(cmd, wait_s=8.0):
       ser.write(cmd)
       ser.flush()
       end = time.time() + wait_s
       buf = b""
       while time.time() < end:
           chunk = ser.read(256)
           if chunk:
               buf += chunk
       text = buf.decode(errors="replace")
       lines = []
       for raw_line in text.split("\n"):
           raw_line = raw_line.strip()
           if not raw_line:
               continue
           cleaned = LOG_FRAGMENT_RE.sub("", raw_line).strip()
           if cleaned:
               lines.append(cleaned)
       return lines

   for label, cmd in [
       ("t", b"t\n"),
       ("s", b"s\n"),
       ("d", b"d{r:1}\n"),
       ("h", b"h{u:-1,v:1}\n"),
   ]:
       print(f"=== {label} ===")
       for l in query(cmd):
           print(l)

   ser.close()
   ```

   Send this as a single inline `python3 -c "..."` (or write it to a temp
   file on the Pi first via a heredoc, then run it) over SSH — either is
   fine, match whatever's least fiddly to quote correctly.

4. Restart the daemon now if (and only if) you stopped it in step 2:
   ```
   ssh doug@brewpi.local "sudo systemctl start krauken-daemon"
   ```
   Do this even if step 3 errored partway through — never leave the
   daemon stopped as a side effect of this command.

## Decoding the raw output into a report

Don't just paste the raw JSON back — parse it and present a clean report:

**Mode** (from `s`'s `"mode"`): `o` → "idle/off", `f` → "fridge constant",
`b` → "beer constant", `p` → "beer profile".

**Setpoint**: `s`'s `fridgeSet`/`beerSet` (or "none" if `null`).

**State** (from `t`'s `"State"`, the raw firmware `TempControl.h` enum,
confirmed this session): `0` IDLE, `1` STATE_OFF, `2` DOOR_OPEN, `3`
HEATING, `4` COOLING, `5` WAITING_TO_COOL, `6` WAITING_TO_HEAT, `7`
WAITING_FOR_PEAK_DETECT, `8` COOLING_MIN_TIME, `9` HEATING_MIN_TIME.

**Chamber/beer temps**: `t`'s `FridgeTemp`/`BeerTemp`.

**Installed devices table** (from `d`, every entry — these all have a
real slot `"i" >= 0`): columns Slot (`i`), Pin or Address (`p` for a pin
device, `a` for a OneWire device), Hardware (decoded `h`), Role (decoded
`f`, the DeviceFunction), Polarity (`x`, pin devices only — omit the
column value for OneWire rows), Value (`v`), Notes.

**Available (uninstalled) candidates table** (from `h`, every entry —
these all have `"i": -1`): same columns minus Role (nothing is assigned
yet) and minus Slot.

**DeviceFunction decode** (confirmed this session, `platforms/brewpi/device_config.py`):
`0` NONE, `1` CHAMBER_DOOR, `2` CHAMBER_HEAT, `3` CHAMBER_COOL, `4`
CHAMBER_LIGHT, `5` CHAMBER_TEMP, `6` ROOM_TEMP, `7` FAN, `9` BEER_TEMP,
`10` BEER_TEMP2, `11` BEER_HEAT, `12` BEER_COOL, `13` BEER_SG.

**DeviceHardware decode**: `0` NONE, `1` PIN, `2` ONEWIRE_TEMP, `3`
ONEWIRE_2413.

**Notes column — flag, don't silently pass through**: for any pin-hardware
device, `v` and `x` are only ever legitimately `0` or `1`. If either is
anything else, call it out explicitly in the Notes column (e.g. "⚠
invalid — expected 0/1") rather than presenting it as a normal reading —
this is a confirmed-real anomaly seen on real hardware this session, not
a hypothetical edge case.

End the report by stating plainly whether the daemon was stopped/restarted
around the read, so it's clear the read was taken with exclusive access.
