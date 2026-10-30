"""Дневник на прогнозите на ВСИЧКИ модели (Dixon-Coles, Модел В v1/v2, Модел Б,
Модел Г) за всеки предстоящ мач — по искане на потребителя (2026-08-06): "след
като имаме мачовете от предстоящия кръг да записваш моделите какво прогнозират,
и след като мине кръга да сверяваш резултата с това което моделите са дали
преди мачовете", за лична анализа кой модел как се справя с времето.

ОТДЕЛЕН от picks_ledger.db (виж picks_ledger.py) - това там е кураторски
"featured pick" за платения продукт (1/седмица), тук е ПЪЛЕН snapshot на
всичко, което всеки модел казва за всеки предстоящ мач, независимо дали е
"добър" pick или не - за честна следа кой модел реално печели с времето.

Употреба:
    python engine/record_predictions.py    # снима текущия предстоящ кръг
    python engine/resolve_predictions.py   # сверява приключилите с реалността
"""
import sqlite3
from pathlib import Path

from config import DATA_DIR
DB_PATH = DATA_DIR / "model_predictions.db"

MODELS = ("dc", "v1", "v2", "model_b", "model_g", "opta_elo")
MODEL_LABELS = {
    "dc": "Dixon-Coles", "v1": "Модел В v1 (xg/xa)", "v2": "Модел В v2 (атрибути)",
    "model_b": "Модел Б (диагностично)", "model_g": "Модел Г (точков)",
    "opta_elo": "Opta-Elo (бета)",
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS model_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            league TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            home TEXT NOT NULL,
            away TEXT NOT NULL,
            match_date TEXT,
            model TEXT NOT NULL,
            p_home REAL,
            p_draw REAL,
            p_away REAL,
            pick_1x2 TEXT,
            p_over25 REAL,
            pick_over25 TEXT,
            p_btts REAL,
            pick_btts TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            actual_hg INTEGER,
            actual_ag INTEGER,
            hit_1x2 INTEGER,
            hit_over25 INTEGER,
            hit_btts INTEGER,
            resolved_at TEXT,
            UNIQUE(event_id, model)
        )
    """)
    con.commit()
