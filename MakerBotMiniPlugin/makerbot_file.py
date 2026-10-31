"""G-code -> ``.makerbot`` converter for the original MakerBot Replicator Mini (``mini_4``).

Pure standard library: no Cura / Qt imports.

Format knowledge comes from a job produced by MakerBot Desktop (bundled meta.json template)
and community converters.  A ``.makerbot`` file is a ZIP containing ``meta.json``,
``print.jsontoolpath`` (JSON array of ``{"command": {...}}``) and three thumbnails.

Facts verified against the reference file:
  * all moves are absolute; ``a`` is the cumulative extruder position, starting at 0;
  * ``feedrate`` is in mm/s (sum of distance/feedrate matches ``commanded_duration_s``);
  * Z only ever changes in pure-Z moves at 23 mm/s; travel is capped at 150 mm/s;
  * temperature/fan commands carry ``"index": 0``.

The slicer coordinates are centred on the bed, so Cura's corner-origin G-code is shifted by
``x_offset`` / ``y_offset``.  Cura's own start-up moves (homing, prime, ...) are not sent:
the toolpath starts at the first layer, behind a safe lift move.
"""

import io
import json
import math
import os
import re
import struct
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import List, Optional

BUILD_X = 100.0
BUILD_Y = 100.0
BUILD_Z = 125.0
BOUNDS_TOLERANCE = 0.5
MAX_TEMPERATURE = 240.0

MAX_FEEDRATE = 150.0
Z_FEEDRATE = 23.0
ENTRY_FEEDRATE = 50.0
DEFAULT_FEEDRATE_MM_S = 25.0
DURATION_OVERHEAD = 1.9   # reference: duration_s / commanded_duration_s = 1.88

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources",
                              "mini_meta_template.json")


class ConversionError(Exception):
    """The G-code cannot be converted safely."""


@dataclass
class JobSettings:
    temperature: Optional[float] = None   # fallback if the G-code has no M104/M109
    filament_diameter: float = 1.77
    density: float = 1.24                 # g/cm^3, fitted to the reference file's mass
    x_offset: float = 0.0                 # added to G-code X (use -50 for Cura's corner origin)
    y_offset: float = 0.0
    entry_z: float = 1.0                  # safe height for the first move
    skip_start_gcode: bool = True         # only send moves from ';LAYER:0' on
    bot_type: str = "mini_4"
    grue_version: str = "5.8.1"
    material: str = "pla"


@dataclass
class ConversionResult:
    toolpath: list
    meta: dict
    warnings: List[str] = field(default_factory=list)


def default_meta_template() -> Optional[dict]:
    try:
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _command(function: str, parameters: dict, tags=None, metadata: Optional[dict] = None) -> dict:
    return {"command": {"function": function, "parameters": parameters,
                        "tags": list(tags or []), "metadata": metadata or {}}}


_ABS = {"relative": {"a": False, "x": False, "y": False, "z": False}}


def _move(x, y, z, a, feedrate, tags=None) -> dict:
    return _command("move", {"a": a, "feedrate": feedrate, "x": x, "y": y, "z": z}, tags, _ABS)


_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_KNOWN = {"G21", "G20", "G28", "G29", "G4", "G30", "M84", "M117", "M140", "M190", "M73", "M105",
          "M110", "M18", "M220", "M221", "M204", "M205", "M201", "M203", "M200", "M302", "M280",
          "M141", "M191", "M42", "M355", "M420", "M501", "M500"}


def _round(value: float) -> float:
    return round(value, 5)


def _cura_header(lines: List[str]) -> dict:
    info = {}
    for raw in lines[:60]:
        if not raw.startswith(";"):
            continue
        match = re.match(r";TIME:\s*(\d+)", raw)
        if match:
            info["time"] = int(match.group(1))
    return info


def _out_of_volume(p: dict) -> bool:
    return (abs(p["x"]) > BUILD_X / 2 + BOUNDS_TOLERANCE or abs(p["y"]) > BUILD_Y / 2 + BOUNDS_TOLERANCE
            or p["z"] > BUILD_Z + BOUNDS_TOLERANCE or p["z"] < -BOUNDS_TOLERANCE)


