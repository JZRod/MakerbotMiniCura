"""Adds Extensions > MakerBot Replicator Mini > Connect to printer..."""

import os

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message

from .MakerBotMiniManager import MakerBotMiniManager

_QML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "qml", "ConnectDialog.qml")


class MakerBotMiniExtension(Extension):
    def __init__(self):
        super().__init__()
        self.setMenuName("MakerBot Replicator Mini")
        self.addMenuItem("Connect to printer...", self.showConnectDialog)
        self._manager = MakerBotMiniManager()
        self._dialog = None

    def showConnectDialog(self) -> None:
        if self._dialog is None:
            try:
                self._dialog = Application.getInstance().createQmlComponent(_QML, {"manager": self._manager})
            except Exception:
                Logger.logException("e", "MakerBot Mini: could not create the connect dialog")
                self._dialog = None
        if self._dialog is None:
            Message("The connect dialog could not be opened (see Cura's log for details). "
                    "You can pair from the command line instead: run pair_printer.py from the "
                    "plugin folder.", title="MakerBot Replicator Mini", lifetime=0).show()
            return
        self._dialog.show()
