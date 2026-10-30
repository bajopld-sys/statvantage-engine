"""Рекалибрация на λ/μ (очаквани голове) — ПО ЛИГА, със shrinkage към pooled.

ПРОБЛЕМЪТ (открит 2026-09-08, при въпрос на потребителя "как може да оправим
λ/μ"): регресията на РЕАЛНИТЕ голове спрямо прогнозираните дава наклон
0.37-0.87 вместо 1.00 при всичките лиги — тоест λ/μ са ПРЕКАЛЕНО разпръснати,
около два пъти по-крайни от реалността. Моделът систематично надценява
силните отбори и подценява слабите:

    прогноза λ=2.59  →  реално 2.22   (надценка -0.36)
    прогноза λ=0.91  →  реално 1.16   (подценка +0.26)

Това е класическа attenuation: оценките на λ съдържат шум (тънки извадки,
форма, lineup корекции), а шумът раздува дисперсията им спрямо истинската.
Поправката е линейна рекалибрация, научена от самите данни:

    λ_калибрирано = a_home + b_home × λ_сурово
    μ_калибрирано = a_away + b_away × μ_сурово

Измерено извън извадката (743 мача учат, 744 тестват): logL се подобрява с
+12.6, Г/Г точността с +3.0пп, Над2.5 с +0.8пп, 1X2 с +0.7пп — порядък
по-голям ефект от per-league rho (+1.45 logL, +0.6пп на един пазар).

Shrinkage към pooled (същия принцип като calibrate_rho.py и
simulate_lineup_v1.shrink_index()): наклоните варират силно по лига
(EPL 0.40, Бундеслига 0.87), но при малките извадки това пак може да е шум.
Тегло на собствената стойност = n/(n+SHRINK_K).

Употреба:
    python engine/backtest_sofa.py --league <име>   # веднъж на лига
    python engine/calibrate_lambda.py               # пише data/lambda_calibration.csv
    python engine/calibrate_lambda.py --validate    # проверка извън извадката
"""
import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))

DATA = DATA_DIR

# Два модела се калибрират оттук (2026-09-08). v2 НЕ може — чете
# Sofascore атрибутите с yearShift=0 (текущия сезон), затова всеки
# исторически бектест би бил с поглед напред — виж паметта
# [[v2-probability-calibration-pending]].
MODELS = {
    # ключ: (glob шаблон, колона лямбда, колона мю, изходен файл)
    "dc": ("backtest_sofa_*.csv", "lam_x", "mu_x", "lambda_calibration.csv"),
    "v1": ("backtest_v1_*.csv", "lam", "mu", "lambda_calibration_v1.csv"),
}

MIN_CALIB_N = 80    # под този брой мача — само pooled (същият праг като rho)
SHRINK_K = 150.0    # тегло на собствената стойност = n/(n+K). По-малко от rho's
# 200, защото линейният наклон се идентифицира по-бързо от rho (два параметъра
# върху непрекъсната величина, срещу една фина корекция на 4 клетки).


def fit(df: pd.DataFrame, lam_col: str = "lam_x", mu_col: str = "mu_x") -> tuple[float, float, float, float]:
    """(a_home, b_home, a_away, b_away) — OLS на реалните срещу прогнозните."""
    bh, ah = np.polyfit(df[lam_col], df["hg"], 1)
    ba, aa = np.polyfit(df[mu_col], df["ag"], 1)
    return float(ah), float(bh), float(aa), float(ba)


def load_frames(pattern: str, lam_col: str, mu_col: str) -> dict[str, pd.DataFrame]:
    out = {}
    prefix = pattern.split("*")[0]
    for path in sorted(DATA.glob(pattern)):
        league = path.stem.replace(prefix, "").lower()
        df = pd.read_csv(path)
        if {lam_col, mu_col, "hg", "ag"} <= set(df.columns):
            out[league] = df
    return out


def build_rows(frames: dict[str, pd.DataFrame], lam_col: str = "lam_x",
               mu_col: str = "mu_x") -> list[dict]:
    pooled = pd.concat(frames.values(), ignore_index=True)
    p = fit(pooled, lam_col, mu_col)
    rows = [{"league": "ALL", "a_home": round(p[0], 4), "b_home": round(p[1], 4),
             "a_away": round(p[2], 4), "b_away": round(p[3], 4),
             "n": len(pooled), "shrink_weight": 1.0,
             "raw_b_home": round(p[1], 4), "raw_b_away": round(p[3], 4)}]
    for league, df in sorted(frames.items()):
        n = len(df)
        if n < MIN_CALIB_N:
            continue
        own = fit(df, lam_col, mu_col)
        w = n / (n + SHRINK_K)
        shrunk = [w * o + (1 - w) * pl for o, pl in zip(own, p)]
        rows.append({"league": league,
                     "a_home": round(shrunk[0], 4), "b_home": round(shrunk[1], 4),
                     "a_away": round(shrunk[2], 4), "b_away": round(shrunk[3], 4),
                     "n": n, "shrink_weight": round(w, 3),
                     "raw_b_home": round(own[1], 4), "raw_b_away": round(own[3], 4)})
    return rows


