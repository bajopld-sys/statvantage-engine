"""Калибрира свободните "форма" параметри на Модел Г (calibrate_model_g.py) -
координатно-извиващо (coordinate-wise) решетъчно търсене, огледално на
calibrate_rho.py / calibrate_alpha_v1.py, но с probabilistic (Brier score)
цел вместо log-likelihood, защото Модел Г излиза направо 1X2 вероятности,
не λ/μ голови параметри.

Калибрират се САМО параметрите, които аз интерполирах при 4-те подобрения
(не са част от точковата спецификация на потребителя):
  - form_recent_w    (тегло на последните 3 мача във form_points, default 0.6)
  - goal_bal_scale   (т./гол в goal_balance_points, default 1.5)
  - home_adv_divisor (делител на PPG разликата в home_advantage_points, default 6.0)
  - draw_base        (базов дял за равенство в draw_share, default 0.20)
  - draw_swing       (обхват на динамичната корекция в draw_share, default 0.16)

Тежестите на 6-те фактора (25/20/20/15/10/10) са СПЕЦИФИКАЦИЯ на потребителя
и НЕ се пипат тук.

Тестова извадка: същите 3 walk-forward прозореца, ползвани при преди/след
сравнението на 4-те подобрения (пул от 225 мача общо):
  - Eliteserien, от кръг 12 (34 мача)
  - Eliteserien, от кръг 4  (98 мача)
  - Allsvenskan, от кръг 4  (93 мача)

Употреба:  python engine/calibrate_model_g.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_b_heuristic import LEAGUES, load_matches
from model_g_scoring import (predict_matches, _load_h2h_safe, FORM_RECENT_W,
                             GOAL_BAL_SCALE, HOME_ADV_DIVISOR, DRAW_SHARE_BASE,
                             DRAW_SHARE_SWING)

from config import DATA_DIR
WINDOWS = [
    ("Eliteserien r12+", LEAGUES["ELITESERIEN"], 12),
    ("Eliteserien r4+", LEAGUES["ELITESERIEN"], 4),
    ("Allsvenskan r4+", DATA_DIR / "sofa_allsvenskan.db", 4),
]

DEFAULTS = {
    "form_recent_w": FORM_RECENT_W,
    "goal_bal_scale": GOAL_BAL_SCALE,
    "home_adv_divisor": HOME_ADV_DIVISOR,
    "draw_base": DRAW_SHARE_BASE,
    "draw_swing": DRAW_SHARE_SWING,
}

# (grid, mирошна ширина за прецизиране) - разширени граници (2026-08-06), за да
# провери дали 3-те параметъра, опрели в ръба на по-тясната решетка (form_recent_w,
# goal_bal_scale, home_adv_divisor), имат вътрешен оптимум или продължават да бягат.
GRIDS = {
    "form_recent_w": [round(0.15 + 0.05 * i, 2) for i in range(17)],       # 0.15..0.95
    "goal_bal_scale": [round(0.25 * i, 2) for i in range(1, 25)],          # 0.25..6.0
    "home_adv_divisor": [round(1.0 + 0.5 * i, 2) for i in range(23)],      # 1.0..12.0
    "draw_base": [round(0.10 + 0.01 * i, 2) for i in range(21)],           # 0.10..0.30
    "draw_swing": [round(0.02 * i, 2) for i in range(17)],                 # 0.0..0.32
}


def load_windows():
    cache = {}
    loaded = []
    for label, db_path, start_round in WINDOWS:
        if db_path not in cache:
            if not db_path.exists():
                sys.exit(f"Няма база {db_path}")
            cache[db_path] = (load_matches(db_path), _load_h2h_safe(db_path))
        matches_all, h2h_all = cache[db_path]
        loaded.append((label, matches_all, h2h_all, start_round))
    return loaded


def brier_and_acc(preds: list[dict]) -> tuple[float, int, int]:
    """Връща (сума на Brier score, брой попадения, общо мачове) за списък от
    predict_matches() резултати."""
    brier_sum = 0.0
    hits = 0
    for p in preds:
        res, actual = p["res"], p["actual"]
        y = {"1": (1, 0, 0), "X": (0, 1, 0), "2": (0, 0, 1)}[actual]
        ph, pd_, pa = res["p_home"], res["p_draw"], res["p_away"]
        brier_sum += (ph - y[0]) ** 2 + (pd_ - y[1]) ** 2 + (pa - y[2]) ** 2
        hits += res["pick_1x2"] == actual
    return brier_sum, hits, len(preds)


def evaluate(windows, params: dict) -> tuple[float, int, int]:
    """Пул Brier сума + hits/total през и трите прозореца, за дадени параметри."""
    total_brier = 0.0
    total_hits = total_n = 0
    for _label, matches_all, h2h_all, start_round in windows:
        preds = predict_matches(matches_all, h2h_all, start_round, **params)
        b, h, n = brier_and_acc(preds)
        total_brier += b
        total_hits += h
        total_n += n
    return total_brier, total_hits, total_n


def coordinate_search(windows, total_n: int, passes: int = 2) -> dict:
    best = dict(DEFAULTS)
    for p in range(passes):
        for key in GRIDS:
            candidates = []
            for val in GRIDS[key]:
                trial = dict(best)
                trial[key] = val
                brier, hits, n = evaluate(windows, trial)
                candidates.append((val, brier, hits))
            best_val, best_brier, best_hits = min(candidates, key=lambda x: x[1])
            best[key] = best_val
            print(f"  pass {p+1}, {key}: best={best_val} "
                 f"(Brier/мач={best_brier/total_n:.4f})")
    return best


def main() -> None:
    windows = load_windows()
    total_n = sum(len(predict_matches(m, h, r)) for _l, m, h, r in windows)
    print(f"Общо мачове в пула: {total_n}\n")

    print("── Базови (текущи) стойности ──")
    base_brier, base_hits, _ = evaluate(windows, DEFAULTS)
    print(f"  Brier/мач={base_brier/total_n:.4f}  точност={base_hits/total_n:.1%} "
         f"({base_hits}/{total_n})\n")

    print("── Координатно-извиващо търсене ──")
    calibrated = coordinate_search(windows, total_n)

    print("\n── Калибрирани стойности ──")
    cal_brier, cal_hits, _ = evaluate(windows, calibrated)
    for key, val in calibrated.items():
        print(f"  {key:<18} {DEFAULTS[key]:>7}  ->  {val:>7}")
    print(f"\n  Brier/мач: {base_brier/total_n:.4f} -> {cal_brier/total_n:.4f} "
         f"({(cal_brier-base_brier)/total_n:+.4f})")
    print(f"  точност:   {base_hits/total_n:.1%} -> {cal_hits/total_n:.1%} "
         f"({cal_hits-base_hits:+d}/{total_n} мача)")

    print("\n── По прозорец (калибрирани параметри) ──")
    for label, matches_all, h2h_all, start_round in windows:
        preds = predict_matches(matches_all, h2h_all, start_round, **calibrated)
        b, h, n = brier_and_acc(preds)
        base_preds = predict_matches(matches_all, h2h_all, start_round, **DEFAULTS)
        bb, bh, _ = brier_and_acc(base_preds)
        print(f"  {label:<18} база: {bh}/{n} ({bh/n:.1%}, Brier/мач={bb/n:.4f})   "
             f"калибр.: {h}/{n} ({h/n:.1%}, Brier/мач={b/n:.4f})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
