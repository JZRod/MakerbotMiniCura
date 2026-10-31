"""The "Print over network" button: converts the sliced G-code and sends it to the printer."""

import os
import threading

from UM.Application import Application
from UM.Logger import Logger
from UM.Message import Message
from UM.OutputDevice.OutputDevice import OutputDevice

from . import MakerBotMiniSettings as settings
from . import makerbot_file, makerbot_rpc

TITLE = "MakerBot Replicator Mini"


class MakerBotMiniOutputDevice(OutputDevice):
    def __init__(self, ip: str):
        super().__init__("makerbot_mini_%s" % ip)
        self._ip = ip
        self._busy = False
        self.setName("%s (%s)" % (TITLE, ip))
        self.setShortDescription("Print over network")
        self.setDescription("Print on %s over the network" % TITLE)
        self.setIconName("print")
        self.setPriority(3)

    # ------------------------------------------------------------------ #
    # Cura entry point
    # ------------------------------------------------------------------ #
    def requestWrite(self, nodes, file_name=None, limit_mimetypes=None, file_handler=None,
                     filter_by_machine=False, **kwargs):
        if self._busy:
            self._show("A job is already being sent to the printer.")
            return
        try:
            self._checkMachineSetup()
            gcode = self._collectGcode()
            job_settings = self._jobSettings()
            reference = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference.makerbot")
            result = makerbot_file.convert(gcode, job_settings,
                                           makerbot_file.load_reference_meta(reference))  # None -> bundled
            package = makerbot_file.build_makerbot_file(result, self._thumbnails())
        except makerbot_file.ConversionError as err:
            self._show(str(err), "Cannot print on the Replicator Mini")
            self._emit("writeError")
            return
        except Exception as err:
            Logger.logException("e", "MakerBot Mini: preparing the job failed")
            self._show("Preparing the print failed: %s" % err, "Cannot print on the Replicator Mini")
            self._emit("writeError")
            return

        filename = makerbot_rpc.sanitize_filename(self._jobName(file_name))
        self._confirm(package, filename, result.warnings)

    # ------------------------------------------------------------------ #
    # Preparing the job
    # ------------------------------------------------------------------ #
    def _checkMachineSetup(self) -> None:
        stack = Application.getInstance().getGlobalContainerStack()
        if stack is None:
            raise makerbot_file.ConversionError("No printer is selected in Cura.")
        width = float(stack.getProperty("machine_width", "value") or 0)
        depth = float(stack.getProperty("machine_depth", "value") or 0)
        if abs(width - makerbot_file.BUILD_X) > 1 or abs(depth - makerbot_file.BUILD_Y) > 1:
            raise makerbot_file.ConversionError(
                "The selected Cura printer has a %gx%g mm bed, but the Replicator Mini is %gx%g mm. "
                "Select the MakerBot Replicator Mini printer (or fix the bed size in Machine Settings)."
                % (width, depth, makerbot_file.BUILD_X, makerbot_file.BUILD_Y))

    def _originOffset(self):
        """Cura G-code is bed-corner based unless 'Origin at center' is set; the Mini is centred."""
        stack = Application.getInstance().getGlobalContainerStack()
        if stack.getProperty("machine_center_is_zero", "value"):
            return 0.0, 0.0
        return (-float(stack.getProperty("machine_width", "value")) / 2.0,
                -float(stack.getProperty("machine_depth", "value")) / 2.0)

    def _collectGcode(self) -> str:
        app = Application.getInstance()
        scene = app.getController().getScene()
        gcode_dict = getattr(scene, "gcode_dict", None)
        if not gcode_dict:
            raise makerbot_file.ConversionError("Nothing has been sliced yet. Slice the model first.")
        plate = app.getMultiBuildPlateModel().activeBuildPlate
        gcode_list = gcode_dict.get(plate)
        if not gcode_list:
            raise makerbot_file.ConversionError("The active build plate has not been sliced yet.")
        return "".join(gcode_list)

    def _jobName(self, file_name) -> str:
        if file_name:
            return str(file_name)
        try:
            return Application.getInstance().getPrintInformation().jobName
        except Exception:
            return "cura_print"

    def _jobSettings(self) -> makerbot_file.JobSettings:
        stack = Application.getInstance().getGlobalContainerStack()
        try:
            extruder = stack.extruderList[0]
        except Exception:
            extruder = stack

        def prop(container, key, default):
            try:
                value = container.getProperty(key, "value")
            except Exception:
                return default
            return default if value is None else value

        x_offset, y_offset = self._originOffset()
        return makerbot_file.JobSettings(
            temperature=float(prop(extruder, "material_print_temperature", 215)),
            filament_diameter=float(prop(extruder, "material_diameter", 1.77)),
            x_offset=x_offset,
            y_offset=y_offset,
        )

    def _thumbnails(self) -> dict:
        """Render the three preview images; falls back to placeholders on any problem."""
        images = {}
        try:
            from cura.Snapshot import Snapshot
            try:
                from PyQt6.QtCore import QBuffer, QIODevice
                write_only = QIODevice.OpenModeFlag.WriteOnly
            except ImportError:
                from PyQt5.QtCore import QBuffer, QIODevice
                write_only = QIODevice.WriteOnly
            for name, (width, height) in makerbot_file.THUMBNAIL_SIZES.items():
                if hasattr(Snapshot, "isometricSnapshot"):
                    image = Snapshot.isometricSnapshot(width, height)
                else:
                    image = Snapshot.snapshot(width=width, height=height)
                if image is None:
                    continue
                buffer = QBuffer()
                buffer.open(write_only)
                image.save(buffer, "PNG")
                images[name] = bytes(buffer.data())
        except Exception:
            Logger.logException("w", "MakerBot Mini: could not render thumbnails; using placeholders")
        return images

    # ------------------------------------------------------------------ #
    # Confirmation and sending
    # ------------------------------------------------------------------ #
    def _confirm(self, package: bytes, filename: str, warnings) -> None:
        text = ("Make sure the build plate is empty and clean - the print starts as soon as "
                "the file has been sent.")
        if warnings:
            text += "\n\nNotes:\n- " + "\n- ".join(warnings)
        message = Message(text, title="%s: ready to send %s" % (TITLE, filename),
                          lifetime=0, dismissable=False)
        message.addAction("start", "Start print", "", "")
        message.addAction("cancel", "Cancel", "", "")

        def on_action(msg, action_id):
            msg.hide()
            if action_id == "start":
                self._send(package, filename)
            else:
                self._emit("writeError")

        message.actionTriggered.connect(on_action)
        message.show()

    def _send(self, package: bytes, filename: str) -> None:
        ip = self._ip
        auth_code = settings.get_auth_code()
        progress = Message("Connecting to the printer...", title="%s: sending %s" % (TITLE, filename),
                           progress=0, lifetime=0, dismissable=False, use_inactivity_timer=False)
        progress.show()
        self._busy = True
        self._emit("writeStarted")

        def on_progress(fraction, text):
            settings.call_on_main(self._updateProgress, progress, fraction, text)

        def worker():
            try:
                makerbot_rpc.upload_and_print(ip, auth_code, package, filename, progress=on_progress)
            except Exception as err:  # noqa: BLE001 - report everything to the user
                Logger.logException("e", "MakerBot Mini: sending the job failed")
                settings.call_on_main(self._finished, progress, err)
            else:
                settings.call_on_main(self._finished, progress, None)

        threading.Thread(target=worker, name="MakerBotMiniUpload", daemon=True).start()

    # -- main-thread callbacks ---------------------------------------------
    def _updateProgress(self, message, fraction, text) -> None:
        message.setProgress(int(fraction * 100))
        message.setText(text)

    def _finished(self, progress_message, error) -> None:
        self._busy = False
        progress_message.hide()
        if error is None:
            self._show("The job was sent and the printer should start heating. Check the printer "
                       "screen to confirm.", "%s: print started" % TITLE)
            self._emit("writeSuccess")
            return
        hint = ""
        if isinstance(error, makerbot_rpc.AuthError):
            hint = "\n\nOpen Extensions > MakerBot Replicator Mini > Connect to printer and pair again."
        self._show("%s%s" % (error, hint), "%s: sending failed" % TITLE)
        self._emit("writeError")

    # -- helpers --------------------------------------------------------------
    def _show(self, text: str, title: str = TITLE) -> None:
        Message(text, title=title, lifetime=0).show()

    def _emit(self, signal_name: str) -> None:
        signal = getattr(self, signal_name, None)
        if signal is not None:
            try:
                signal.emit(self)
            except Exception:
                Logger.logException("w", "MakerBot Mini: could not emit %s" % signal_name)
