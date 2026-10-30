"""Walk-forward бектест на Sofascore моделите (xG и threat) — огледално на
backtest.py, но по-просто (без lineups, sofa.db е на ниво отбор).

За всеки мач: cutoff = датата му → профили само от МАЧОВЕ ПРЕДИ тази дата
(in-memory филтрирано copy на sofa.db, за да няма поглед напред).

Употреба:  python engine/backtest_sofa.py [--min-hist 60]
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profiles_sofa import build_profiles, SOFA_DB
from simulate_sofa import simulate_xg, simulate_threat
from h2h import build_h2h, build_h2h_shots, build_h2h_markets

MIN_HIST = 60


def _filtered_con(src: sqlite3.Connection, cutoff: pd.Timestamp) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:")
    for (sql,) in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql NOT NULL"):
        mem.execute(sql)
    cut = cutoff.strftime("%Y-%m-%d")
    ids = [r[0] for r in src.execute("SELECT match_id FROM matches WHERE date < ?", (cut,))]
    idset = set(ids)
    # ПОПРАВКА (2026-07): "player_ratings" липсваше тук — profiles_sofa.py
    # чете rating_form от тази таблица, затова тя оставаше празна във всеки
    # walk-forward кръг и rating_form тихо излизаше 1.0 (неутрално) за всеки
    # отбор във всеки бектест, вместо истинската му стойност.
    #
    # ПОПРАВКА (2026-08-09): "player_match" добавена — profiles_sofa._rating_form()
    # вече ПРЕДПОЧИТА player_match (новия API пайплайн) пред player_ratings (виж
    # profiles_sofa._read_ratings_table()), защото player_ratings изобщо не
    # съществува за активните лиги (Eliteserien/Allsvenskan/Bundesliga2). Без този
    # ред бектестът пак би измервал неутрален rating_form за тях, дори с фикса в
    # profiles_sofa.py — таблицата просто не стигаше до in-memory копието тук.
    for tbl in ("matches", "stats", "player_ratings", "player_match"):
        if not src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
            continue  # не всяка лига има тази таблица (виж goal_timeline.has_goal_events)
        cols = [d[1] for d in src.execute(f"PRAGMA table_info({tbl})")]
        rows = [r for r in src.execute(f"SELECT * FROM {tbl}")
                if r[cols.index("match_id")] in idset]
        if rows:
            mem.executemany(f"INSERT INTO {tbl} VALUES ({','.join('?' * len(cols))})", rows)
    mem.commit()
    return mem


def run(min_hist: int, db_path: Path | None = None):
    src = sqlite3.connect(db_path or SOFA_DB)
    matches = src.execute(
        "SELECT match_id, date, home, away, hg, ag FROM matches ORDER BY date").fetchall()

    records = []
    cache = {}
    # ПОПРАВКА (2026-09-04): n_before се смяташе с пълно сканиране на списъка
    # ВЪТРЕ в цикъла по същия списък — O(n²) (при 3000 мача ~9 млн. сравнения
    # само за този брояч). Мачовете идват вече ORDER BY date, затова броят
    # по-ранни се смята веднъж, инкрементално, преди цикъла.
    dates = [m[1] for m in matches]
    n_before_by_index = []
    seen = 0
    for idx, d in enumerate(dates):
        while seen < idx and dates[seen] < d:
            seen += 1
        n_before_by_index.append(seen)
    for idx, (match_id, date, h, a, hg, ag) in enumerate(matches):
        cutoff = pd.Timestamp(date)
        n_before = n_before_by_index[idx]
        if n_before < min_hist:
            continue
        key = date
        if key not in cache:
            fcon = _filtered_con(src, cutoff)
            try:
                prof = build_profiles(fcon, as_of=cutoff)
            except SystemExit:
                cache[key] = None
                continue
            h2h = build_h2h(cutoff)
            h2h_m = build_h2h_markets(cutoff)
            h2h_s = build_h2h_shots(cutoff)
            cache[key] = (prof, h2h, h2h_m, h2h_s)
        if cache[key] is None:
            continue
        prof, h2h, h2h_m, h2h_s = cache[key]
        if h not in prof["teams"].index or a not in prof["teams"].index:
            continue

        sx = simulate_xg(prof, h, a, h2h, h2h_m, True)
        st = simulate_threat(prof, h, a, h2h_s, True)

        res = "H" if hg > ag else ("A" if ag > hg else "D")
        records.append({
            "date": date, "home": h, "away": a, "hg": hg, "ag": ag, "result": res,
            # СУРОВИ λ/μ (2026-09-08): simulate_xg() вече връща КАЛИБРИРАНИ
            # в "lam"/"mu"; записването им тук би накарало calibrate_lambda.py
            # да се учи от вече коригирани стойности (двойно свиване).
            "lam_x": sx.get("lam_raw", sx["lam"]), "mu_x": sx.get("mu_raw", sx["mu"]),
            "lam_t": st["lam"], "mu_t": st["mu"],      # за пре-калибриране на rho
            "p_home_x": sx["p_home"], "p_draw_x": sx["p_draw"], "p_away_x": sx["p_away"],
            "p_over25_x": sx["p_over25"], "p_btts_x": sx["p_btts"],
            "p_home_t": st["p_home"], "p_draw_t": st["p_draw"], "p_away_t": st["p_away"],
            "p_over25_t": st["p_over25"], "p_btts_t": st["p_btts"],
        })
    return pd.DataFrame(records)


def fav_accuracy(df, suffix, label):
    picks = []
    for _, r in df.iterrows():
        probs = {"H": r[f"p_home{suffix}"], "D": r[f"p_draw{suffix}"], "A": r[f"p_away{suffix}"]}
        k = max(probs, key=probs.get)
        picks.append((probs[k], r["result"] == k))
    n = len(picks)
    wins = sum(1 for _, w in picks if w)
    print(f"{label:16}: {wins}/{n} = {wins/n:.1%}")
    for lo, hi, name in [(0.33, 0.45, "33-45%"), (0.45, 0.55, "45-55%"),
                         (0.55, 0.65, "55-65%"), (0.65, 1.01, "65%+")]:
        sub = [(c, w) for c, w in picks if lo <= c < hi]
        if sub:
            w = sum(1 for _, won in sub if won)
            print(f"    {name:8} {w}/{len(sub)} = {w/len(sub):.0%}")


def market_accuracy(df, market, col_x, col_t, actual_col):
    for suffix, col in (("xG", col_x), ("threat", col_t)):
        picks = []
        for _, r in df.iterrows():
            p = r[col]
            over = p >= 0.5
            won = r[actual_col] if over else (not r[actual_col])
            picks.append(won)
        n = len(picks)
        wins = sum(picks)
        print(f"  {market} ({suffix}): {wins}/{n} = {wins/n:.1%}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hist", type=int, default=MIN_HIST)
    ap.add_argument("--league", help="напр. eliteserien/allsvenskan/bundesliga2 - "
                    "определя data/sofa_<лига>.db; по подразбиране EPL (data/sofa.db)")
    ap.add_argument("--db", help="директен override на пътя до базата (има приоритет пред --league)")
    args = ap.parse_args()

    data = DATA_DIR
    if args.db:
        db_path = Path(args.db)
    elif args.league:
        db_path = data / f"sofa_{args.league.lower()}.db"
    else:
        db_path = None  # SOFA_DB (EPL) по подразбиране

    df = run(args.min_hist, db_path)
    # ПОПРАВКА (2026-09-08): при лига без достатъчно история (напр. Championship
    # с 60 мача при --min-hist 60) df излизаше празен, записваше се празен CSV и
    # следващите редове гърмяха с KeyError: 'hg' вместо да кажат защо.
    if df.empty:
        sys.exit(f"Няма достатъчно история за бектест (--min-hist {args.min_hist}) — "
                 f"базата има твърде малко изиграни мачове. Намали --min-hist или "
                 f"изчакай да се натрупат още кръгове.")
    out_name = f"backtest_sofa_{args.league.lower()}.csv" if args.league else "backtest_sofa.csv"
    df.to_csv(data / out_name, index=False)

    df["over"] = (df["hg"] + df["ag"]) > 2.5
    df["btts"] = (df["hg"] > 0) & (df["ag"] > 0)

    print(f"Мачове (walk-forward, Sofascore): {len(df)}\n")
    print("1X2 ФАВОРИТ:")
    fav_accuracy(df, "_x", "xG модел")
    print()
    fav_accuracy(df, "_t", "threat модел")

    print("\nГОЛОВИ ПАЗАРИ:")
    market_accuracy(df, "Над 2.5", "p_over25_x", "p_over25_t", "over")
    market_accuracy(df, "BTTS", "p_btts_x", "p_btts_t", "btts")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
