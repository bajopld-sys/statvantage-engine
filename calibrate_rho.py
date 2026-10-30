"""Калибрира RHO (Dixon-Coles ниска-резултат корекция) чрез максимално
правдоподобие върху walk-forward λ/μ от backtest_sofa.py.

ПО ЛИГА (2026-09-08, по въпрос на потребителя: "не е ли по-коректно да се
зададе на всяка лига отделно, защото всяка лига е различна"): rho мери колко
по-често се падат ниски резултати (0:0, 1:0, 0:1, 1:1) спрямо чист Poisson —
това е стилова характеристика на лигата (дефанзивна срещу офанзивна), не
универсална константа. Досега една стойност (-0.12), изведена САМО от EPL
25/26 (320 мача, 2025-10-03 → 2026-05-24), се ползваше за всичките 10 лиги.

Логиката огледава build_opta_elo.py's калибрация: собствена стойност за
лигата, ако има >= MIN_RHO_CALIB_N мача в бектеста; иначе pooled "ALL" ред
(всички лиги наведнъж), докато натрупа история.

Чете ВСИЧКИ data/backtest_sofa*.csv файлове (по един на лига — генерирай ги с
`python engine/backtest_sofa.py --league <име>`), пише data/rho_calibration.csv,
който dixon_coles.rho_for_league() чете.

Употреба:
    python engine/backtest_sofa.py --league eliteserien   # веднъж на лига
    python engine/calibrate_rho.py
"""
import csv
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dixon_coles import score_matrix, MAX_GOALS, RHO

DATA = DATA_DIR
OUT_PATH = DATA / "rho_calibration.csv"

MIN_RHO_CALIB_N = 80   # под този брой мача per-league оценката е предимно шум —
# log-likelihood повърхността на rho е плоска (виж бележката в dixon_coles.RHO:
# дори при 320 мача третата десетична не е стабилна). Начална, НЕ калибрирана
# стойност — прагът е преценка, не изведен от данните.

SHRINK_K = 200.0   # shrinkage към pooled: тегло на собствената стойност =
# n/(n+K). Огледално на simulate_lineup_v1.shrink_index() (там K=3 за играчи;
# тук K е много по-голямо, защото rho се идентифицира далеч по-бавно от
# отборните индекси).
#
# ЗАЩО изобщо shrinkage (2026-09-08, при първото реално пускане): суровите
# per-league оценки излязоха диво нестабилни — Eliteserien +0.056 и Faroe
# +0.160, тоест ПОЛОЖИТЕЛЕН rho, обратен знак на стандартния Dixon-Coles, при
# печалба в log-likelihood от +0.00 до +1.04 върху 24-105 мача (≈0.004/мач).
# Това е шум от плоска правдоподобност, не реална стилова разлика между лигите.
# Shrinkage дава коректното поведение и в двата края: малко данни → почти
# изцяло pooled; много данни → лигата постепенно печели собствения си глас,
# без рязък праг.

# backtest_sofa.csv (без суфикс) е историческият EPL файл (25/26, 2025-10-03 →
# 2026-05-24) — единствената ни пълносезонна извадка, от която дойде
# оригиналното -0.12. Етикетира се просто "epl", защото rho е стилова
# характеристика на ЛИГАТА (колко често падат ниски резултати), която се
# пренася между сезони, за разлика от отборните рейтинги, които не се пренасят.
LEGACY_LABEL = "epl"


def total_log_likelihood(df: pd.DataFrame, lam_col: str, mu_col: str, rho: float) -> float:
    ll = 0.0
    for lam, mu, hg, ag in zip(df[lam_col], df[mu_col], df["hg"], df["ag"]):
        if hg > MAX_GOALS or ag > MAX_GOALS:
            continue    # извън обхвата на score_matrix (MAX_GOALS=10) — редки, пропускат се
        m = score_matrix(lam, mu, rho=rho)
        ll += np.log(max(m[hg, ag], 1e-12))
    return ll


