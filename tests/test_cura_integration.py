"""Exercise the Cura-facing modules against hand-written stubs of Uranium/Cura/Qt.

This catches typos, wrong call order and threading mistakes in our own code.  It
does NOT prove the real Cura APIs behave like the stubs - that needs a real Cura.
"""

import os
import queue
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mock_printer import AUTH_CODE, MockPrinter  # noqa: E402


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class SignalDescriptor:
    def __init__(self, *types_):
        pass

    def __set_name__(self, owner, name):
        self.name = "_sig_" + name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.name not in obj.__dict__:
            obj.__dict__[self.name] = FakeSignal()
        return obj.__dict__[self.name]


def module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class FakePrefs:
    def __init__(self):
        self.values = {}

    def addPreference(self, key, default):
        self.values.setdefault(key, default)

    def getValue(self, key):
        return self.values.get(key)

    def setValue(self, key, value):
        self.values[key] = value


class FakeStack:
    def __init__(self, props):
        self.props = props
        self.extruderList = [self]

    def getProperty(self, key, _what):
        return self.props.get(key)


class FakeOutputDeviceManager:
    def __init__(self):
        self.devices = {}

    def addOutputDevice(self, device):
        self.devices[device.getId()] = device

    def removeOutputDevice(self, device_id):
        self.devices.pop(device_id, None)


class FakeApp:
    def __init__(self):
        self.prefs = FakePrefs()
        self.events = queue.Queue()
        self.output_devices = FakeOutputDeviceManager()
        self.stack = FakeStack({
            "machine_center_is_zero": True, "machine_width": 100, "machine_depth": 100, "layer_height": 0.2, "infill_sparse_density": 20,
            "wall_line_count": 2, "support_enable": False, "adhesion_type": "skirt",
            "material_print_temperature": 210, "material_diameter": 1.75,
        })
        scene = types.SimpleNamespace(gcode_dict={0: [open(os.path.join(HERE, "sample.gcode")).read()]})
        self.controller = types.SimpleNamespace(getScene=lambda: scene)
        self.scene = scene

    def getPreferences(self): return self.prefs
    def savePreferences(self): pass
    def functionEvent(self, event): self.events.put(event)
    def getGlobalContainerStack(self): return self.stack
    def getController(self): return self.controller
    def getMultiBuildPlateModel(self): return types.SimpleNamespace(activeBuildPlate=0)
    def getPrintInformation(self): return types.SimpleNamespace(jobName="Test Part")
    def getOutputDeviceManager(self): return self.output_devices

    def pump(self, until, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=0.02)
                event.func(*event.args, **event.kwargs)
            except queue.Empty:
                pass
            if until():
                return True
        return False


APP = FakeApp()
MESSAGES = []


class FakeMessage:
    def __init__(self, text="", title=None, lifetime=30, dismissable=True, progress=None, **kw):
        self.text, self.title, self.progress = text, title, progress
        self.actions = []
        self.actionTriggered = FakeSignal()
        self.hidden = False

    def addAction(self, action_id, name, icon, description, *a, **kw): self.actions.append(action_id)
    def show(self): MESSAGES.append(self)
    def hide(self): self.hidden = True
    def setProgress(self, value): self.progress = value
    def setText(self, text): self.text = text


class FakeOutputDevice:
    def __init__(self, device_id, **kw):
        self._id = device_id
        self.writeStarted, self.writeSuccess, self.writeError = FakeSignal(), FakeSignal(), FakeSignal()

    def getId(self): return self._id
    def setName(self, v): self.name = v
    def setShortDescription(self, v): pass
    def setDescription(self, v): pass
    def setIconName(self, v): pass
    def setPriority(self, v): pass


class FakeOutputDevicePlugin:
    def getOutputDeviceManager(self): return APP.output_devices


class FakeExtension:
    def setMenuName(self, name): self.menu = name
    def addMenuItem(self, name, cb): self.items = getattr(self, "items", []) + [name]


class _Logger:
    @staticmethod
    def log(*a, **k): pass

    @staticmethod
    def logException(*a, **k):
        import traceback
        traceback.print_exc()


class Application:
    @staticmethod
    def getInstance(): return APP


class CallFunctionEvent:
    def __init__(self, func, args, kwargs): self.func, self.args, self.kwargs = func, args, kwargs


class QObject:
    def __init__(self, parent=None): pass


def pyqtProperty(type_, notify=None):
    return lambda fn: property(fn)


def pyqtSlot(*a):
    return lambda fn: fn


module("UM")
module("UM.Application", Application=Application)
module("UM.Event", CallFunctionEvent=CallFunctionEvent)
module("UM.Logger", Logger=_Logger)
module("UM.Message", Message=FakeMessage)
module("UM.Extension", Extension=FakeExtension)
module("UM.Resources", Resources=types.SimpleNamespace(addSearchPath=lambda p: None))
module("UM.OutputDevice")
module("UM.OutputDevice.OutputDevice", OutputDevice=FakeOutputDevice)
module("UM.OutputDevice.OutputDevicePlugin", OutputDevicePlugin=FakeOutputDevicePlugin)
module("PyQt6")
module("PyQt6.QtCore", QObject=QObject, pyqtProperty=pyqtProperty, pyqtSignal=SignalDescriptor, pyqtSlot=pyqtSlot)

