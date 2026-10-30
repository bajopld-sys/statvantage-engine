"""Ранно изплащане (early payout): букмейкъри изплащат залог "X печели"
веднага щом X поведе с 2 гола разлика, независимо от крайния резултат —
пример: Lens поведе 2:0 срещу Monaco (кръг 23), после загуби 2:3, но залог
"Lens печели" вече е изплатен като печеливш при достигане на 2-головото
предимство.

Предпочита goal_timeline.build_goal_timelines_from_events() (точен, от
fetch_sofa.py API данни — EPL/LaLiga/Eliteserien), пада на
build_goal_timelines_sofa() (vault MD клипинги, съвпадение фамилия-срещу-
състав) за лиги без API данни (Bundesliga/Ligue1). Покритието зависи от
наличния timeline (не всеки мач се възстановява при vault пътя — виж
бележките в изхода), не е скрито/подразбиращо се 100%.

Употреба:
  python engine/early_payout.py --league EPL --db data/sofa.db
  python engine/early_payout.py --league Ligue1 [--db ...] [--vault ...]
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from config import DATA_DIR, VAULT_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))
from goal_timeline import (build_goal_timelines_sofa,
                           build_goal_timelines_from_events, has_goal_events)




def find_triggers(timeline: list[tuple], home: str, away: str) -> list[dict]:
    """-> [{"team":..., "minute":..., "score_at":"2:0", "lead":2}, ...] —
    ВСЕКИ момент, в който водещото предимство на даден отбор се разширява
    до ново ниво ≥2 (само първия път, когато достигне 2, 3, 4...)."""
    hs = as_ = 0
    seen_leads: dict[str, int] = {home: 0, away: 0}
    triggers = []
    for minute, team, half in timeline:
        if team == home:
            hs += 1
        else:
            as_ += 1
        diff = hs - as_
        leader, lead = (home, diff) if diff > 0 else (away, -diff)
        if lead >= 2 and lead > seen_leads[leader]:
            seen_leads[leader] = lead
            triggers.append({"team": leader, "minute": minute,
                             "score_at": f"{hs}:{as_}", "lead": lead})
    return triggers


def gather(league: str, db: str | None = None, vault: str | None = None) -> dict:
    """-> {"timelines_n","total","notes_n","events":[...],"team_table":[...],
    "comeback_table":[...]}
    events: по един ред за всеки мач с достигнато 2-голово предимство.
    team_table: класация по отбор — колко пъти е повеждал с 2 гола и в
    колко от тях НЕ е спечелил накрая ("прогорял" early payout залог).
    comeback_table: огледалната статистика — колко пъти отборът е ИЗОСТАВАЛ
    с 2 гола и в колко от тях НЕ е загубил накрая (обърнал резултата или
    изтръгнал равенство). "Лидерът не спечели" и "изоставащият не загуби" са
    математически същото събитие (единствените изходи са победа на лидера,
    равенство, или победа на изоставащия) — затова reversed_ се преизползва
    директно за двете страни, не се смята втори път."""
    db_path = Path(db) if db else DATA_DIR / f"sofa_{league.lower()}.db"
    con = sqlite3.connect(db_path)
    if has_goal_events(con):
        # parse_sofa_api.py база (incidents.json) — точен timeline,
        # без съвпадение фамилия-срещу-състав и без vault зависимост.
        timelines, notes = build_goal_timelines_from_events(con)
    else:
        vault_dir = Path(vault) if vault else VAULT_ROOT / league
        timelines, notes = build_goal_timelines_sofa(con, vault_dir)
    rows = con.execute(
        "SELECT match_id, date, home, away, hg, ag FROM matches "
        "ORDER BY date").fetchall()

    events = []
    team_counts: dict[str, dict] = {}
    for match_id, date, home, away, hg, ag in rows:
        if match_id not in timelines:
            continue
        triggers = find_triggers(timelines[match_id], home, away)
        final_winner = home if hg > ag else (away if ag > hg else None)
        for t in triggers:
            if t["lead"] != 2:
                continue
            leader = t["team"]
            trailer = away if leader == home else home
            reversed_ = leader != final_winner
            events.append({"date": date, "home": home, "away": away, "hg": hg, "ag": ag,
                           "team": leader, "trailer": trailer, "minute": t["minute"],
                           "score_at": t["score_at"], "reversed": reversed_})
            tc_l = team_counts.setdefault(leader, {"led2": 0, "no_win": 0, "trailed2": 0, "not_lost": 0})
            tc_l["led2"] += 1
            tc_l["no_win"] += int(reversed_)
            tc_t = team_counts.setdefault(trailer, {"led2": 0, "no_win": 0, "trailed2": 0, "not_lost": 0})
            tc_t["trailed2"] += 1
            tc_t["not_lost"] += int(reversed_)

    team_matches: dict[str, list] = {}
    comeback_matches: dict[str, list] = {}
    for e in events:
        if not e["reversed"]:
            continue
        opp = e["away"] if e["team"] == e["home"] else e["home"]
        team_matches.setdefault(e["team"], []).append({
            "opp": opp, "date": e["date"], "minute": e["minute"],
            "score_at": e["score_at"], "final": f"{e['hg']}:{e['ag']}"})
        comeback_matches.setdefault(e["trailer"], []).append({
            "opp": e["team"], "date": e["date"], "minute": e["minute"],
            "score_at": e["score_at"], "final": f"{e['hg']}:{e['ag']}"})

    team_table = [{"team": t, "led2": c["led2"], "no_win": c["no_win"],
                  "pct": c["no_win"] / c["led2"], "matches": team_matches.get(t, [])}
                 for t, c in team_counts.items() if c["led2"] > 0]
    team_table.sort(key=lambda r: (-r["no_win"], -r["led2"]))

    comeback_table = [{"team": t, "trailed2": c["trailed2"], "not_lost": c["not_lost"],
                      "pct": c["not_lost"] / c["trailed2"], "matches": comeback_matches.get(t, [])}
                     for t, c in team_counts.items() if c["trailed2"] > 0]
    comeback_table.sort(key=lambda r: (-r["not_lost"], -r["trailed2"]))

    return {"timelines_n": len(timelines), "total": len(rows), "notes_n": len(notes),
           "events": events, "team_table": team_table, "comeback_table": comeback_table}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL")
    ap.add_argument("--db")
    ap.add_argument("--vault")
    args = ap.parse_args()

    data = gather(args.league, args.db, args.vault)
    print(f"Ранно изплащане ({args.league}) — {data['timelines_n']}/{data['total']} мача "
          f"с възстановен timeline, {data['notes_n']} непокрити.\n")

    for e in data["events"]:
        flag = " ← НЕ спечели накрая (early payout спасява залога)" if e["reversed"] else ""
        print(f"  {e['date']}  {e['home']} {e['hg']}:{e['ag']} {e['away']}  —  "
              f"{e['team']} поведе {e['score_at']} на {e['minute']}'{flag}")

    hit_count = len(data["events"])
    reversal_count = sum(1 for e in data["events"] if e["reversed"])
    print(f"\nОбщо {hit_count} мача с 2-голово предимство в даден момент; "
          f"{reversal_count} от тях НЕ завършиха с победа на водещия отбор "
          f"(early payout е единствената причина залогът да е печеливш).")

    print("\nКласация по отбор (повел с 2 гола, но НЕ спечелил накрая):")
    for r in data["team_table"]:
        if r["no_win"] == 0:
            continue
        print(f"  {r['team']:<24} {r['no_win']}/{r['led2']} ({r['pct']*100:.0f}%)")

    print("\nКласация по отбор (изостанал с 2 гола, но НЕ загубил накрая):")
    for r in data["comeback_table"]:
        if r["not_lost"] == 0:
            continue
        print(f"  {r['team']:<24} {r['not_lost']}/{r['trailed2']} ({r['pct']*100:.0f}%)")

    if data["notes_n"]:
        print(f"\n({data['notes_n']} мача без възстановен timeline — не са включени по-горе.)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