def validate(frames: dict[str, pd.DataFrame], lam_col: str = "lam_x",
             mu_col: str = "mu_x") -> None:
    """Хронологичен split по лига: първата половина учи, втората тества.
    Сравнява 3 варианта — без корекция, само pooled, per-league със shrinkage."""
    from dixon_coles import match_markets

    tr_parts, te_parts = [], []
    for league, df in frames.items():
        d = df.sort_values("date")
        h = len(d) // 2
        tr_parts.append(d.iloc[:h].assign(lg=league))
        te_parts.append(d.iloc[h:].assign(lg=league))
    tr = pd.concat(tr_parts, ignore_index=True)
    te = pd.concat(te_parts, ignore_index=True)

    rows = build_rows({lg: g for lg, g in tr.groupby("lg")}, lam_col, mu_col)
    calib = {r["league"]: r for r in rows}
    pooled = calib["ALL"]

    te = te.copy()
    te["over"] = (te.hg + te.ag) > 2.5
    te["btts"] = (te.hg > 0) & (te.ag > 0)
    te["res"] = ["H" if h > a else ("A" if a > h else "D") for h, a in zip(te.hg, te.ag)]

    def score(mode: str) -> None:
        ll = h1 = ho = hb = 0
        for r in te.itertuples():
            raw_lam = getattr(r, lam_col)
            raw_mu = getattr(r, mu_col)
            if mode == "none":
                lam, mu = raw_lam, raw_mu
            else:
                c = pooled if mode == "pooled" else calib.get(r.lg, pooled)
                lam = c["a_home"] + c["b_home"] * raw_lam
                mu = c["a_away"] + c["b_away"] * raw_mu
            m = match_markets(max(lam, 0.05), max(mu, 0.05), rho=-0.048)
            pr = {"H": m["p_home"], "D": m["p_draw"], "A": m["p_away"]}
            ll += np.log(max(pr[r.res], 1e-12))
            h1 += max(pr, key=pr.get) == r.res
            ho += (m["p_over25"] >= 0.5) == r.over
            hb += (m["p_btts"] >= 0.5) == r.btts
        n = len(te)
        label = {"none": "без корекция", "pooled": "само pooled",
                 "league": "per-league + shrinkage"}[mode]
        print(f"  {label:26} logL={ll:8.2f}  1X2={h1/n:.1%}  "
              f"Над2.5={ho/n:.1%}  Г/Г={hb/n:.1%}")

    print(f"Валидация: {len(tr)} мача учат, {len(te)} тестват (хронологично по лига)\n")
    for mode in ("none", "pooled", "league"):
        score(mode)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="сравнява без корекция / pooled / per-league извън извадката")
    ap.add_argument("--model", default="dc", choices=sorted(MODELS),
                    help="dc = Dixon-Coles (backtest_sofa_*.csv), v1 = Модел В v1 "
                         "(backtest_v1_*.csv)")
    args = ap.parse_args()

    pattern, lam_col, mu_col, out_name = MODELS[args.model]
    out_path = DATA / out_name
    frames = load_frames(pattern, lam_col, mu_col)
    if not frames:
        sys.exit(f"Няма data/{pattern} — пусни първо съответния бектест.")

    if args.validate:
        validate(frames, lam_col, mu_col)
        return

    rows = build_rows(frames, lam_col, mu_col)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["league", "a_home", "b_home", "a_away", "b_away",
                                          "n", "shrink_weight", "raw_b_home", "raw_b_away"])
        w.writeheader()
        w.writerows(rows)

    print(f"Pooled ({rows[0]['n']} мача): λ_нов = {rows[0]['a_home']:+.3f} + "
          f"{rows[0]['b_home']:.3f}×λ   μ_нов = {rows[0]['a_away']:+.3f} + "
          f"{rows[0]['b_away']:.3f}×μ\n")
    print("По лига (в скоби суровият наклон преди свиване):")
    for r in rows[1:]:
        print(f"  {r['league']:14} n={r['n']:4}  тегло {r['shrink_weight']:.0%}  "
              f"b_дом={r['b_home']:.3f} ({r['raw_b_home']:.2f})  "
              f"b_гост={r['b_away']:.3f} ({r['raw_b_away']:.2f})")
    print(f"\nЗаписано: {out_path.name}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
