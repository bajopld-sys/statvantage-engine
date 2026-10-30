"""Калибрира NB_ALPHA (Модел В v1 Negative Binomial overdispersion) чрез
максимално правдоподобие върху walk-forward λ/μ от backtest_lineup_v1.py
(data/backtest_v1_<лига>.csv) — огледално на calibrate_rho.py, но за alpha
вместо rho, и score_matrix_nbinom() вместо score_matrix() (Poisson).

За всеки мач: P(реалния резултат | λ, μ, alpha) от score_matrix_nbinom().
Търси alpha, максимизиращо сумата log P през всички мачове — груба решетка,
после локално прецизиране (огледално на calibrate_rho.py).

Употреба:  python engine/calibrate_alpha_v1.py --league ELITESERIEN
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dixon_coles import score_matrix_nbinom, MAX_GOALS, NB_ALPHA


def total_log_likelihood(df: pd.DataFrame, alpha: float) -> float:
    ll = 0.0
    for lam, mu, hg, ag in zip(df["lam"], df["mu"], df["hg"], df["ag"]):
        if hg > MAX_GOALS or ag > MAX_GOALS:
            continue    # извън обхвата на score_matrix_nbinom (MAX_GOALS=10) — редки, пропускат се
        m = score_matrix_nbinom(lam, mu, alpha=alpha)
        p = m[hg, ag]
        ll += np.log(max(p, 1e-12))
    return ll


def grid_search(df: pd.DataFrame, coarse: np.ndarray) -> tuple[float, float, list]:
    scores = [(a, total_log_likelihood(df, a)) for a in coarse]
    best_a, best_ll = max(scores, key=lambda x: x[1])
    return best_a, best_ll, scores


def refine(df: pd.DataFrame, center: float, width: float, steps: int = 21):
    fine = np.linspace(max(center - width, 1e-4), center + width, steps)
    scores = [(a, total_log_likelihood(df, a)) for a in fine]
    return max(scores, key=lambda x: x[1])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="ELITESERIEN")
    args = ap.parse_args()

    data_path = (DATA_DIR
                / f"backtest_v1_{args.league.lower()}.csv")
    if not data_path.exists():
        sys.exit(f"Няма {data_path} — пусни първо "
                 f"engine/backtest_lineup_v1.py --league {args.league}.")
    df = pd.read_csv(data_path)
    n = len(df)
    if n == 0:
        sys.exit("Празна извадка — виж пропуснатите бройки от backtest_lineup_v1.py.")
    print(f"Мачове в извадката: {n}\n")

    coarse = np.round(np.arange(0.02, 0.81, 0.02), 3)
    best_a, best_ll, scores = grid_search(df, coarse)
    fine_a, fine_ll = refine(df, best_a, 0.02)
    ll_at_default = total_log_likelihood(df, NB_ALPHA)

    print(f"груба решетка: alpha={best_a:.2f}  logL={best_ll:.2f}")
    print(f"прецизирано:   alpha={fine_a:.4f}  logL={fine_ll:.2f}")
    print(f"сегашно {NB_ALPHA}:  logL={ll_at_default:.2f}  "
         f"(разлика {fine_ll - ll_at_default:+.2f})")
    print(f"logL/мач при оптимума: {fine_ll/n:.4f}   при {NB_ALPHA}: {ll_at_default/n:.4f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
