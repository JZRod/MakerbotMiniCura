"""Registers the network output device once a printer has been paired."""

from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin

from . import MakerBotMiniSettings as settings
from .MakerBotMiniOutputDevice import MakerBotMiniOutputDevice


class MakerBotMiniOutputDevicePlugin(OutputDevicePlugin):
    def __init__(self):
        super().__init__()
        self._device = None
        settings.add_listener(self.refresh)

    def start(self) -> None:
        self.refresh()

    def stop(self) -> None:
        self._remove()

    def refresh(self) -> None:
        self._remove()
        if settings.is_configured():
            self._device = MakerBotMiniOutputDevice(settings.get_ip())
            self.getOutputDeviceManager().addOutputDevice(self._device)

    def _remove(self) -> None:
        if self._device is not None:
            self.getOutputDeviceManager().removeOutputDevice(self._device.getId())
            self._device = None