def convert(gcode: str, settings: Optional[JobSettings] = None,
            meta_template: Optional[dict] = None) -> ConversionResult:
    """``meta_template`` defaults to the bundled meta.json of a MakerBot Desktop job."""
    settings = settings or JobSettings()
    if meta_template is None:
        meta_template = default_meta_template()
    lines = gcode.splitlines()
    header = _cura_header(lines)
    warnings: List[str] = []
    unknown = set()

    has_layer0 = any(l.startswith(";LAYER:0") for l in lines)
    printing = not (settings.skip_start_gcode and has_layer0)

    toolpath: list = []
    logical = {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0}
    offset = {"x": settings.x_offset, "y": settings.y_offset, "z": 0.0, "e": 0.0}
    emitted = {"x": None, "y": None, "z": None, "e": 0.0}   # None until the first move is sent
    absolute_xyz = True
    absolute_e = True
    feedrate = None
    fan_on = False
    fan_duty = None
    temperature = None
    temperature_changed = False
    bed_heat = False
    travel_time = 0.0
    extruding_points = []
    entered = False

    def emit_move(x, y, z, e, feed, tags):
        nonlocal travel_time
        feed = _round(min(feed, MAX_FEEDRATE))
        p = {"x": _round(x), "y": _round(y), "z": _round(z)}
        if _out_of_volume(p):
            raise ConversionError(
                "Line %d moves outside the Replicator Mini's %gx%gx%g mm build volume "
                "(X%.2f Y%.2f Z%.2f). The model does not fit the plate, or the origin settings "
                "are wrong." % (line_no, BUILD_X, BUILD_Y, BUILD_Z, p["x"], p["y"], p["z"]))
        if emitted["x"] is not None and feed > 0:
            travel_time += math.dist((p["x"], p["y"], p["z"]),
                                     (emitted["x"], emitted["y"], emitted["z"])) / feed
        toolpath.append(_move(p["x"], p["y"], p["z"], _round(e), feed, tags))
        emitted.update(p)
        emitted["e"] = _round(e)

    line_no = 0
    for line_no, raw in enumerate(lines, start=1):
        if not printing and raw.startswith(";LAYER:0"):
            printing = True
        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        words = [(m.group(1).upper(), float(m.group(2))) for m in _WORD.finditer(code)]
        if not words:
            continue
        letter, number = words[0]
        args = {l: v for l, v in words[1:]}
        name = "%s%d" % (letter, int(number)) if number == int(number) else "%s%s" % (letter, number)

        if name in ("G0", "G1"):
            if "F" in args and args["F"] > 0:
                feedrate = args["F"] / 60.0
            target = dict(logical)
            for axis in ("x", "y", "z"):
                key = axis.upper()
                if key in args:
                    target[axis] = args[key] if absolute_xyz else logical[axis] + args[key]
            if "E" in args:
                target["e"] = args["E"] if absolute_e else logical["e"] + args["E"]
            moved = target != logical
            prev_logical, logical = logical, target
            if not moved or not printing:
                continue
            if feedrate is None:
                feedrate = DEFAULT_FEEDRATE_MM_S
                warnings.append("No feedrate before first move (line %d); using %g mm/s" %
                                (line_no, DEFAULT_FEEDRATE_MM_S))
            x, y, z = (target[a] + offset[a] for a in ("x", "y", "z"))
            e = target["e"] + offset["e"]
            if not entered:
                # Safe entry, like the reference job: lift, go over the start point, then lower.
                safe_z = max(settings.entry_z, z)
                emit_move(x, y, safe_z, emitted["e"], ENTRY_FEEDRATE, ["Retract"])
                emit_move(x, y, z, emitted["e"], Z_FEEDRATE, ["Travel Move"])
                entered = True
            dxy = math.hypot(x - emitted["x"], y - emitted["y"])
            dz = z - emitted["z"]
            if abs(dz) > 1e-9 and dxy > 1e-9:
                # The printer only ever sees pure-Z moves for Z changes.
                if dz > 0:
                    emit_move(emitted["x"], emitted["y"], z, emitted["e"], Z_FEEDRATE, ["Travel Move"])
                    dz = 0.0
            if dxy > 1e-9 or (abs(e - emitted["e"]) > 1e-9 and abs(dz) <= 1e-9):
                if dxy <= 1e-9:
                    feedrate = min(feedrate, ENTRY_FEEDRATE)   # extruder-only moves: reference max is 50
                extruding = e > emitted["e"] + 1e-9 and dxy > 1e-9
                if extruding:
                    tags = []
                elif e > emitted["e"] + 1e-9:
                    tags = ["Restart"]
                elif e < emitted["e"] - 1e-9 and dxy <= 1e-9:
                    tags = ["Retract"]
                else:
                    tags = ["Travel Move"]
                z_now = emitted["z"] if abs(dz) > 1e-9 else z
                emit_move(x, y, z_now, e, feedrate, tags)
                if extruding:
                    extruding_points.append(dict(emitted))
            if abs(dz) > 1e-9:
                emit_move(x, y, z, emitted["e"], Z_FEEDRATE, ["Travel Move"])

        elif name == "G92":
            for axis in ("x", "y", "z", "e"):
                key = axis.upper()
                if key in args:
                    logical[axis] = args[key]
                    if axis == "e":
                        offset["e"] = emitted["e"] - args[key]
                    elif emitted[axis] is not None:
                        offset[axis] = emitted[axis] - args[key]
            if not any(k in args for k in "XYZE"):
                for axis in ("x", "y", "z", "e"):
                    logical[axis] = 0.0
                offset["e"] = emitted["e"]

        elif name == "G90":
            absolute_xyz = True
        elif name == "G91":
            absolute_xyz = False
        elif name == "M82":
            absolute_e = True
        elif name == "M83":
            absolute_e = False

        elif name == "M106":
            if not printing:
                continue
            duty = min(max(args.get("S", 255.0) / 255.0, 0.0), 1.0)
            if not fan_on:
                toolpath.append(_command("toggle_fan", {"index": 0, "value": True}))
                fan_on = True
            if duty != fan_duty:
                toolpath.append(_command("fan_duty", {"index": 0, "value": _round(duty)}))
                fan_duty = duty
        elif name == "M107":
            if printing and fan_on:
                toolpath.append(_command("toggle_fan", {"index": 0, "value": False}))
                fan_on = False
                fan_duty = None

        elif name in ("M104", "M109"):
            if "S" in args and args["S"] > 0:
                if args["S"] > MAX_TEMPERATURE:
                    raise ConversionError("Line %d requests %g C, above the %g C safety limit." %
                                          (line_no, args["S"], MAX_TEMPERATURE))
                if temperature is None:
                    temperature = args["S"]
                elif abs(args["S"] - temperature) > 0.5:
                    temperature_changed = True
        elif name in ("M140", "M190"):
            bed_heat = bed_heat or args.get("S", 0) > 0

        elif name in ("G10", "G11"):
            raise ConversionError("Line %d uses firmware retraction (%s), which cannot be converted. "
                                  "Disable 'Firmware Retraction' in the printer settings." % (line_no, name))
        elif name in ("G2", "G3"):
            raise ConversionError("Line %d uses arc moves (%s), which cannot be converted." % (line_no, name))
        elif name not in _KNOWN:
            unknown.add(name)

    if not entered:
        raise ConversionError("The G-code contains no printable moves.")
    if fan_on:
        toolpath.append(_command("toggle_fan", {"index": 0, "value": False}))
    if temperature is None:
        temperature = settings.temperature
    if temperature is None:
        raise ConversionError("No hot-end temperature found in the G-code (M104/M109 with S>0).")
    if temperature_changed:
        warnings.append("The G-code changes hot-end temperature mid-print; the printer cannot do that, "
                        "so only the first temperature (%g C) is used." % temperature)
    if bed_heat:
        warnings.append("Bed temperature commands are ignored (the Mini has no heated bed).")
    if unknown:
        warnings.append("Ignored unsupported commands: %s" % ", ".join(sorted(unknown)))

    temp_int = int(round(temperature))
    toolpath.insert(0, _command("set_toolhead_temperature", {"index": 0, "temperature": temp_int}))
    toolpath.append(_command("comment", {"comment": "End of print"}))

    filament_mm = max(emitted["e"], 0.0)
    area = math.pi * (settings.filament_diameter / 2.0) ** 2
    mass_g = filament_mm * area / 1000.0 * settings.density
    zs = sorted({round(p["z"], 3) for p in extruding_points})
    box = {}
    if extruding_points:
        for axis in "xyz":
            values = [p[axis] for p in extruding_points]
            box[axis + "_min"] = min(values)
            box[axis + "_max"] = max(values)
    duration = header.get("time") or travel_time * DURATION_OVERHEAD

    meta = dict(meta_template) if meta_template else {}
    meta.update({
        "bot_type": settings.bot_type,
        "grue_version": settings.grue_version,
        "material": settings.material,
        "materials": [settings.material],
        "uuid": str(uuid.uuid4()),
        "duration_s": _round(duration),
        "commanded_duration_s": _round(travel_time),
        "extruder_temperature": temp_int,
        "extruder_temperatures": [temp_int],
        "extrusion_distance_mm": _round(filament_mm),
        "extrusion_distances_mm": [_round(filament_mm)],
        "extrusion_mass_g": _round(mass_g),
        "extrusion_masses_g": [_round(mass_g)],
        "total_commands": len(toolpath),
        "num_z_layers": len(zs),
        "num_z_transitions": max(len(zs) - 1, 0),
    })
    if box:
        meta["bounding_box"] = box
    if not meta_template:
        warnings.append("meta.json template missing: only the basic fields were written.")
    return ConversionResult(toolpath=toolpath, meta=meta, warnings=warnings)


