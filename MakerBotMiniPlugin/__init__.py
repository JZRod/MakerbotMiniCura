import os


def getMetaData():
    return {}


def register(app):
    # Imported lazily so that a problem in one component shows up in Cura's log
    # with a clear traceback instead of failing the whole package import.
    from . import MakerBotMiniSettings
    from .MakerBotMiniExtension import MakerBotMiniExtension
    from .MakerBotMiniOutputDevicePlugin import MakerBotMiniOutputDevicePlugin

    MakerBotMiniSettings.register_preferences()

    # Make the bundled printer definition available (best effort; see README for the
    # manual route if "MakerBot Replicator Mini" does not show up in "Add Printer").
    try:
        from UM.Resources import Resources
        Resources.addSearchPath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources"))
    except Exception:  # pragma: no cover - depends on Cura internals
        from UM.Logger import Logger
        Logger.logException("w", "MakerBot Mini: could not register bundled printer definition")

    return {
        "extension": MakerBotMiniExtension(),
        "output_device": MakerBotMiniOutputDevicePlugin(),
    }