def best_rho(df: pd.DataFrame, lam_col: str = "lam_x", mu_col: str = "mu_x") -> tuple[float, float]:
    """(rho, logL) — груба решетка, после локално прецизиране."""
    coarse = np.round(np.arange(-0.30, 0.16, 0.02), 3)
    scores = [(r, total_log_likelihood(df, lam_col, mu_col, r)) for r in coarse]
    c_rho, _ = max(scores, key=lambda x: x[1])
    fine = np.linspace(c_rho - 0.02, c_rho + 0.02, 21)
    return max(((r, total_log_likelihood(df, lam_col, mu_col, r)) for r in fine),
               key=lambda x: x[1])


def load_league_frames() -> dict[str, pd.DataFrame]:
    """{име_на_лига: df} от всички data/backtest_sofa*.csv."""
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(DATA.glob("backtest_sofa*.csv")):
        m = re.fullmatch(r"backtest_sofa_(.+)", path.stem)
        league = m.group(1).lower() if m else LEGACY_LABEL
        df = pd.read_csv(path)
        if "lam_x" not in df.columns:
            print(f"  ⚠ {path.name} е отпреди добавянето на lam/mu колоните — пропуснат")
            continue
        out[league] = df
    return out


def main() -> None:
    frames = load_league_frames()
    if not frames:
        sys.exit("Няма нито един data/backtest_sofa*.csv — пусни първо engine/backtest_sofa.py.")

    pooled = pd.concat(frames.values(), ignore_index=True)
    pooled_rho, pooled_ll = best_rho(pooled)
    print(f"── Pooled (ALL): {len(pooled)} мача от {len(frames)} лиги ──")
    print(f"   rho={pooled_rho:+.4f}   logL={pooled_ll:.2f}   logL/мач={pooled_ll/len(pooled):.4f}")
    print(f"   (за сравнение, старата глобална константа {RHO:+.2f}: "
          f"logL={total_log_likelihood(pooled, 'lam_x', 'mu_x', RHO):.2f})\n")

    rows = [{"league": "ALL", "rho": round(pooled_rho, 4), "n": len(pooled),
             "ll_gain_vs_pooled": 0.0, "own_calibration": 0,
             "raw_rho": round(pooled_rho, 4), "shrink_weight": 1.0}]

    print("── По лига ──")
    for league, df in sorted(frames.items()):
        n = len(df)
        own_rho, own_ll = best_rho(df)
        ll_at_pooled = total_log_likelihood(df, "lam_x", "mu_x", pooled_rho)
        gain = own_ll - ll_at_pooled          # колко печели СУРОВАТА своя стойност
        qualifies = n >= MIN_RHO_CALIB_N
        w = n / (n + SHRINK_K)
        shrunk = w * own_rho + (1 - w) * pooled_rho
        mark = (f"✔ собствена (тегло {w:.0%})" if qualifies
                else f"✖ pooled (под {MIN_RHO_CALIB_N})")
        print(f"  {league:16} n={n:4}  сурово rho={own_rho:+.4f} → свито {shrunk:+.4f}  "
              f"logL печалба (сурово): {gain:+.2f}  →  {mark}")
        if qualifies:
            rows.append({"league": league, "rho": round(shrunk, 4), "n": n,
                         "ll_gain_vs_pooled": round(gain, 3), "own_calibration": 1,
                         "raw_rho": round(own_rho, 4), "shrink_weight": round(w, 3)})

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["league", "rho", "n", "ll_gain_vs_pooled",
                                          "own_calibration", "raw_rho", "shrink_weight"])
        w.writeheader()
        w.writerows(rows)
    own_n = sum(r["own_calibration"] for r in rows)
    print(f"\nЗаписано: {OUT_PATH.name} — {own_n} лиги със собствена калибрация, "
          f"останалите падат на pooled 'ALL' ({pooled_rho:+.4f}).")
    print(f"Записаните rho са СВИТИ към pooled (SHRINK_K={SHRINK_K:.0f}) — суровата "
          f"стойност е в колоната raw_rho за справка.")
    print("ЗАБЕЛЕЖКА: 'logL печалба' близка до 0 значи, че суровата стойност на лигата "
          "не носи реално подобрение — шум от плоска правдоподобност, не стилова разлика.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
