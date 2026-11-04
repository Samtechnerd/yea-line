"""Settings and call history for the MP50 UI, kept as small JSON files in the YeaLine folder under APPDATA (migrated from the old MP50Phone folder)."""
import json
import os

_BASE = os.environ.get("APPDATA", os.path.expanduser("~"))
DIR = os.path.join(_BASE, "YeaLine")
_OLD_DIR = os.path.join(_BASE, "MP50Phone")
SETTINGS = os.path.join(DIR, "settings.json")
HISTORY = os.path.join(DIR, "history.json")
MAX_HISTORY = 500


def _migrate():
    """First run under the new name: carry over settings and history from the old folder."""
    if os.path.isdir(DIR) or not os.path.isdir(_OLD_DIR):
        return
    try:
        import shutil
        shutil.copytree(_OLD_DIR, DIR)
    except OSError:
        pass


_migrate()


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only profile must never break the phone


def load_settings():
    d = _read(SETTINGS, {})
    return d if isinstance(d, dict) else {}


def save_settings(d):
    _write(SETTINGS, d)


def load_history():
    d = _read(HISTORY, [])
    return d if isinstance(d, list) else []


def add_history(entry):
    h = load_history()
    h.append(entry)
    _write(HISTORY, h[-MAX_HISTORY:])


def clear_history():
    _write(HISTORY, [])
