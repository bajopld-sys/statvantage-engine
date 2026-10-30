"""Sofascore H2H (последни <=10 срещи, ВСИЧКИ турнири) -> h2h_summary в
sofa_<лига>.db.

/event/{id}/h2h дава само СБОРНА бройка (home/away/draws), НЕ списък мачове
с дати/резултати -- единственото налично през това API (проверени на ръка,
2026-07: /h2h/events, /h2h/tournament, /team/{a}/h2h/{b} -- всички 404).

Изисква СЪЩЕСТВУВАЩ event_id между двата отбора — все едно изигран или не.
Потвърдено на ръка (2026-07): заявка към НЕИЗИГРАН бъдещ мач (Arsenal–
Coventry, сезон 26/27, "notstarted") връща нормална H2H история — Sofascore
явно смята историята независимо от статуса на закотвящия мач. Затова тук
се минава ЦЯЛОТО сезонно разписание (само леката "кой играе с кого и кога"
информация от /events/round/{n} — НЕ пълни statistics/lineups/incidents за
бъдещите кръгове), за да има event_id за ВСЯКА двойка в лигата, дори такива,
които още не са играли тази година (напр. LaLiga, само 11 от 38 кръга
изтеглени с пълни данни). Предпочита се последната ИЗИГРАНА среща, ако има
такава — бъдещ fixture е само fallback, когато двойката още не се е играла.

Само за лиги, познати на fetch_sofa.py (LEAGUES речника) — Bundesliga/
Ligue1 все още не са минали оттам, остават без тази таблица;
make_team_report.py пада грациозно на старото поведение.

Употреба:
    python engine/fetch_h2h.py --league EPL --db data/sofa.db
    python engine/fetch_h2h.py --league LaLiga --db data/sofa_laliga.db --rounds 1-38
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_sofa import Fetcher, RAW, LEAGUES, resolve_season, parse_rounds, event_date
from parse_sofa_api import canon


def ensure_h2h_schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS h2h_summary(
        team_a TEXT, team_b TEXT, wins_a INTEGER, wins_b INTEGER,
        draws INTEGER, total INTEGER, as_of_event INTEGER, as_of_date TEXT,
        PRIMARY KEY(team_a, team_b))""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(h2h_summary)")]
    if "anchor_finished" not in cols:
        con.execute("ALTER TABLE h2h_summary ADD COLUMN anchor_finished INTEGER")


def best_anchor_per_pair(f: Fetcher, raw: Path, tour: int, season: int,
                         rounds: list[int]) -> dict[tuple[str, str], dict]:
    """{(team_a, team_b): анкериращ мач} — за всяка двойка в цялото
    разписание. Предпочитание: последната ИЗИГРАНА среща; при липса на
    такава — кой да е бъдещ fixture (само за да имаме event_id за h2h)."""
    best: dict[tuple[str, str], dict] = {}
    for rd in rounds:
        cache = raw / "rounds" / f"{tour}_{season}_r{rd}.json"
        data = f.get(f"/unique-tournament/{tour}/season/{season}/events/round/{rd}",
                    cache)
        if not data:
            continue
        for ev in data.get("events", []):
            home, away = canon(ev["homeTeam"]["name"]), canon(ev["awayTeam"]["name"])
            key = tuple(sorted([home, away]))
            finished = ev.get("status", {}).get("type") == "finished"
            date = event_date(ev)
            cand = {"event_id": ev["id"], "home": home, "away": away,
                   "date": date, "finished": finished}
            cur = best.get(key)
            if cur is None:
                best[key] = cand
                continue
            if finished and not cur["finished"]:
                best[key] = cand          # изигран измества бъдещ fallback
            elif finished == cur["finished"] and date > cur["date"]:
                best[key] = cand          # по-скорошен от същия тип
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL")
    ap.add_argument("--year", default="25/26")
    ap.add_argument("--db", required=True)
    ap.add_argument("--rounds", default="1-38",
                    help="целият сезонен диапазон за откриване на event_id-та "
                         "(само разписание — НЕ пълни данни за бъдещите кръгове)")
    ap.add_argument("--raw", default=str(RAW))
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--refresh", action="store_true",
                    help="презаписва кеша (кръгове + h2h.json) вместо да го ползва")
    args = ap.parse_args()

    key = args.league.upper()
    tour = int(args.league) if args.league.isdigit() else LEAGUES.get(key)
    if tour is None:
        sys.exit(f"Непозната лига '{args.league}'. Известни: {', '.join(LEAGUES)}")

    raw = Path(args.raw)
    f = Fetcher(delay=args.delay, refresh=args.refresh)
    season, season_name = resolve_season(f, tour, args.year)
    rounds = parse_rounds(args.rounds)
    print(f"{season_name} (season id {season}) — сканирам кръгове "
         f"{rounds[0]}-{rounds[-1]} за разписание (леко, без пълни данни)")

    pairs = best_anchor_per_pair(f, raw, tour, season, rounds)
    with_future_anchor = sum(1 for r in pairs.values() if not r["finished"])
    print(f"Двойки в разписанието: {len(pairs)}  "
         f"(от тях {with_future_anchor} закотвени на бъдещ, все още неизигран мач)")

    con = sqlite3.connect(args.db)
    ensure_h2h_schema(con)

    got = skipped = 0
    for (ta, tb), r in pairs.items():
        eid = r["event_id"]
        data = f.get(f"/event/{eid}/h2h", raw / "events" / str(eid) / "h2h.json")
        duel = (data or {}).get("teamDuel")
        if not duel:
            skipped += 1
            continue
        home_wins = duel.get("homeWins", 0) or 0
        away_wins = duel.get("awayWins", 0) or 0
        draws = duel.get("draws", 0) or 0
        if r["home"] == ta:
            wins_a, wins_b = home_wins, away_wins
        else:
            wins_a, wins_b = away_wins, home_wins
        total = wins_a + wins_b + draws
        if total == 0:
            skipped += 1
            continue
        con.execute(
            "INSERT OR REPLACE INTO h2h_summary VALUES (?,?,?,?,?,?,?,?,?)",
            (ta, tb, wins_a, wins_b, draws, total, eid, r["date"],
             int(r["finished"])))
        got += 1

    con.commit()
    con.close()
    print(f"H2H запазени: {got}   пропуснати (без данни от Sofascore): {skipped}   "
         f"двойки общо: {len(pairs)}")
    print(f"Заявки: {f.downloads} изтеглени, {f.hits} от кеша")
    if f.fails:
        print(f"Провалени: {len(f.fails)}")
        for x in f.fails[:10]:
            print("  !", x)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
