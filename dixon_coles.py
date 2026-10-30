"""Споделена Dixon-Coles/Poisson математика — извадена от simulate.py
(2026-07-23, при разчистване на стария Opta-clippings пайплайн) в собствен
модул, БЕЗ никаква зависимост от db.py/profiles.py (Opta), за да може
simulate_sofa.py, calibrate_rho.py и бъдещи Sofascore инструменти да я
ползват, без да теглят цялата Opta CLI логика на simulate.py.

Оригиналната математика е по ValueEngine (друг проект на автора,
engine/dixon_coles.py) — score matrix: Poisson + Dixon-Coles rho корекция
за ниските резултати.
"""
import csv
from pathlib import Path

import numpy as np
from scipy.special import gammaln
from config import DATA_DIR

RHO = -0.12          # DC корекция — калибрирано по MLE върху 320 EPL walk-forward
                     # мача (engine/calibrate_rho.py, 2026-07): пик на log-likelihood
                     # при rho≈-0.11..-0.13 (xG и threat модел се съгласяват
                     # независимо); -0.05 беше ясно подоптимално (Δ log-likelihood
                     # +0.73 общо). Точната трета десетична не е стабилно определена
                     # от тази извадка — виж бележките в calibrate_rho.py.
NB_ALPHA = 0.046     # Negative Binomial overdispersion (Модел В v1) — калибрирано
                     # по MLE върху 91-мачов Eliteserien walk-forward (2026-08-02,
                     # engine/backtest_lineup_v1.py + calibrate_alpha_v1.py, огледално
                     # на calibrate_rho.py). var = mean + alpha*mean²; alpha→0 се
                     # доближава до Poisson. Прекалибрирано СЛЕД добавяне на shrinkage
                     # в simulate_lineup_v1.py (0.082→0.046 — shrinkage вече намалява
                     # overdispersion-а от тънки lineup извадки, остава по-малко за NB
                     # да компенсира). Само Eliteserien досега (Allsvenskan няма
                     # достатъчно история за min_hist=30 филтъра) — прекалибрирай при
                     # повече данни/лиги.
MAX_GOALS = 10
LINEUP_CLAMP = (0.75, 1.25)
SHOTS_LINES = (20.5, 23.5, 26.5)


LAM_FLOOR = 1e-6   # log(0) guard — виж score_matrix()


def score_matrix(lam: float, mu: float, rho: float = RHO) -> np.ndarray:
    """ПОПРАВКА (2026-09-04): без долна граница lam/mu<=0 даваше log(0)=-inf
    и цялата матрица излизаше nan (тихо, без изключение — nan-ите просто
    се разнасяха до вероятностите). Освен това DC корекциите 1+lam*rho /
    1+mu*rho стават ОТРИЦАТЕЛНИ при lam или mu над ~8.33 (при RHO=-0.12),
    т.е. отрицателна вероятност за 0:1/1:0 — clip-нати на 0 вместо това.
    И двата случая значат, че нещо нагоре по веригата вече е счупено, но
    по-добре дегенерирал, отколкото nan/отрицателен изход."""
    lam = max(float(lam), LAM_FLOOR)
    mu = max(float(mu), LAM_FLOOR)
    k = np.arange(MAX_GOALS + 1)
    ph = np.exp(k * np.log(lam) - lam - gammaln(k + 1))
    pa = np.exp(k * np.log(mu) - mu - gammaln(k + 1))
    m = np.outer(ph, pa)
    m[0, 0] *= max(0.0, 1 - lam * mu * rho)
    m[0, 1] *= max(0.0, 1 + lam * rho)
    m[1, 0] *= max(0.0, 1 + mu * rho)
    m[1, 1] *= max(0.0, 1 - rho)
    total = m.sum()
    return m / total if total > 0 else m


def match_markets(lam: float, mu: float, rho: float = RHO) -> dict:
    """rho вече е параметър (2026-09-08) — виж rho_for_league() отдолу: всяка
    лига има собствена калибрация, вместо една глобална константа за всички."""
    m = score_matrix(lam, mu, rho=rho)
    i, j = np.indices(m.shape)
    total = i + j
    top = sorted(((m[x, y], x, y) for x in range(7) for y in range(7)),
                 reverse=True)[:3]
    return {"p_home": m[i > j].sum(), "p_draw": np.trace(m),
            "p_away": m[i < j].sum(),
            "p_over25": m[total > 2.5].sum(), "p_under25": m[total < 2.5].sum(),
            "p_btts": m[(i > 0) & (j > 0)].sum(),
            "top_scores": [(f"{x}:{y}", p) for p, x, y in top]}


