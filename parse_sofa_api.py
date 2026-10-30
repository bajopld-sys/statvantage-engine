"""Sofascore API парсер → sofa.db, чете от кеша на fetch_sofa.py.

Заменя ръчното клипване: fetch_sofa.py вече е свалил суровия JSON в
data/raw_sofa/ (statistics.json / lineups.json на мач); този файл го
превръща в редове в sofa.db — същата схема, същите колони, същия
match_id формат като parse_sofa.py (MD-clipping версията), за да могат
двата парсера да пълнят една и съща база без конфликт. За лиги, за които
все още само клипваш на ръка (LaLiga/SerieA без fetch), parse_sofa.py си
остава активен.

Разлика spрямо parse_sofa.py:
  - Няма позиционно декодиране на редове — API-то дава етикет→стойност.
    XG_TOL/класификацията полувреме-срещу-цял-мач отпадат, защото периодът
    (ALL/1ST/2ND) идва вече верен от Sofascore.
  - Играчова статистика (lineups.json) — API дава 40+ полета на играч,
    вместо само име+рейтинг. Пише се в НОВА таблица `player_match`
    (виж ensure_player_schema). НЕ заменя `player_ratings` — goal_timeline.py
    още го чете за лигите без Опта база; не е нужно да се чупи.
    profiles_sofa.py/simulate_sofa.py умишлено остават на отборно ниво
    (виж техните докстрингове) — тази таблица не е свързана към модела,
    само е събрана и достъпна за бъдеща lineup версия на sofa модела.

Известни пропуски (честно, не скрито):
  - Няма odds_h/odds_d/odds_a — идваха от самата scrape-ната MD страница
    (блокът "Full-time 1 X 2"), което API-то никъде не носи. merge_match()
    ПАЗИ съществуващата стойност на мач, вече вкарана от parse_sofa.py,
    вместо да я трие с NULL — INSERT OR REPLACE тук би я изтрил тихо. За
    НОВИ мачове (извън наличния vault) тези колони излизат NULL — реална
    дупка, не бъг.
  - referee ИМА извор — 'event' endpoint (/event/{id}, добавен във
    fetch_sofa.py 2026-07-22), полето event.referee.name. Изисква отделен
    fetch (по-стар кеш без event.json няма да го даде — with_referee_missing
    в извода показва колко мача чакат повторен fetch_sofa.py).
  - Отсъстващ ключ при БРОЯЧНИ статистики (удари, отнемания, картони...) се
    приема за 0 — потвърдено на ръка (2026-07): "Red cards" липсва в мачове
    без червен картон. Рейтинг/xG/xA НЕ се дефолтват до 0 (виж
    PLAYER_NO_ZERO_DEFAULT) — липсващ рейтинг значи "без оценка", не "нула",
    и (2026-07, открито при добавяне на Allsvenskan) някои лиги нямат xG
    проследяване от Sofascore ИЗОБЩО (всеки играч, всеки мач) — 0-дефолт там
    би дал фалшива "средна 0.0" вместо коректно "няма данни", което чупи
    simulate_lineup_v1.py с деление на нула. За лиги С реално xG покритие
    (EPL/Eliteserien), "expectedGoals" липсва конкретно при играчи без нито
    един удар — NULL вместо 0 там означава такива мачове не влизат в
    роловата средна на играча, вместо да я дърпат надолу с честно 0.

Употреба:
    python engine/parse_sofa_api.py                       # EPL, целия кеш
    python engine/parse_sofa_api.py --rounds 30-38         # само нови кръгове
    python engine/parse_sofa_api.py --db data/sofa_bundesliga.db --raw data/raw_sofa_bundesliga
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_sofa import STAT_FIELDS, ensure_schema, upsert_stats
from fetch_sofa import ALIASES

from config import DATA_DIR
RAW = DATA_DIR / "raw_sofa"
SOFA_DB = DATA_DIR / "sofa.db"

PERIOD_MAP = {"ALL": "full", "1ST": "A", "2ND": "B"}

# label -> STAT_FIELDS колона; по няколко имена на поле, защото Sofascore е
# преименувал поне едно от тях по средата на сезона ("Expected goals (xG)"
# -> "Expected goals"). Стойностите извън тук пазят точно етикетите от
# parse_sofa.FIELD_MAP, за да значат едно и също нещо в двата парсера.
LABEL_ALIASES = {
    "possession": ["Ball possession"],
    "xg": ["Expected goals", "Expected goals (xG)"],
    "big_chances": ["Big chances"],
    "shots": ["Total shots"],
    "saves": ["Goalkeeper saves", "Total saves"],
    "corners": ["Corner kicks"],
    "fouls": ["Fouls"],
    "passes": ["Passes"],
    "tackles": ["Tackles"],
    "free_kicks": ["Free kicks"],
    "yellows": ["Yellow cards"],
    "reds": ["Red cards"],
    "sot": ["Shots on target"],
    "woodwork": ["Hit woodwork"],
    "shots_off": ["Shots off target"],
    "blocked": ["Blocked shots"],
    "inside_box": ["Shots inside box"],
    "outside_box": ["Shots outside box"],
    "big_scored": ["Big chances scored"],
    "big_missed": ["Big chances missed"],
    "through_balls": ["Through balls"],
    "fouled_final3": ["Fouled in final third"],
    "offsides": ["Offsides"],
}
assert set(LABEL_ALIASES) == set(STAT_FIELDS), \
    "LABEL_ALIASES трябва да покрива точно колоните на parse_sofa.STAT_FIELDS"

# играчова статистика: Sofascore ключ -> колона в player_match. Броячни
# полета дефолтват до 0 при липса (виж докстринга); рейтинг/xG/xA не.
PLAYER_FIELDS = {
    "minutesPlayed": "minutes", "rating": "rating", "goals": "goals",
    "goalAssist": "assists", "totalShots": "shots",
    "onTargetScoringAttempt": "sot", "expectedGoals": "xg",
    "expectedAssists": "xa", "keyPass": "key_passes",
    "touches": "touches", "totalPass": "passes",
    "accuratePass": "accurate_passes", "totalTackle": "tackles",
    "wonTackle": "tackles_won", "interceptionWon": "interceptions",
    "totalClearance": "clearances", "ballRecovery": "recoveries",
    "duelWon": "duels_won", "duelLost": "duels_lost",
    "aerialWon": "aerials_won", "aerialLost": "aerials_lost",
    "totalContest": "dribbles", "wonContest": "dribbles_won",
    "fouls": "fouls", "wasFouled": "was_fouled",
    "totalOffside": "offsides", "bigChanceCreated": "big_chances_created",
    "bigChanceMissed": "big_chances_missed", "totalCross": "crosses",
    "accurateCross": "accurate_crosses", "saves": "saves",
    "goalsPrevented": "goals_prevented",
    "possessionLostCtrl": "possession_lost",
}
# полета, за които липсващ ключ НЕ значи нула (виж докстринга)
PLAYER_NO_ZERO_DEFAULT = {"rating", "xg", "xa"}


def ensure_player_schema(con: sqlite3.Connection) -> None:
    cols = ", ".join(f"{c} REAL" for c in sorted(set(PLAYER_FIELDS.values())))
    con.execute(f"""CREATE TABLE IF NOT EXISTS player_match(
        match_id TEXT, team TEXT, sofa_player_id INTEGER, name TEXT,
        position TEXT, started INTEGER, captain INTEGER, {cols},
        PRIMARY KEY(match_id, team, sofa_player_id))""")
    cols_existing = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    for col in ("sofa_event_id", "home_ht", "away_ht"):
        if col not in cols_existing:
            typ = "INTEGER"
            con.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")
    con.execute("""CREATE TABLE IF NOT EXISTS goal_events(
        match_id TEXT, minute INTEGER, added_time INTEGER, is_home INTEGER,
        scorer TEXT, kind TEXT, home_score INTEGER, away_score INTEGER,
        PRIMARY KEY(match_id, minute, added_time, is_home, scorer))""")


def parse_incidents(inc_json: dict) -> tuple[tuple | None, tuple | None, list[dict]]:
    """(HT (h,a) или None, FT (h,a) или None, [голове по време]).

    HT/FT идват от Sofascore-овите СОБСТВЕНИ 'period' инциденти (text='HT'/
    'FT') с точния резултат в момента — не се смятат от нас. Всеки гол носи
    isHome = коя страна се КРЕДИТИРА (не отбора на играча) — Sofascore вече е
    решил автоголовете вместо нас: проверено на ръка (2026-07, Sunderland
    0:5 Nottingham Forest) — Trai Hume (Sunderland/домакин) вкарва автогол,
    isHome=False, awayScore расте — точно обратно на отбора му, коректно.
    """
    ht = ft = None
    goals = []
    for inc in inc_json.get("incidents", []):
        kind = inc.get("incidentType")
        if kind == "period":
            if inc.get("text") == "HT":
                ht = (inc.get("homeScore"), inc.get("awayScore"))
            elif inc.get("text") == "FT":
                ft = (inc.get("homeScore"), inc.get("awayScore"))
        elif kind == "goal":
            player = inc.get("player") or {}
            goals.append({
                "minute": inc.get("time"), "added_time": inc.get("addedTime"),
                "is_home": inc.get("isHome"), "scorer": player.get("name"),
                "kind": inc.get("incidentClass"),
                "home_score": inc.get("homeScore"), "away_score": inc.get("awayScore"),
            })
    return ht, ft, goals


def load_goal_events(con: sqlite3.Connection, match_id: str, goals: list[dict]) -> None:
    con.execute("DELETE FROM goal_events WHERE match_id = ?", (match_id,))
    for g in goals:
        con.execute(
            "INSERT OR REPLACE INTO goal_events "
            "(match_id, minute, added_time, is_home, scorer, kind, home_score, away_score) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (match_id, g["minute"], g["added_time"], int(bool(g["is_home"])),
             g["scorer"], g["kind"], g["home_score"], g["away_score"]))


def stat_value(stats_json: dict, period_api: str, *names: str) -> tuple:
    """(домакин, гост) за даден Sofascore период. Липсва → (None, None)."""
    wanted = set(names)
    for per in stats_json.get("statistics", []):
        if per.get("period") != period_api:
            continue
        for g in per.get("groups", []):
            for item in g.get("statisticsItems", []):
                if item.get("name") in wanted:
                    return item.get("homeValue"), item.get("awayValue")
    return (None, None)


def team_stats_for_period(stats_json: dict, period_api: str) -> dict:
    """{колона: (домакин, гост)} за период, готово за upsert_stats()."""
    out = {}
    for col, names in LABEL_ALIASES.items():
        h, a = stat_value(stats_json, period_api, *names)
        # липсващ брояч == 0 (потвърдено: "Red cards" отсъства точно когато
        # е 0-0); липсва цял период (мач без lineup статистика) се хваща
        # по-горе, преди тази функция изобщо да се извика.
        out[col] = (h if h is not None else 0, a if a is not None else 0)
    return out


def canon(name: str) -> str:
    return ALIASES.get(name, name)


def merge_match(con: sqlite3.Connection, match_id: str, date: str,
               round_: int, home: str, away: str, hg, ag,
               event_id: int, home_ht=None, away_ht=None, referee=None) -> None:
    """INSERT OR REPLACE би изтрил odds_h/odds_d/odds_a, вкарани от
    parse_sofa.py (тази API страна няма такива данни) — затова четем
    съществуващия ред и ги пренасяме, вместо да презаписваме с NULL. Същото
    за home_ht/away_ht/referee, когато този run е без incidents.json/event.json
    (напр. частичен fetch) — не искаме да изтрием вече засечена стойност
    с NULL. referee ИМА нов извор тук (event.json 'event' endpoint) — ако е
    подаден, замества старата стойност (свеж прочит, не просто fallback)."""
    row = con.execute(
        "SELECT odds_h, odds_d, odds_a, referee, home_ht, away_ht "
        "FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if row:
        odds_h, odds_d, odds_a, old_referee, old_ht, old_at = row
    else:
        odds_h = odds_d = odds_a = old_referee = old_ht = old_at = None
    if home_ht is None:
        home_ht = old_ht
    if away_ht is None:
        away_ht = old_at
    if referee is None:
        referee = old_referee
    con.execute(
        "INSERT OR REPLACE INTO matches "
        "(match_id, date, round, home, away, hg, ag, odds_h, odds_d, odds_a, "
        " referee, sofa_event_id, home_ht, away_ht) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, date, round_, home, away, hg, ag,
         odds_h, odds_d, odds_a, referee, event_id, home_ht, away_ht))


def load_players(con: sqlite3.Connection, match_id: str, team: str,
                 side: dict) -> int:
    inserted = 0
    for p in side.get("players", []):
        st = p.get("statistics") or {}
        if "minutesPlayed" not in st:
            continue          # неизползван резерв — не е играл, пропуска се
        vals = {}
        for src_key, col in PLAYER_FIELDS.items():
            if src_key in st:
                vals[col] = st[src_key]
            elif col not in PLAYER_NO_ZERO_DEFAULT:
                vals[col] = 0
            else:
                vals[col] = None
        cols = ", ".join(vals)
        ph = ", ".join("?" for _ in vals)
        con.execute(
            f"INSERT OR REPLACE INTO player_match "
            f"(match_id, team, sofa_player_id, name, position, started, "
            f" captain, {cols}) VALUES (?,?,?,?,?,?,?,{ph})",
            (match_id, team, p["player"]["id"], p["player"]["name"],
             p.get("position"), int(not p.get("substitute", False)),
             int(p.get("captain", False)), *vals.values()))
        inserted += 1
    return inserted


def parse_rounds(spec: str | None) -> set[int] | None:
    if not spec:
        return None
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL",
                    help="определя кой index_<ЛИГА>.json да чете (виж fetch_sofa.py)")
    ap.add_argument("--raw", default=str(RAW), help="папка на fetch_sofa.py кеша")
    ap.add_argument("--db", default=None,
                    help="по подразбиране: data/sofa_<лига>.db (отделен файл на лига, "
                         "за да не се пише случайно в общия sofa.db)")
    ap.add_argument("--rounds", help="'1-38' или '30,31' — по подразбиране всички в индекса")
    args = ap.parse_args()

    if args.db is None:
        args.db = str(DATA_DIR / f"sofa_{args.league.lower()}.db")

    raw = Path(args.raw)
    index_path = raw / f"index_{args.league.upper()}.json"
    if not index_path.exists():
        sys.exit(f"Няма {index_path} — първо пусни fetch_sofa.py --league {args.league}.")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    wanted_rounds = parse_rounds(args.rounds)

    con = sqlite3.connect(args.db)
    ensure_schema(con)
    ensure_player_schema(con)

    matches = players_total = skipped_unfinished = skipped_no_stats = 0
    with_incidents_missing = with_referee_missing = 0
    notes: list[str] = []

    for row in index:
        if wanted_rounds is not None and row["round"] not in wanted_rounds:
            continue
        if not row["finished"]:
            skipped_unfinished += 1
            continue
        if "statistics" not in row["got"]:
            skipped_no_stats += 1
            notes.append(f"{row['date']} {row['home']}–{row['away']}: "
                         f"без statistics.json (fetch неуспешен?)")
            continue

        home, away = canon(row["home"]), canon(row["away"])
        match_id = f"{row['date']}|{home}|{away}"
        event_dir = raw / "events" / str(row["event_id"])

        home_ht = away_ht = None
        inc_path = event_dir / "incidents.json"
        if inc_path.exists():
            inc_json = json.loads(inc_path.read_text(encoding="utf-8"))
            ht, ft, goals = parse_incidents(inc_json)
            if ht:
                home_ht, away_ht = ht
            if ft and (ft[0] != row["hg"] or ft[1] != row["ag"]):
                notes.append(f"{row['date']} {home}–{away}: FT от incidents "
                             f"{ft[0]}:{ft[1]} != statistics {row['hg']}:{row['ag']}")
            load_goal_events(con, match_id, goals)
        else:
            with_incidents_missing += 1

        referee = None
        ev_path = event_dir / "event.json"
        if ev_path.exists():
            ev_json = json.loads(ev_path.read_text(encoding="utf-8"))
            referee = (ev_json.get("event", {}).get("referee") or {}).get("name")
        else:
            with_referee_missing += 1

        stats = json.loads((event_dir / "statistics.json").read_text(encoding="utf-8"))
        merge_match(con, match_id, row["date"], row["round"], home, away,
                   row["hg"], row["ag"], row["event_id"], home_ht, away_ht, referee)
        for period_api, period_col in PERIOD_MAP.items():
            s = team_stats_for_period(stats, period_api)
            upsert_stats(con, match_id, home, away, period_col, s)
        matches += 1

        lu_path = event_dir / "lineups.json"
        if lu_path.exists():
            lu = json.loads(lu_path.read_text(encoding="utf-8"))
            players_total += load_players(con, match_id, home, lu.get("home", {}))
            players_total += load_players(con, match_id, away, lu.get("away", {}))

    con.commit()
    con.close()
    print(f"Мачове: {matches}   играчови записи: {players_total}")
    print(f"Пропуснати: {skipped_unfinished} незавършени, "
         f"{skipped_no_stats} без statistics.json, "
         f"{with_incidents_missing} без incidents.json (без HT/голове), "
         f"{with_referee_missing} без event.json (без съдия)")
    for n in notes[:20]:
        print("  !", n)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