# --------------------------------------------------------------------------- #
# Thumbnails and packaging
# --------------------------------------------------------------------------- #

THUMBNAIL_SIZES = {
    "thumbnail_55x40.png": (55, 40),
    "thumbnail_110x80.png": (110, 80),
    "thumbnail_320x200.png": (320, 200),
}


def placeholder_png(width: int, height: int) -> bytes:
    """A plain grey-blue PNG with a lighter border, generated without any imaging library."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            border = x < 2 or y < 2 or x >= width - 2 or y >= height - 2
            row.extend((170, 185, 200) if border else (225, 232, 240))
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def build_makerbot_file(result: ConversionResult, thumbnails: Optional[dict] = None) -> bytes:
    """Package a conversion result as the bytes of a ``.makerbot`` archive.

    ``thumbnails`` maps the three thumbnail file names to PNG bytes; any that are
    missing are replaced by generated placeholders.
    """
    thumbnails = thumbnails or {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("meta.json", json.dumps(result.meta))
        archive.writestr("print.jsontoolpath", json.dumps(result.toolpath))
        for name, (width, height) in THUMBNAIL_SIZES.items():
            archive.writestr(name, thumbnails.get(name) or placeholder_png(width, height))
    return buffer.getvalue()


def load_reference_meta(path: str) -> Optional[dict]:
    """meta.json from a genuine MakerBot Desktop file, or None if unavailable/unreadable."""
    try:
        with zipfile.ZipFile(path) as archive:
            meta = json.loads(archive.read("meta.json"))
        return meta if isinstance(meta, dict) else None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def gcode_to_makerbot(gcode: str, settings: Optional[JobSettings] = None,
                      thumbnails: Optional[dict] = None, meta_template: Optional[dict] = None):
    """Convenience wrapper: returns (makerbot_bytes, ConversionResult)."""
    result = convert(gcode, settings, meta_template)
    return build_makerbot_file(result, thumbnails), result
