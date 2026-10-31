"""Stored connection settings (printer address + pairing code) and a main-thread helper."""

from UM.Application import Application
from UM.Event import CallFunctionEvent
from UM.Logger import Logger

PREF_IP = "makerbot_mini/ip"
PREF_AUTH = "makerbot_mini/auth_code"

_listeners = []


def register_preferences() -> None:
    preferences = Application.getInstance().getPreferences()
    preferences.addPreference(PREF_IP, "")
    preferences.addPreference(PREF_AUTH, "")


def _prefs():
    return Application.getInstance().getPreferences()


def get_ip() -> str:
    return str(_prefs().getValue(PREF_IP) or "")


def get_auth_code() -> str:
    return str(_prefs().getValue(PREF_AUTH) or "")


def is_configured() -> bool:
    return bool(get_ip() and get_auth_code())


def add_listener(callback) -> None:
    _listeners.append(callback)


def _notify() -> None:
    for callback in list(_listeners):
        try:
            callback()
        except Exception:
            Logger.logException("e", "MakerBot Mini: settings listener failed")


def _persist() -> None:
    try:
        Application.getInstance().savePreferences()
    except Exception:
        pass  # Cura also saves preferences on exit


def save(ip: str, auth_code: str) -> None:
    _prefs().setValue(PREF_IP, ip)
    _prefs().setValue(PREF_AUTH, auth_code)
    _persist()
    _notify()


def clear() -> None:
    _prefs().setValue(PREF_AUTH, "")
    _persist()
    _notify()


def call_on_main(func, *args, **kwargs) -> None:
    """Run ``func`` on Cura's main (UI) thread.  Safe to call from worker threads."""
    Application.getInstance().functionEvent(CallFunctionEvent(func, args, kwargs))
