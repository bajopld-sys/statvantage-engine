"""Състави и контузени от FotMob като РЕЗЕРВА на Sofascore (2026-10-03, докато
api.sofascore.com е блокиран). Връща данни в ФОРМАТА на Sofascore
/event/{id}/lineups, за да не се променя нищо надолу по веригата
(lineup_impact.fetch_lineup_starters_detailed / fetch_missing_players_detailed):

    {"confirmed": bool,
     "home": {"players": [{"substitute": bool, "player": {"name", "id", "position"}}],
              "missingPlayers": [{"player": {"name", "position"}, "description": str}]},
     "away": {...}}

Куката е в lineup_impact.lineups_json(): ползва се САМО когато Sofascore не върне
нищо. Какво дава FotMob за предстоящ мач: списък НЕДОСТЪПНИ играчи (контузия/
наказание + очаквано връщане) още дни напред; стартови 11 — само близо до мача.

Защити:
  * играчовите ID-та са ОТРИЦАТЕЛНИ (-fotmob_id). Модел В v2 теглят атрибути по
    Sofascore ID; с FotMob ID биха взели ГРЕШЕН играч, когато Sofascore се
    върне. Отрицателно -> заявката просто не намира нищо (v2 остава без състав).
  * резултатът се кешира САМО в data/raw_fotmob/lineups/ — никога в ключа
    lineups_<event_id> на Sofascore кеша.
  * връзката Sofascore event_id -> FotMob matchId е по двойката отбори (домакин,
    гост) в предстоящите кръгове; строи се лениво и се пази в event_map.json.
"""
import datetime
import json
import sqlite3
import time
from pathlib import Path

import fotmob_adapter as fm
from config import DATA_DIR

MAP_PATH = fm.RAW / "event_map.json"
LINEUP_DIR = fm.RAW / "lineups"
MAP_TTL = 6 * 3600
POS = {0: "G", 1: "D", 2: "M", 3: "F"}
_map_cache: dict | None = None


def _league_dbs() -> dict[tuple[int, int], tuple[str, Path]]:
    return {fm.TOUR_SEASON[lg]: (lg, DATA_DIR / fm.LEAGUES[lg][1]) for lg in fm.TOUR_SEASON}


def build_event_map() -> dict[str, dict]:
    """{sofascore_event_id: {"fotmob_id", "league", "kickoff_ts"}} за предстоящите
    мачове, чрез двойката отбори (нашите канонични имена)."""
    from fetch_sofa import ALIASES  # noqa: F401  (нормализацията на имена е в canon)
    from parse_sofa_api import canon

    result = {}
    for (tour, season), (lg, dbp) in _league_dbs().items():
        if not dbp.exists():
            continue
        con = sqlite3.connect(dbp)
        ours = [r[0] for r in con.execute("SELECT home FROM matches UNION SELECT away FROM matches")]
        con.close()
        try:
            data = fm._get(f"https://www.fotmob.com/api/data/leagues?id={fm.LEAGUES[lg][0]}&ccode3=BGR",
                           fm.RAW / "leagues" / f"{fm.LEAGUES[lg][0]}.json", fm.LEAGUE_LIST_TTL)
        except fm.Blocked:
            continue
        up = [m for m in data["fixtures"]["allMatches"]
              if not m["status"].get("finished") and not m["status"].get("cancelled")]
        fm_names = sorted({m["home"]["name"] for m in up} | {m["away"]["name"] for m in up})
        nmap = fm.build_name_map(fm_names, ours)
        by_pair = {}
        for m in up:
            h, a = nmap.get(m["home"]["name"]), nmap.get(m["away"]["name"])
            if h and a:
                by_pair[(h, a)] = m

        # Sofascore кръговете за тази лига
        sofa_events = []
        for p in (DATA_DIR / "raw_sofa" / "rounds").glob(f"{tour}_{season}_r*.json"):
            try:
                sofa_events += json.loads(p.read_text(encoding="utf-8")).get("events", [])
            except Exception:
                pass
        sofa_names = sorted({canon(e["homeTeam"]["name"]) for e in sofa_events}
                            | {canon(e["awayTeam"]["name"]) for e in sofa_events})
        smap = fm.build_name_map(sofa_names, ours)
        for e in sofa_events:
            if e.get("_source") == "fotmob":
                continue
            h, a = smap.get(canon(e["homeTeam"]["name"])), smap.get(canon(e["awayTeam"]["name"]))
            m = by_pair.get((h, a))
            if m:
                ts = datetime.datetime.fromisoformat(m["status"]["utcTime"].replace("Z", "+00:00")).timestamp()
                result[str(e["id"])] = {"fotmob_id": int(m["id"]), "league": lg, "kickoff_ts": int(ts)}
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(json.dumps({"built": time.time(), "map": result}), encoding="utf-8")
    return result


def _event_map(event_id: int) -> dict | None:
    global _map_cache
    if _map_cache is None:
        if MAP_PATH.exists() and time.time() - MAP_PATH.stat().st_mtime < MAP_TTL:
            _map_cache = json.loads(MAP_PATH.read_text(encoding="utf-8")).get("map", {})
        else:
            try:
                _map_cache = build_event_map()
            except Exception as exc:
                print(f"⚠ fotmob event map: {str(exc)[:70]}")
                _map_cache = {}
    return _map_cache.get(str(event_id))


def _desc(u: dict) -> str:
    un = u.get("unavailability") or {}
    kind, back = (un.get("type") or "unknown"), un.get("expectedReturn")
    if kind == "suspension":
        return "suspension"
    return f"{kind.capitalize()} ({back})" if back else kind.capitalize()


def _shape(md: dict, kickoff_ts: int) -> dict:
    lu = (md.get("content") or {}).get("lineup") or {}
    out = {"confirmed": False}
    started = False
    for side, key in (("home", "homeTeam"), ("away", "awayTeam")):
        t = lu.get(key) or {}
        players = []
        for p in t.get("starters") or []:
            players.append({"substitute": False, "player": {
                "name": p.get("name"), "id": -int(p["id"]),
                "position": POS.get(p.get("usualPlayingPositionId"))}})
            started = True
        for p in t.get("subs") or []:
            players.append({"substitute": True, "player": {
                "name": p.get("name"), "id": -int(p["id"]),
                "position": POS.get(p.get("usualPlayingPositionId"))}})
        missing = [{"player": {"name": u.get("name"), "position": None}, "description": _desc(u)}
                   for u in t.get("unavailable") or [] if u.get("name")]
        out[side] = {"players": players, "missingPlayers": missing}
    # Sofascore потвърждава състава ~1ч преди мача; FotMob не казва — приемаме го така
    out["confirmed"] = started and (kickoff_ts - time.time()) < 75 * 60
    return out


def lineups_from_fotmob(event_id: int) -> dict | None:
    """Sofascore-подобен /lineups отговор за event_id или None."""
    info = _event_map(event_id)
    if not info:
        return None
    # далечни мачове: контузиите се менят бавно -> 6ч кеш; близки (<48ч) -> 30 мин
    ttl = 1800 if (info["kickoff_ts"] - time.time()) < 48 * 3600 else 6 * 3600
    try:
        md = fm._get(f"https://www.fotmob.com/api/data/matchDetails?matchId={info['fotmob_id']}",
                     LINEUP_DIR / f"{info['fotmob_id']}.json", ttl)
    except fm.Blocked as exc:
        print(f"⚠ fotmob състав {event_id}: {str(exc)[:60]}")
        return None
    return _shape(md, info["kickoff_ts"])
