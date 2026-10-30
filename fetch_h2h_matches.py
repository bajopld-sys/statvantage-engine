"""Реален мач-по-мач H2H (дати, резултати, турнир) -> h2h_matches в
sofa_<лига>.db — заменя/допълва h2h_summary (fetch_h2h.py), която дава само
W-D-L бройка. Потребителско искане (2026-07): "резултатът ще ми върши
работа, а не само WDL" — пробвахме tipster.bg (има раздел "Директни срещи",
но HTML скрейп + ново съпоставяне на кирилски имена), после открихме, че
Sofascore-то, което вече ползваме, дава директно същото и по-добре:

  /team/{id}/events/last/{page}   — пълна история на отбора (ВСИЧКИ
                                     турнири/сезони — лига, купа, суперкупа,
                                     приятелски), 30 мача на страница,
                                     с реален резултат и турнир. Потвърдено
                                     на ръка: Реал Мадрид срещу Барселона,
                                     11 срещи намерени за 6 страници назад
                                     (LaLiga, Copa del Rey, Supercopa,
                                     Club Friendly Games — не само лигата).

Съпоставянето е по Sofascore-ов числов team_id (от кешираните round JSON-и
на fetch_sofa.py), не по име — няма нужда от ALIASES/кирилица главоболия.

Употреба:
    python engine/fetch_h2h_matches.py --league LaLiga --db data/sofa_laliga.db
    python engine/fetch_h2h_matches.py --league EPL --db data/sofa.db --pages 8
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_sofa import Fetcher, RAW
from parse_sofa_api import canon

MAX_MEETINGS = 10


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS h2h_matches(
        team_a TEXT, team_b TEXT, event_id INTEGER, date TEXT,
        tournament TEXT, home TEXT, away TEXT, hg INTEGER, ag INTEGER,
        PRIMARY KEY(team_a, team_b, event_id))""")


def team_ids_from_rounds(raw: Path, tour: int, season: int,
                        rounds: range) -> dict[str, int]:
    """{канонично име: sofa team_id} — от вече кешираните round JSON-и
    (fetch_sofa.py); не тегли нищо ново."""
    ids: dict[str, int] = {}
    for rd in rounds:
        cache = raw / "rounds" / f"{tour}_{season}_r{rd}.json"
        if not cache.exists():
            continue
        data = json.loads(cache.read_text(encoding="utf-8"))
        for ev in data.get("events", []):
            ids[canon(ev["homeTeam"]["name"])] = ev["homeTeam"]["id"]
            ids[canon(ev["awayTeam"]["name"])] = ev["awayTeam"]["id"]
    return ids


def event_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_team_history(f: Fetcher, raw: Path, team_id: int,
                       max_pages: int) -> list[dict]:
    """Последните мачове на отбора, ВСИЧКИ турнири — пагинирано, кеширано
    на диск. Спира, ако страницата свърши (hasNextPage=False) или при
    max_pages."""
    events = []
    for page in range(max_pages):
        cache = raw / "teams" / str(team_id) / f"events_last_{page}.json"
        data = f.get(f"/team/{team_id}/events/last/{page}", cache)
        if not data:
            break
        for e in data.get("events", []):
            if e.get("status", {}).get("type") != "finished":
                continue
            events.append({
                "event_id": e["id"], "ts": e.get("startTimestamp", 0),
                "tournament": e.get("tournament", {}).get("name", ""),
                "home_id": e["homeTeam"]["id"], "away_id": e["awayTeam"]["id"],
                # ПОПРАВКА (2026-07): преди се пазеше суровото Sofascore име
                # (напр. "Liverpool FC", "Wolverhampton") вместо канонично
                # (canon()/ALIASES вече се прилага за team_ids по-горе в
                # този файл) — всяко точно сравнение с matches.home/away
                # тихо пропускаше алиасирани отбори.
                "home": canon(e["homeTeam"]["name"]), "away": canon(e["awayTeam"]["name"]),
                "hg": e.get("homeScore", {}).get("current"),
                "ag": e.get("awayScore", {}).get("current"),
            })
        if not data.get("hasNextPage"):
            break
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL")
    ap.add_argument("--db", required=True)
    ap.add_argument("--raw", default=str(RAW))
    ap.add_argument("--rounds", default="1-38",
                    help="кои кеширани round JSON-и да сканира за team_id-та")
    ap.add_argument("--year", default="25/26",
                    help="сезонна година (напр. '25/26' или '2026' за Eliteserien) — "
                         "само за да намери правилния кеш файл на кръговете")
    ap.add_argument("--pages", type=int, default=8,
                    help="максимум страници (по 30 мача) назад на отбор")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    from fetch_sofa import LEAGUES, resolve_season, parse_rounds
    key = args.league.upper()
    tour = int(args.league) if args.league.isdigit() else LEAGUES.get(key)
    if tour is None:
        sys.exit(f"Непозната лига '{args.league}'. Известни: {', '.join(LEAGUES)}")

    raw = Path(args.raw)
    f = Fetcher(delay=args.delay, refresh=args.refresh)
    season, season_name = resolve_season(f, tour, args.year)
    rounds = parse_rounds(args.rounds)

    team_ids = team_ids_from_rounds(raw, tour, season, rounds)
    if not team_ids:
        sys.exit("Няма кеширани кръгове за тази лига — пусни fetch_sofa.py първо.")
    print(f"Отбори с известен team_id: {len(team_ids)}")

    histories: dict[int, list[dict]] = {}
    for name, tid in team_ids.items():
        histories[tid] = fetch_team_history(f, raw, tid, args.pages)
        print(f"  {name}: {len(histories[tid])} мача в историята "
             f"({f.downloads} общо изтеглени досега)")

    con = sqlite3.connect(args.db)
    ensure_schema(con)

    pairs_done = matches_written = 0
    names = sorted(team_ids)
    for i, name_a in enumerate(names):
        id_a = team_ids[name_a]
        for name_b in names[i + 1:]:
            id_b = team_ids[name_b]
            meetings = [e for e in histories[id_a]
                       if e["home_id"] == id_b or e["away_id"] == id_b]
            meetings.sort(key=lambda e: e["ts"], reverse=True)
            meetings = meetings[:MAX_MEETINGS]
            if not meetings:
                continue
            pairs_done += 1
            for m in meetings:
                con.execute(
                    "INSERT OR REPLACE INTO h2h_matches VALUES (?,?,?,?,?,?,?,?,?)",
                    (name_a, name_b, m["event_id"], event_date(m["ts"]),
                     m["tournament"], m["home"], m["away"], m["hg"], m["ag"]))
                matches_written += 1

    con.commit()
    con.close()
    print(f"\nДвойки с намерена история: {pairs_done}/{len(names) * (len(names) - 1) // 2}")
    print(f"Записани мачове: {matches_written}")
    print(f"Заявки: {f.downloads} изтеглени, {f.hits} от кеша")
    if f.fails:
        print(f"Провалени: {len(f.fails)}")
        for x in f.fails[:10]:
            print("  !", x)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
