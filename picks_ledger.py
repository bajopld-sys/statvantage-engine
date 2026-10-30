"""Picks ledger — публичен track record за платения продукт.

Всеки "featured pick" (продуктова прогноза, не личен залог) се записва тук с
timestamp преди началото на мача (доказателство чрез публикуване + по-късно
git commit, аналогично на publish() в update_all.py). Проследяването е на
плоски единици (1 unit залог, коефициент-претеглена печалба/загуба) — не в
реални пари, за да не зависи от това в кой букмейкърски акаунт потребителят
реално играе.

Таблицата НЕ пази кой модел точно е дал сигнала (виж позиционирането — само
"Модел 1/2/3", не конкретни имена) — полето `source` е свободен текст за
вътрешна употреба, не се показва задължително публично.

Обем/видимост (решено 2026-08-02): 1 "сигурен" pick на първенство на седмица,
за платените абонати. Trial потребителите виждат само 1 pick общо (teaser) —
затова полето `visibility`: 'public' = вижда се без абонамент (trial примерът),
'subscriber' = само за платени. Не е отделен набор picks, а флаг върху едни и
същи записи в ledger-а.
"""
import re
import sqlite3
from pathlib import Path

from config import DATA_DIR
DB_PATH = DATA_DIR / "picks_ledger.db"

MARKETS = ("1X2", "OU", "BTTS")

_1X2_ALIASES = {
    "home": "HOME", "1": "HOME", "домакин": "HOME",
    "draw": "DRAW", "x": "DRAW", "равенство": "DRAW",
    "away": "AWAY", "2": "AWAY", "гост": "AWAY",
}
_BTTS_ALIASES = {
    "yes": "YES", "да": "YES",
    "no": "NO", "не": "NO",
}
_OU_RE = re.compile(r"(over|under|над|под)\s*([\d.]+)", re.IGNORECASE)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            published_at TEXT NOT NULL,
            league TEXT NOT NULL,
            home TEXT NOT NULL,
            away TEXT NOT NULL,
            match_date TEXT,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            odds REAL,
            stake_units REAL NOT NULL DEFAULT 1.0,
            visibility TEXT NOT NULL DEFAULT 'subscriber',
            status TEXT NOT NULL DEFAULT 'pending',
            result_score TEXT,
            resolved_at TEXT,
            source TEXT,
            note TEXT
        )
    """)
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(picks)")}
    if "visibility" not in existing_cols:
        con.execute("ALTER TABLE picks ADD COLUMN visibility TEXT NOT NULL DEFAULT 'subscriber'")
    con.commit()


def normalize_selection(market: str, selection: str) -> str:
    """Нормализира селекцията до каноничен вътрешен код, приема БГ/EN синоними."""
    s = selection.strip().lower()
    if market == "1X2":
        if s not in _1X2_ALIASES:
            raise ValueError(f"Непозната 1X2 селекция: {selection!r} (очаква Home/Draw/Away или 1/X/2)")
        return _1X2_ALIASES[s]
    if market == "BTTS":
        if s not in _BTTS_ALIASES:
            raise ValueError(f"Непозната BTTS селекция: {selection!r} (очаква Yes/No)")
        return _BTTS_ALIASES[s]
    if market == "OU":
        m = _OU_RE.match(s)
        if not m:
            raise ValueError(f"Непозната OU селекция: {selection!r} (очаква напр. 'Over2.5'/'Под2.5')")
        direction = "OVER" if m.group(1).lower() in ("over", "над") else "UNDER"
        return f"{direction}{float(m.group(2))}"
    raise ValueError(f"Непознат пазар: {market!r}")


def resolve_outcome(market: str, norm_selection: str, hg: int, ag: int) -> str:
    """Връща 'won' / 'lost' за завършен мач, дадени пълни резултатни голове."""
    if market == "1X2":
        actual = "HOME" if hg > ag else ("AWAY" if hg < ag else "DRAW")
        return "won" if norm_selection == actual else "lost"
    if market == "BTTS":
        both_scored = hg > 0 and ag > 0
        actual = "YES" if both_scored else "NO"
        return "won" if norm_selection == actual else "lost"
    if market == "OU":
        # norm_selection е "OVER2.5" / "UNDER2.5" — разцепи буквен префикс от числото
        direction, threshold = None, None
        for prefix in ("OVER", "UNDER"):
            if norm_selection.startswith(prefix):
                direction = prefix
                try:
                    threshold = float(norm_selection[len(prefix):])
                except ValueError:
                    threshold = None
                break
        # ПОПРАВКА (2026-09-04): при непознат формат threshold оставаше None и
        # сравнението `total > None` хвърляше TypeError по средата на
        # resolve_picks.py — а commit()-ът там е ЧАК след цикъла, т.е. вече
        # разплатените picks в същия run се губеха. Сега е ясна грешка тук.
        if direction is None or threshold is None:
            raise ValueError(f"Неразпознат OU формат: {norm_selection!r} "
                             f"(очаква напр. 'OVER2.5'/'UNDER3.0')")
        total = hg + ag
        # ПОПРАВКА (2026-09-04): цели линии (3.0) при точен резултат се връщат
        # (push), не се броят за загубени — преди 3 гола на линия 3.0 излизаше
        # "lost". Половинките (2.5) никога не дават push, там нищо не се мени.
        if total == threshold:
            return "push"
        over_hit = total > threshold
        return "won" if (direction == "OVER") == over_hit else "lost"
    raise ValueError(f"Непознат пазар: {market!r}")


def unit_result(status: str, odds: float | None, stake: float) -> float:
    """Печалба/загуба в единици за приключен pick."""
    if status == "won":
        if odds is None:
            return stake  # без коефициент — просто +1 единица номинално
        return stake * (odds - 1)
    if status == "lost":
        return -stake
    return 0.0  # push/void/pending — залогът се връща, нула нето