def joint_markets(lam: float, mu: float, rho: float = RHO) -> dict:
    """Съвместни вероятности "знак И Над 2.5" и "знак И Г/Г" от същата DC матрица (2026-10-26, по
    искане на потребителя: топ 5 на знак+Г/Г и знак+Над 2.5). {"over25": {"1","X","2"}, "btts": {...}}."""
    m = score_matrix(lam, mu, rho=rho)
    i, j = np.indices(m.shape)
    total = i + j
    signs = {"1": i > j, "X": i == j, "2": i < j}
    over, btts = total > 2.5, (i > 0) & (j > 0)
    return {"over25": {k: float(m[v & over].sum()) for k, v in signs.items()},
            "btts": {k: float(m[v & btts].sum()) for k, v in signs.items()}}


def joint_markets_nbinom(lam: float, mu: float, alpha: float = NB_ALPHA) -> dict:
    """Същото като joint_markets(), но върху отрицателно-биномната матрица на Модел В v1."""
    m = score_matrix_nbinom(lam, mu, alpha)
    i, j = np.indices(m.shape)
    total = i + j
    signs = {"1": i > j, "X": i == j, "2": i < j}
    over, btts = total > 2.5, (i > 0) & (j > 0)
    return {"over25": {k: float(m[v & over].sum()) for k, v in signs.items()},
            "btts": {k: float(m[v & btts].sum()) for k, v in signs.items()}}


def nbinom_pmf_vector(mean: float, alpha: float, kmax: int) -> np.ndarray:
    """PMF за k=0..kmax на Negative Binomial (NB2: mean=mean, var=mean+alpha·mean²).
    Пълна gammaln форма (не рекурентната от nbinom_over) — трябва цял вектор,
    не само камулативна опашка над една линия."""
    alpha = max(alpha, 1e-6)
    r = 1.0 / alpha
    p = r / (r + mean)
    k = np.arange(kmax + 1)
    log_pmf = gammaln(k + r) - gammaln(r) - gammaln(k + 1) + r * np.log(p) + k * np.log(1 - p)
    return np.exp(log_pmf)


def score_matrix_nbinom(lam: float, mu: float, alpha: float = NB_ALPHA) -> np.ndarray:
    """Като score_matrix(), но с Negative Binomial маргинали вместо Poisson —
    позволява over-dispersion (var > mean), по-реалистично за гол
    разпределения с по-голяма несигурност (Модел В v1, конкретен състав, не
    сезонна средна). НЕ прилага Dixon-Coles rho корекцията за ниски резултати
    — тя е изведена специфично за Poisson двойки, не пренася се директно.
    Независими NB маргинали, аналогично на некоригираната Poisson основа."""
    ph = nbinom_pmf_vector(lam, alpha, MAX_GOALS)
    pa = nbinom_pmf_vector(mu, alpha, MAX_GOALS)
    m = np.outer(ph, pa)
    return m / m.sum()


def match_markets_nbinom(lam: float, mu: float, alpha: float = NB_ALPHA) -> dict:
    m = score_matrix_nbinom(lam, mu, alpha)
    i, j = np.indices(m.shape)
    total = i + j
    top = sorted(((m[x, y], x, y) for x in range(7) for y in range(7)),
                 reverse=True)[:3]
    return {"p_home": m[i > j].sum(), "p_draw": np.trace(m),
            "p_away": m[i < j].sum(),
            "p_over25": m[total > 2.5].sum(), "p_under25": m[total < 2.5].sum(),
            "p_btts": m[(i > 0) & (j > 0)].sum(),
            "top_scores": [(f"{x}:{y}", p) for p, x, y in top]}


# ---------------------------------------------- per-league rho (2026-09-08)

RHO_CALIB_PATH = DATA_DIR / "rho_calibration.csv"
_RHO_CACHE: dict[str, float] | None = None


