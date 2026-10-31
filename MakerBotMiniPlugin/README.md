# MakerBot Replicator Mini network printing for Cura

Slice in Cura, press **Print over network**, and the plugin converts the G-code to a
`.makerbot` file and sends it to the printer over TCP port 9999.

**Status: experimental and untested on real hardware.** It passes tests against a mock
printer and stubbed Cura APIs only. Watch the first print and keep a hand near the power switch.

## Install (Cura 5.x, Windows)
1. Cura: *Help > Show Configuration Folder*, open `plugins`.
2. Copy the `MakerBotMiniPlugin` folder there. Restart Cura.
3. (Optional) The `meta.json` layout of a real Mini job is bundled. To use your own instead, put a
   MakerBot Desktop file next to `__init__.py` as `reference.makerbot`.
4. Add the printer: *Add Printer > Add a non-networked printer > MakerBot > MakerBot Replicator Mini*.
   If it is missing, copy the two files under `resources/` into Cura's matching `definitions` and
   `extruders` folders and restart. Any 100x100 mm printer profile works too: Cura's corner-origin
   G-code is shifted to the bed centre automatically.
5. *Extensions > MakerBot Replicator Mini > Connect to printer...*, enter the IP, press **Pair**,
   and accept on the printer. (`pair_printer.py` does the same from a terminal.)
6. Slice, then use **Print over network**. Confirm the build plate is clear.

## Check the converter against your reference file
```
python inspect_makerbot.py reference.makerbot
python inspect_makerbot.py reference.makerbot --compare generated.makerbot
```
This shows the first commands, meta keys, and whether `feedrate` is mm/s
(sum of distance/feedrate should be close to `commanded_duration_s`).

## What is verified against a real MakerBot Desktop Mini job
feedrate in mm/s; absolute moves with cumulative `a`; `index: 0` on temperature/fan commands;
Z changes only as pure-Z moves at 23 mm/s; travel <= 150 mm/s; centred coordinates; meta.json fields.
Cura's start-up moves (homing, prime) are not sent: the toolpath starts at `;LAYER:0` after a
lift to 1 mm over the first point.

## Known unknowns
- Whether the original Mini accepts the pairing/upload sequence below (taken from Gen 5 documentation).
- Whether the printer homes/heats by itself before running the toolpath (the reference suggests yes).
- Limits: no mid-print temperature changes, arcs, firmware retraction; hot end capped at 240 C.

## Protocol used
HTTP `/auth` pairing (accept on printer) -> access token -> JSON-RPC on :9999:
`handshake`, `authenticate`, `print`, `process_method(build_plate_cleared)`, `put_init`,
`put_raw` blocks, `put_term` (CRC32).

Credits: protocol and format knowledge from tjhorner/makerbot-rpc, abdyfranco/makerbot-php,
seeburger/convert_makerbot and xuhao1/gcode2dotmakebot (GPL-2.0 - this plugin contains no copied code).
