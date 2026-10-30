"""Central configuration: every filesystem location and secret the engine needs.

Values come from environment variables, optionally loaded from a ``.env`` file
next to this module (see ``.env.example``). Real environment variables always
win over the file. Nothing here is machine-specific: with no ``.env`` at all the
engine keeps everything inside its own folder.

    DATA_DIR                  SQLite databases, raw API caches, calibration CSVs
    OUTPUT_DIR                where the generated team_report.html / track_record.html go
    PUBLISH_DIR               a separate git checkout served by GitHub Pages
    VAULT_ROOT                optional: legacy Markdown match clippings (Bundesliga/Ligue1 import)
    FOOTBALL_DATA_RAW_DIR     optional: football-data.co.uk CSV dumps used by h2h.py
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional: top-picks alert
"""
import os
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE parser (comments, blank lines and quotes handled).
    Existing environment variables are never overwritten."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(ENGINE_DIR / ".env")


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


DATA_DIR = _path("DATA_DIR", ENGINE_DIR / "data")
OUTPUT_DIR = _path("OUTPUT_DIR", ENGINE_DIR / "output")
PUBLISH_DIR = _path("PUBLISH_DIR", ENGINE_DIR / "publish")
VAULT_ROOT = _path("VAULT_ROOT", ENGINE_DIR / "vault")
FOOTBALL_DATA_RAW_DIR = _path("FOOTBALL_DATA_RAW_DIR", DATA_DIR / "football_data_raw")

REPORT_PATH = OUTPUT_DIR / "team_report.html"
TRACK_RECORD_PATH = OUTPUT_DIR / "track_record.html"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