def _load_rho_cache() -> dict[str, float]:
    """Чете data/rho_calibration.csv (пише се от calibrate_rho.py). Празен
    речник, ако файлът липсва — тогава rho_for_league() пада на константата
    RHO, т.е. поведението е точно както преди въвеждането на per-league."""
    global _RHO_CACHE
    if _RHO_CACHE is not None:
        return _RHO_CACHE
    _RHO_CACHE = {}
    if RHO_CALIB_PATH.exists():
        with open(RHO_CALIB_PATH, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    _RHO_CACHE[r["league"].strip().lower()] = float(r["rho"])
                except (KeyError, ValueError):
                    continue
    return _RHO_CACHE


def rho_for_league(league: str | None) -> float:
    """rho за конкретната лига, с два fallback-а (огледално на
    build_opta_elo.predict_1x2()'s own-league → pooled 'ALL' логика):
      1. собствен ред на лигата, ако е калибрирана (достатъчно история)
      2. pooled 'ALL' ред (всички лиги наведнъж)
      3. модулната константа RHO, ако още няма никакъв калибрационен файл

    Защо изобщо per-league (по въпрос на потребителя, 2026-09-08): rho мери
    колко по-често/рядко се падат ниски резултати (0:0, 1:0, 0:1, 1:1) спрямо
    чист Poisson. Това е стилова характеристика на лигата (дефанзивна срещу
    офанзивна), не универсална константа — една стойност за 10 различни лиги
    е компромис, а не коректност. Прагът за собствена калибрация е в
    calibrate_rho.py (MIN_RHO_CALIB_N), за да не се доверяваме на шумна
    оценка от малка извадка."""
    cache = _load_rho_cache()
    if not cache:
        return RHO
    if league:
        own = cache.get(league.strip().lower())
        if own is not None:
            return own
    return cache.get("all", RHO)


# ------------------------------------ λ/μ рекалибрация по лига (2026-09-08)

# Един файл на модел (2026-09-08): DC и v1 имат различни наклони, защото
# извеждат λ/μ от различен сигнал (отборен xG срещу играчов xg/xa).
# v2 няма файл — не може да се бектества (yearShift=0 → поглед напред).
LAMBDA_CALIB_PATHS = {
    "dc": DATA_DIR / "lambda_calibration.csv",
    "v1": DATA_DIR / "lambda_calibration_v1.csv",
}
_LAMBDA_CACHE: dict[str, dict[str, tuple[float, float, float, float]]] = {}


def _load_lambda_cache(model: str) -> dict[str, tuple[float, float, float, float]]:
    if model in _LAMBDA_CACHE:
        return _LAMBDA_CACHE[model]
    cache: dict[str, tuple[float, float, float, float]] = {}
    path = LAMBDA_CALIB_PATHS.get(model)
    if path and path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    cache[r["league"].strip().lower()] = (
                        float(r["a_home"]), float(r["b_home"]),
                        float(r["a_away"]), float(r["b_away"]))
                except (KeyError, ValueError):
                    continue
    _LAMBDA_CACHE[model] = cache
    return cache


def calibrate_lambda_mu(lam: float, mu: float, league: str | None,
                        model: str = "dc") -> tuple[float, float]:
    """Линейна рекалибрация на очакваните голове — виж calibrate_lambda.py.

    ЗАЩО (2026-09-08): регресията на реалните голове спрямо прогнозните дава
    наклон 0.37-0.87 вместо 1.00 във ВСЯКА лига — λ/μ са около два пъти
    по-разпръснати от реалността, моделът надценява силните и подценява
    слабите отбори. Измерено извън извадката: +7 logL и +3.4пп точност на Г/Г
    върху 745 тестови мача. Далеч по-голям ефект от per-league rho.

    Без lambda_calibration.csv връща стойностите непроменени — точно
    поведението отпреди въвеждането на рекалибрацията."""
    cache = _load_lambda_cache(model)
    if not cache:
        return lam, mu
    c = (cache.get(league.strip().lower()) if league else None) or cache.get("all")
    if c is None:
        return lam, mu
    a_h, b_h, a_a, b_a = c
    return max(a_h + b_h * lam, LAM_FLOOR), max(a_a + b_a * mu, LAM_FLOOR)


def poisson_over(lam: float, line: float) -> float:
    lam = max(float(lam), LAM_FLOOR)   # виж score_matrix() — същия log(0) guard
    k = np.arange(int(np.ceil(line)))
    return float(1 - np.exp(k * np.log(lam) - lam - gammaln(k + 1)).sum())


def nbinom_over(mu: float, alpha: float, line: float) -> float:
    """P(X > line) за Negative Binomial с mean=mu, var=mu+alpha·mu². Рекурентна
    PMF (P(0)=p^r, P(k)=P(k-1)·(k+r-1)/k·(1-p)), без gammaln, за да съвпада
    1:1 с JS порта в make_team_report.py/make_edge_table.py."""
    alpha = max(alpha, 1e-6)
    r = 1.0 / alpha
    p = r / (r + mu)
    kmax = int(np.ceil(line))
    pmf = p ** r
    cum = pmf
    for k in range(1, kmax):
        pmf *= (k + r - 1) / k * (1 - p)
        cum += pmf
    return float(1 - cum)


def fair(p: float) -> str:
    return f"{1 / p:6.2f}" if p > 0.001 else "     —"
