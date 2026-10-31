"""Tests for the pure-Python parts: converter, protocol client against a mock printer."""

import io
import json
import os
import sys
import threading
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "MakerBotMiniPlugin"))
sys.path.insert(0, HERE)

import makerbot_file as mf  # noqa: E402
import makerbot_rpc as rpc  # noqa: E402
from mock_printer import AUTH_CODE, MockPrinter  # noqa: E402

SAMPLE = open(os.path.join(HERE, "sample.gcode")).read()


class ConverterTests(unittest.TestCase):
    def test_converts_sample(self):
        data, result = mf.gcode_to_makerbot(SAMPLE, mf.JobSettings())
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(sorted(z.namelist()),
                             ["meta.json", "print.jsontoolpath", "thumbnail_110x80.png",
                              "thumbnail_320x200.png", "thumbnail_55x40.png"])
            meta = json.loads(z.read("meta.json"))
            path = json.loads(z.read("print.jsontoolpath"))
            for name in z.namelist():
                if name.endswith(".png"):
                    self.assertEqual(z.read(name)[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(meta["extruder_temperature"], 210)
        self.assertEqual(meta["bot_type"], "mini_4")
        self.assertEqual(meta["material"], "pla")
        self.assertEqual(meta["duration_s"], 312)
        self.assertGreater(meta["commanded_duration_s"], 0)
        self.assertGreater(meta["extrusion_distance_mm"], 0)
        self.assertTrue(all("command" in c for c in path))
        funcs = [c["command"]["function"] for c in path]
        self.assertIn("toggle_fan", funcs)
        self.assertIn("fan_duty", funcs)
        self.assertEqual(path[0]["command"]["function"], "set_toolhead_temperature")
        self.assertEqual(path[0]["command"]["parameters"], {"index": 0, "temperature": 210})

    def test_toolpath_matches_reference_conventions(self):
        _, result = mf.gcode_to_makerbot(SAMPLE)
        cmds = [c["command"] for c in result.toolpath]
        self.assertEqual(cmds[0]["parameters"], {"index": 0, "temperature": 210})
        for c in cmds:
            if c["function"] in ("toggle_fan", "fan_duty"):
                self.assertEqual(c["parameters"]["index"], 0)
        moves = [c["parameters"] for c in cmds if c["function"] == "move"]
        # safe entry: lift to >= 1 mm over the start point, then lower at 23 mm/s
        self.assertEqual((moves[0]["z"], moves[0]["feedrate"]), (1.0, 50.0))
        self.assertEqual(moves[1]["feedrate"], 23.0)
        # Z never changes together with XY, and only at 23 mm/s
        for prev, cur in zip(moves, moves[1:]):
            if abs(cur["z"] - prev["z"]) > 1e-9:
                self.assertAlmostEqual(cur["x"], prev["x"])
                self.assertAlmostEqual(cur["y"], prev["y"])
                self.assertEqual(cur["feedrate"], 23.0)
        self.assertLessEqual(max(m["feedrate"] for m in moves), 150.0)
        self.assertEqual(cmds[-1]["function"], "comment")

    def test_start_gcode_before_layer0_is_not_sent(self):
        gcode = "M104 S200\nG28\nG1 Z15 F6000\nG1 F200 E3\n;LAYER:0\nG0 F3600 X5 Y5 Z0.3\nG1 F1200 X6 Y5 E0.1\n"
        _, result = mf.gcode_to_makerbot(gcode)
        zs = [c["command"]["parameters"]["z"] for c in result.toolpath if c["command"]["function"] == "move"]
        self.assertNotIn(15.0, zs)

    def test_corner_origin_is_shifted_and_real_job_converts(self):
        gcode = open(os.path.join(HERE, "cura_corner_origin.gcode")).read()
        _, result = mf.gcode_to_makerbot(gcode, mf.JobSettings(x_offset=-50, y_offset=-50))
        moves = [c["command"]["parameters"] for c in result.toolpath if c["command"]["function"] == "move"]
        self.assertTrue(all(abs(m["x"]) <= 17.81 and abs(m["y"]) <= 17.81 for m in moves))
        self.assertEqual(result.meta["extruder_temperature"], 200)
        self.assertEqual(result.meta["total_commands"], len(result.toolpath))
        self.assertEqual(result.meta["num_z_layers"], 198)
        # unshifted, the same job is wrongly placed outside the plate
        with self.assertRaises(mf.ConversionError):
            mf.convert(gcode, mf.JobSettings(x_offset=0, y_offset=0))
        # ...and full template keys from the bundled reference meta survive
        self.assertIn("machine_config", result.meta)
        self.assertEqual(result.meta["tool_type"], "mk13")

    def test_reference_meta_is_used_as_template(self):
        template = {"bot_type": "old", "grue_version": "x", "custom": "abc", "extra": {"k": 1}}
        _, result = mf.gcode_to_makerbot(SAMPLE, meta_template=template)
        self.assertEqual(result.meta["custom"], "abc")
        self.assertNotEqual(result.meta.get("uuid"), "abc")
        self.assertEqual(result.meta["extra"], {"k": 1})
        self.assertEqual(result.meta["bot_type"], "mini_4")
        self.assertFalse(any("template" in w for w in result.warnings))

    def test_load_reference_meta(self):
        data, _ = mf.gcode_to_makerbot(SAMPLE)
        path = os.path.join(HERE, "_ref.makerbot")
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            self.assertEqual(mf.load_reference_meta(path)["bot_type"], "mini_4")
        finally:
            os.remove(path)
        self.assertIsNone(mf.load_reference_meta(os.path.join(HERE, "missing.makerbot")))

    def test_absolute_extruder_survives_g92(self):
        _, result = mf.gcode_to_makerbot(SAMPLE, mf.JobSettings())
        moves = [c["command"]["parameters"] for c in result.toolpath if c["command"]["function"] == "move"]
        a_values = [0.0] + [m["a"] for m in moves]  # start from the printer's A = 0
        # A is the cumulative extruder position: G92 resets in the G-code must not
        # make it jump back to zero.  Hand-computed final value for sample.gcode:
        # (the pre-layer-0 retract is not sent) 0.8 + 0.8 + 0.8 - 0.5 + 0.5 + 1.0 + 0.4 = 3.8 - 0.5 = 3.0
        self.assertAlmostEqual(a_values[-1], 3.0, places=5)
        # The only decrease is the mid-print retraction (-0.5).
        drops = [(a, b) for a, b in zip(a_values, a_values[1:]) if b < a - 1e-9]
        self.assertEqual(len(drops), 1)

    def test_feedrate_converted_to_mm_per_second(self):
        _, result = mf.gcode_to_makerbot(SAMPLE, mf.JobSettings())
        first_print_move = [c["command"]["parameters"] for c in result.toolpath
                            if c["command"]["function"] == "move" and c["command"]["parameters"]["x"] == 10][0]
        self.assertAlmostEqual(first_print_move["feedrate"], 1200 / 60.0)

    def test_rejects_out_of_bounds(self):
        bad = SAMPLE + "G1 X80 Y0 E5\n"
        with self.assertRaises(mf.ConversionError) as ctx:
            mf.convert(bad)
        self.assertIn("build volume", str(ctx.exception))

    def test_rejects_overtemperature(self):
        with self.assertRaises(mf.ConversionError):
            mf.convert(SAMPLE.replace("S210", "S300"))

    def test_rejects_firmware_retraction_and_arcs(self):
        for bad in ("G10\n", "G2 X1 Y1 I1 J0\n"):
            with self.assertRaises(mf.ConversionError):
                mf.convert(SAMPLE + bad)

    def test_requires_temperature(self):
        no_temp = "\n".join(l for l in SAMPLE.splitlines() if "M104" not in l and "M109" not in l)
        with self.assertRaises(mf.ConversionError):
            mf.convert(no_temp)
        # ...unless a fallback is supplied
        self.assertEqual(mf.convert(no_temp, mf.JobSettings(temperature=205)).meta["extruder_temperature"], 205)

    def test_warns_on_mid_print_temperature_change(self):
        result = mf.convert(SAMPLE + "M104 S190\n")
        self.assertTrue(any("mid-print" in w for w in result.warnings))
        self.assertEqual(result.meta["extruder_temperature"], 210)

    def test_relative_modes(self):
        gcode = "M104 S200\nG91\nM83\nG1 F600 X5 Y5 E1\nG1 X5 Y0 E1\n"
        result = mf.convert(gcode, mf.JobSettings())
        last = [c["command"]["parameters"] for c in result.toolpath if c["command"]["function"] == "move"][-1]
        self.assertEqual((last["x"], last["y"], last["a"]), (10, 5, 2))

    def test_empty_gcode(self):
        with self.assertRaises(mf.ConversionError):
            mf.convert("M104 S200\n;nothing\n")


class ProtocolTests(unittest.TestCase):
    def test_pair_then_print(self):
        with MockPrinter(polls_before_accept=3) as printer:
            waiting = []
            code = rpc.pair("127.0.0.1", "CuraTest", http_port=printer.http_port,
                            poll_interval=0.01, on_waiting=lambda: waiting.append(1))
            self.assertEqual(code, AUTH_CODE)
            self.assertEqual(waiting, [1])
            self.assertEqual(printer.pairing_requested, "CuraTest")
            self.assertEqual(printer.answer_polls, 3)

            payload, _ = mf.gcode_to_makerbot(SAMPLE)
            payload = payload * 20  # force several 100 KB-ish blocks with a small block size
            seen = []
            info = rpc.upload_and_print("127.0.0.1", code, payload, "My Part (v2).makerbot",
                                        http_port=printer.http_port, rpc_port=printer.rpc_port,
                                        block_size=4096, progress=lambda f, t: seen.append(f))
            self.assertEqual(info["filename"], "My_Part_v2.makerbot")
            self.assertEqual(bytes(printer.received), payload)
            self.assertTrue(printer.crc_ok)
            methods = [m for m, _ in printer.calls]
            self.assertEqual(methods[:6], ["handshake", "authenticate", "print", "process_method",
                                           "put_init", "put_raw"])
            self.assertEqual(methods[-1], "put_term")
            self.assertEqual(seen[-1], 1.0)
            self.assertEqual(seen, sorted(seen))

    def test_pairing_rejected(self):
        with MockPrinter(polls_before_accept=1, reject=True) as printer:
            with self.assertRaises(rpc.AuthError):
                rpc.pair("127.0.0.1", http_port=printer.http_port, poll_interval=0.01)

    def test_pairing_timeout_and_cancel(self):
        with MockPrinter(polls_before_accept=10 ** 9) as printer:
            with self.assertRaises(rpc.AuthError) as ctx:
                rpc.pair("127.0.0.1", http_port=printer.http_port, poll_interval=0.01, timeout_s=0.1)
            self.assertIn("Timed out", str(ctx.exception))
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(rpc.AuthError) as ctx:
                rpc.pair("127.0.0.1", http_port=printer.http_port, poll_interval=0.01, cancel=cancel)
            self.assertIn("cancelled", str(ctx.exception))

    def test_bad_credentials(self):
        with MockPrinter() as printer:
            with self.assertRaises(rpc.AuthError):
                rpc.get_access_token("127.0.0.1", "wrong", http_port=printer.http_port)

    def test_busy_printer_error_is_reported(self):
        with MockPrinter(busy=True) as printer:
            with self.assertRaises(rpc.RpcError) as ctx:
                rpc.upload_and_print("127.0.0.1", AUTH_CODE, b"abc", "x", http_port=printer.http_port,
                                     rpc_port=printer.rpc_port)
            self.assertIn("busy", str(ctx.exception))

    def test_unreachable_printer(self):
        with self.assertRaises(rpc.MakerBotError):
            rpc.pair("127.0.0.1", http_port=1, http_timeout=1)

    def test_filename_sanitising(self):
        self.assertEqual(rpc.sanitize_filename(""), "cura_print.makerbot")
        self.assertEqual(rpc.sanitize_filename("../../etc/passwd"), "etc_passwd.makerbot")
        long_name = rpc.sanitize_filename("a" * 100)
        self.assertEqual(len(long_name), 40 + len(".makerbot"))
        self.assertTrue(long_name.endswith(".makerbot"))


if __name__ == "__main__":
    unittest.main()