import MakerBotMiniPlugin as plugin  # noqa: E402
from MakerBotMiniPlugin import MakerBotMiniSettings as settings  # noqa: E402
from MakerBotMiniPlugin import makerbot_rpc  # noqa: E402


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        MESSAGES.clear()
        APP.prefs.values.clear()
        APP.output_devices.devices.clear()
        APP.stack.props.update({"machine_center_is_zero": True, "machine_width": 100, "machine_depth": 100})
        APP.scene.gcode_dict = {0: [open(os.path.join(HERE, "sample.gcode")).read()]}
        self.parts = plugin.register(APP)
        self.output_plugin = self.parts["output_device"]
        self.output_plugin.start()

    def test_register_returns_extension_and_output_device(self):
        self.assertEqual(set(self.parts), {"extension", "output_device"})
        self.assertEqual(self.parts["extension"].items, ["Connect to printer..."])

    def test_device_appears_only_after_pairing(self):
        self.assertEqual(APP.output_devices.devices, {})
        settings.save("192.168.1.50", "code")
        self.assertEqual(list(APP.output_devices.devices), ["makerbot_mini_192.168.1.50"])
        settings.clear()
        self.assertEqual(APP.output_devices.devices, {})

    def test_pairing_via_manager_then_print(self):
        with MockPrinter(polls_before_accept=2) as printer:
            real_pair, real_upload = makerbot_rpc.pair, makerbot_rpc.upload_and_print
            makerbot_rpc.pair = lambda host, user, **kw: real_pair(
                host, user, http_port=printer.http_port, poll_interval=0.01, **kw)
            makerbot_rpc.upload_and_print = lambda host, code, data, name, **kw: real_upload(
                host, code, data, name, http_port=printer.http_port, rpc_port=printer.rpc_port, **kw)
            try:
                manager = self.parts["extension"]._manager
                manager.setIp("127.0.0.1")
                manager.pair()
                self.assertTrue(APP.pump(lambda: settings.is_configured()))
                self.assertEqual(settings.get_auth_code(), AUTH_CODE)
                self.assertIn("Paired", manager.status)
                device = APP.output_devices.devices["makerbot_mini_127.0.0.1"]

                finished = []
                device.writeSuccess.connect(lambda d: finished.append("ok"))
                device.writeError.connect(lambda d: finished.append("error"))
                device.requestWrite([])

                confirm = MESSAGES[-1]
                self.assertEqual(confirm.actions, ["start", "cancel"])
                self.assertIn("build plate", confirm.text)
                self.assertEqual(printer.calls, [])  # nothing sent before confirmation
                confirm.actionTriggered.emit(confirm, "start")

                self.assertTrue(APP.pump(lambda: finished))
                self.assertEqual(finished, ["ok"])
                self.assertTrue(printer.crc_ok)
                self.assertEqual(printer.received[:2], b"PK")  # a zip archive
                self.assertTrue(any("print started" in (m.title or "") for m in MESSAGES))
            finally:
                makerbot_rpc.pair, makerbot_rpc.upload_and_print = real_pair, real_upload

    def test_cancel_sends_nothing(self):
        settings.save("127.0.0.1", "code")
        device = APP.output_devices.devices["makerbot_mini_127.0.0.1"]
        results = []
        device.writeError.connect(lambda d: results.append("error"))
        device.requestWrite([])
        confirm = MESSAGES[-1]
        confirm.actionTriggered.emit(confirm, "cancel")
        self.assertEqual(results, ["error"])
        self.assertTrue(confirm.hidden)

    def test_corner_origin_job_is_shifted_automatically(self):
        settings.save("127.0.0.1", "code")
        APP.stack.props["machine_center_is_zero"] = False
        APP.scene.gcode_dict = {0: [open(os.path.join(HERE, "cura_corner_origin.gcode")).read()]}
        APP.output_devices.devices["makerbot_mini_127.0.0.1"].requestWrite([])
        self.assertEqual(MESSAGES[-1].actions, ["start", "cancel"])  # converted fine, awaiting confirmation

    def test_refuses_wrong_bed_size(self):
        settings.save("127.0.0.1", "code")
        APP.stack.props.update({"machine_width": 252, "machine_depth": 199})
        APP.output_devices.devices["makerbot_mini_127.0.0.1"].requestWrite([])
        self.assertIn("Select the MakerBot Replicator Mini", MESSAGES[-1].text)
        self.assertEqual(MESSAGES[-1].actions, [])

    def test_refuses_unsliced_scene(self):
        settings.save("127.0.0.1", "code")
        APP.scene.gcode_dict = {}
        APP.output_devices.devices["makerbot_mini_127.0.0.1"].requestWrite([])
        self.assertIn("slice", MESSAGES[-1].text.lower())

    def test_refuses_out_of_bounds_gcode(self):
        settings.save("127.0.0.1", "code")
        APP.scene.gcode_dict = {0: ["M104 S200\nG1 F600 X90 Y0 E1\n"]}
        APP.output_devices.devices["makerbot_mini_127.0.0.1"].requestWrite([])
        self.assertIn("build volume", MESSAGES[-1].text)

    def test_connection_failure_is_reported(self):
        settings.save("127.0.0.1", "code")
        real_upload = makerbot_rpc.upload_and_print
        makerbot_rpc.upload_and_print = lambda *a, **kw: (_ for _ in ()).throw(
            makerbot_rpc.AuthError("Printer refused the stored credentials"))
        try:
            device = APP.output_devices.devices["makerbot_mini_127.0.0.1"]
            results = []
            device.writeError.connect(lambda d: results.append("error"))
            device.requestWrite([])
            confirm = MESSAGES[-1]
            confirm.actionTriggered.emit(confirm, "start")
            self.assertTrue(APP.pump(lambda: results))
            self.assertIn("pair again", MESSAGES[-1].text)
        finally:
            makerbot_rpc.upload_and_print = real_upload

    def test_manager_rejects_bad_host(self):
        manager = self.parts["extension"]._manager
        manager.setIp("http://evil.example/x")
        manager.pair()
        self.assertIn("not a valid", manager.status)


if __name__ == "__main__":
    unittest.main()
