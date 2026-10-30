"""Walk-forward бектест на Модел В v1 (lineup-based xg/xa) — генерира
data/backtest_v1_<лига>.csv за calibrate_alpha_v1.py (NB_ALPHA калибрация,
огледално на backtest_sofa.py/calibrate_rho.py за RHO).

За разлика от backtest_sofa.py (отборно ниво, само cutoff по дата), тук
трябва РЕАЛНИЯТ старов състав на всеки исторически мач — вместо live
Sofascore prediction (fetch_lineup_starters е за предстоящи мачове), четем
директно player_match.started=1 за match_id-то (реалният състав, който е
играл, вече в базата от parse_sofa_api.py). team_indices()/league_averages()
вътре в simulate() си гледат само мачове ПРЕДИ as_of — без поглед напред.

Употреба:  python engine/backtest_lineup_v1.py --league ELITESERIEN [--min-hist 30]
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_lineup_v1 import simulate, LEAGUES

MIN_HIST = 30
MIN_STARTERS = 8  # под това броим състава за непълен/липсващ в базата


def starters_for_match(con: sqlite3.Connection, match_id: str, team: str) -> set[str]:
    rows = con.execute(
        "SELECT name FROM player_match WHERE match_id=? AND team=? AND started=1",
        (match_id, team)).fetchall()
    return {r[0] for r in rows}


def run(league: str, min_hist: int, db_override: str | None = None) -> pd.DataFrame:
    if db_override:
        db_path = Path(db_override)
    else:
        db_name = LEAGUES.get(league.upper())
        if not db_name:
            sys.exit(f"Непозната лига за v1. Известни: {', '.join(LEAGUES)}")
        db_path = DATA_DIR / db_name
    if not db_path.exists():
        sys.exit(f"Няма {db_path}.")
    con = sqlite3.connect(db_path)

    matches = con.execute(
        "SELECT match_id, date, home, away, hg, ag FROM matches ORDER BY date").fetchall()
    all_teams = sorted({m[2] for m in matches} | {m[3] for m in matches})

    records = []
    skipped_lineup, skipped_error = 0, 0
    # ПОПРАВКА (2026-09-08): n_before се смяташе с пълно сканиране вътре в
    # цикъла — O(n²), както в backtest_sofa.py. Мачовете идват ORDER BY date,
    # затова броят по-ранни се смята веднъж, инкрементално.
    dates = [m[1] for m in matches]
    n_before_by_idx, seen = [], 0
    for idx, d in enumerate(dates):
        while seen < idx and dates[seen] < d:
            seen += 1
        n_before_by_idx.append(seen)
    for idx, (match_id, date, h, a, hg, ag) in enumerate(matches):
        as_of = pd.Timestamp(date)
        if n_before_by_idx[idx] < min_hist:
            continue

        home_starters = starters_for_match(con, match_id, h)
        away_starters = starters_for_match(con, match_id, a)
        if len(home_starters) < MIN_STARTERS or len(away_starters) < MIN_STARTERS:
            skipped_lineup += 1
            continue

        res = simulate(con, h, a, as_of, home_starters, away_starters, all_teams)
        if "error" in res:
            skipped_error += 1
            continue

        result = "H" if hg > ag else ("A" if ag > hg else "D")
        records.append({
            "match_id": match_id, "date": date, "home": h, "away": a,
            "hg": hg, "ag": ag, "result": result,
            # СУРОВИ λ/μ — калибрацията се УЧИ от тях; записването на
            # калибрираните би приложило свиването два пъти (2026-09-08).
            "lam": res.get("lam_raw", res["lam"]), "mu": res.get("mu_raw", res["mu"]),
        })

    print(f"Мачове общо: {len(matches)}  |  включени: {len(records)}  |  "
         f"пропуснати (непълен състав в базата): {skipped_lineup}  |  "
         f"пропуснати (недостатъчно играчови данни): {skipped_error}")
    return pd.DataFrame(records)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="ELITESERIEN")
    ap.add_argument("--min-hist", type=int, default=MIN_HIST)
    ap.add_argument("--db", help="директен път до база (за архивни сезони)")
    args = ap.parse_args()

    df = run(args.league, args.min_hist, args.db)
    if df.empty:
        sys.exit("Няма нито един използваем мач (непълни състави или малко история).")
    data = DATA_DIR
    out = data / f"backtest_v1_{args.league.lower()}.csv"
    df.to_csv(out, index=False)
    print(f"Записано: {out}  ({len(df)} мача)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
