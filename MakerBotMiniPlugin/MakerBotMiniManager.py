"""QObject exposed to the QML connect dialog: holds the printer IP, runs pairing and scanning."""

import threading

try:
    from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot
except ImportError:  # Cura 5.0 - 5.6 ship Qt 5
    from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from . import MakerBotMiniSettings as settings
from . import makerbot_rpc


class MakerBotMiniManager(QObject):
    ipChanged = pyqtSignal()
    statusChanged = pyqtSignal()
    busyChanged = pyqtSignal()
    pairedChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ip = settings.get_ip()
        self._busy = False
        self._cancel = threading.Event()
        self._status = ""
        settings.add_listener(self._onSettingsChanged)
        self._refreshStatus()

    # -- properties ---------------------------------------------------------
    @pyqtProperty(str, notify=ipChanged)
    def ip(self) -> str:
        return self._ip

    @pyqtProperty(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @pyqtProperty(bool, notify=pairedChanged)
    def paired(self) -> bool:
        return settings.is_configured()

    # -- slots called from QML ------------------------------------------------
    @pyqtSlot(str)
    def setIp(self, ip: str) -> None:
        ip = ip.strip()
        if ip != self._ip:
            self._ip = ip
            self.ipChanged.emit()

    @pyqtSlot()
    def pair(self) -> None:
        if self._busy:
            return
        try:
            host = makerbot_rpc.validate_host(self._ip)
        except makerbot_rpc.MakerBotError as err:
            self._setStatus(str(err))
            return
        self._cancel.clear()
        self._setBusy(True)
        self._setStatus("Contacting printer...")

        def on_waiting():
            settings.call_on_main(self._setStatus,
                                  "Now accept the connection on the printer (use its dial / touch "
                                  "screen). Waiting up to 2 minutes...")

        def worker():
            try:
                code = makerbot_rpc.pair(host, "Cura", on_waiting=on_waiting, cancel=self._cancel)
            except Exception as err:  # noqa: BLE001 - shown to the user
                settings.call_on_main(self._pairFailed, str(err))
            else:
                settings.call_on_main(self._pairSucceeded, host, code)

        threading.Thread(target=worker, name="MakerBotMiniPair", daemon=True).start()

    @pyqtSlot()
    def cancelPairing(self) -> None:
        self._cancel.set()

    @pyqtSlot()
    def scan(self) -> None:
        if self._busy:
            return
        self._setBusy(True)
        self._setStatus("Looking for printers on the network...")

        def worker():
            try:
                found = makerbot_rpc.discover(3.0)
            except Exception:  # noqa: BLE001
                found = []
            settings.call_on_main(self._scanDone, found)

        threading.Thread(target=worker, name="MakerBotMiniScan", daemon=True).start()

    @pyqtSlot()
    def forget(self) -> None:
        settings.clear()
        self._setStatus("Pairing removed.")

    # -- main-thread callbacks --------------------------------------------------
    def _pairSucceeded(self, host: str, code: str) -> None:
        self._setBusy(False)
        self._ip = host
        self.ipChanged.emit()
        settings.save(host, code)
        self._setStatus("Paired with %s. 'Print over network' is now available after slicing." % host)

    def _pairFailed(self, message: str) -> None:
        self._setBusy(False)
        self._setStatus(message)

    def _scanDone(self, found) -> None:
        self._setBusy(False)
        if not found:
            self._setStatus("No printer announced itself. Enter the IP address shown on the "
                            "printer's network settings screen.")
            return
        name, ip = found[0]
        self._ip = ip
        self.ipChanged.emit()
        extra = "" if len(found) == 1 else " (%d printers found; using the first)" % len(found)
        self._setStatus("Found %s at %s%s." % (name, ip, extra))

    def _onSettingsChanged(self) -> None:
        self.pairedChanged.emit()
        self._refreshStatus()

    # -- helpers ------------------------------------------------------------------
    def _refreshStatus(self) -> None:
        if settings.is_configured():
            self._setStatus("Paired with %s." % settings.get_ip())
        elif not self._status:
            self._setStatus("Not paired yet.")

    def _setStatus(self, text: str) -> None:
        self._status = text
        self.statusChanged.emit()

    def _setBusy(self, busy: bool) -> None:
        self._busy = busy
        self.busyChanged.emit()
