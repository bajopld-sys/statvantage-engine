"""OPTA-style хибриден Elo рейтинг — 7-ми модел, 2026-08-24 (по искане на потребителя,
след проучване на публичните Opta Power Rankings: "модифициран Elo, 80% класически
(резултат + margin of victory) + 20% xG-компонент, генериран от probability distribution
на правдоподобни резултати").

Точната формула на Opta е комерсиална тайна (потвърдено от потребителя при проучването
— няма публикувана xG-претеглителна функция), затова тук е адаптация с два реални,
документирани градивни елемента вместо гадаене:

1. "Класическа" Elo част (80% тегло) — margin-of-victory multiplier по конвенцията на
   eloratings.net's World Football Elo Ratings (най-цитираната реална MoV Elo система
   във футбола, вероятно самата база, от която Opta тръгва):
       G(gd) = 1                  ако |гол разлика| <= 1   (вкл. равенство)
             = 1.5                ако |гол разлика| == 2
             = (11+|гол разлика|)/8   ако |гол разлика| >= 3
   K_ефективно = K * G(gd).

2. xG-претеглена част (20% тегло) — вместо действителния резултат (1/0.5/0), ползва
   ОЧАКВАНАТА стойност на резултата според РЕАЛНОТО създадено xG в мача (не Elo/форма
   прогноза преди мача, а какво отборите РЕАЛНО създадоха): подава истинските xG_home/
   xG_away на мача (от `stats` таблицата, period='full') през вече калибрирания
   Dixon-Coles score matrix (dixon_coles.match_markets(), RHO=-0.12) → получава
   p_home_xG/p_draw_xG/p_away_xG → S_xG = p_home_xG*1 + p_draw_xG*0.5 + p_away_xG*0.
   Точно механизмът, който потребителят описа за Opta: "компенсира лош late finishing
   или лош късмет" — отбор, който е създал повече/по-качествени положения, но не е
   отбелязал (лош late finishing), получава Elo движение по-близко до "би трябвало да
   победи", вместо изцяло по действителния резултат.

И двете части сравняват срещу СЪЩОТО очаквано E_home (стандартна Elo логистична
формула, HOME_ADV точки добавени само за тази сметка) — комбинират се линейно:

    elo_change = K * [W_ACTUAL * G(gd) * (S_actual - E_home)
                     + W_XG * (S_xG - E_home)]

Приложено симетрично: elo_home += elo_change, elo_away -= elo_change (валидно, защото
S_away = 1-S_actual, E_away = 1-E_home за същия мач).

**Обхват (съзнателно ограничен, виж дискусията с потребителя 2026-08-24):** ПО ЛИГА, не
cross-league сравним — за истинска Opta-стил глобална 0-100 скала трябват десетилетия
международни/cross-league данни (3.3M мача от 1990 при реалния Opta), които нямаме.
Всеки отбор стартира на BASE_ELO=1500 при първата си поява в дадената лига база.
Обработва се хронологично СТРОГО по дата (не по `round`, за да не зависи от
пренасрочени мачове) — walk-forward safe by construction: E_home/E_away за всеки мач
се смятат САМО от рейтинги, изградени от предходни мачове, никога поглед напред.

xG покритие: 100% екипно xG на ниво мач (stats таблица) потвърдено във ВСИЧКИ 10
активни лиги към 2026-08-24 (за разлика от play-level xG, който липсва за Allsvenskan/
LaLiga2/Faroe — виж [[opta-engine-project]] паметта). Ако за някой мач все пак липсва
xG (защитно, не очаквано в момента), xG частта отпада и цялото тегло минава към
класическата MoV част (W_ACTUAL ефективно става 1.0 за този мач).

Изход (в data/, не profiles/ — този engine няма profiles/ конвенция):
  data/opta_elo_current.csv   league,team,elo           (текущ рейтинг)
  data/opta_elo_history.csv   league,date,round,home,away,elo_home_pre,elo_away_pre,
                               e_home,s_actual,s_xg,elo_change,hg,ag,xg_home,xg_away
                               (walk-forward snapshot ПРЕДИ всеки мач — за честен бектест)

Употреба:
    python engine/build_opta_elo.py
    python engine/build_opta_elo.py --backtest   (принтира accuracy по confidence bucket)
"""
import argparse
import csv
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dixon_coles import match_markets, rho_for_league


K = 20.0
HOME_ADV = 65.0
BASE_ELO = 1500.0
W_ACTUAL = 0.80
W_XG = 0.20
XG_FLOOR = 0.05  # предпазва Poisson log(0) при мач с записано 0.0 xG за отбор

LEAGUES = [
    "eliteserien", "allsvenskan", "bundesliga", "bundesliga2", "championship", "belgiumpl",
    "laliga", "laliga2", "epl", "ligue1", "seriea",
]


def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(elo_a - elo_b) / 400.0))


def mov_multiplier(goal_diff: int) -> float:
    """eloratings.net World Football Elo конвенция — виж докстринга по-горе."""
    gd = abs(goal_diff)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11 + gd) / 8.0


def xg_implied_score(xg_home: float, xg_away: float, league: str | None = None) -> float:
    lam = max(xg_home, XG_FLOOR)
    mu = max(xg_away, XG_FLOOR)
    m = match_markets(lam, mu, rho=rho_for_league(league))
    return m["p_home"] * 1.0 + m["p_draw"] * 0.5 + m["p_away"] * 0.0


def read_matches(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT match_id, date, round, home, away, hg, ag FROM matches "
        "WHERE hg IS NOT NULL AND ag IS NOT NULL ORDER BY date, match_id").fetchall()
    xg = {}
    for r in con.execute("SELECT match_id, team, xg FROM stats WHERE period='full'"):
        xg[(r["match_id"], r["team"])] = r["xg"]
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["xg_home"] = xg.get((d["match_id"], d["home"]))
        d["xg_away"] = xg.get((d["match_id"], d["away"]))
        out.append(d)
    return out


def run_league(league: str, matches: list[dict]) -> tuple[dict, list[dict]]:
    ratings: dict[str, float] = {}
    history = []
    for m in matches:
        home, away = m["home"], m["away"]
        elo_h = ratings.setdefault(home, BASE_ELO)
        elo_a = ratings.setdefault(away, BASE_ELO)
        e_home = expected_score(elo_h + HOME_ADV, elo_a)

        hg, ag = m["hg"], m["ag"]
        s_actual = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        gmult = mov_multiplier(hg - ag)

        xg_h, xg_a = m["xg_home"], m["xg_away"]
        if xg_h is not None and xg_a is not None:
            s_xg = xg_implied_score(xg_h, xg_a, league)
            w_actual, w_xg = W_ACTUAL, W_XG
        else:
            s_xg = None
            w_actual, w_xg = 1.0, 0.0

        elo_change = K * (w_actual * gmult * (s_actual - e_home)
                          + w_xg * ((s_xg - e_home) if s_xg is not None else 0.0))

        history.append({
            "league": league, "date": m["date"], "round": m["round"],
            "home": home, "away": away,
            "elo_home_pre": round(elo_h, 2), "elo_away_pre": round(elo_a, 2),
            "e_home": round(e_home, 4), "s_actual": s_actual,
            "s_xg": round(s_xg, 4) if s_xg is not None else "",
            "elo_change": round(elo_change, 3),
            "hg": hg, "ag": ag,
            "xg_home": xg_h if xg_h is not None else "",
            "xg_away": xg_a if xg_a is not None else "",
        })

        ratings[home] = elo_h + elo_change
        ratings[away] = elo_a - elo_change

    return ratings, history


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def _fit_dr_margin(dr: np.ndarray, margin: np.ndarray) -> tuple[float, float, float]:
    """Нисковнивов OLS fit, споделен между fit_calibration() (production,
    вижда цялата история — легитимно, "сега" реално включва всичко досега
    известно) и backtest()'s walk-forward refit (само минали мачове спрямо
    оценявания). Защитно връща неутрална калибрация вместо NaN/крак при <2
    точки (напр. пресен checkout без данни все още — виж бъга, докладван
    от потребителя 2026-08-24)."""
    if len(dr) < 2:
        return 0.0, 0.0, 1.0
    X = np.column_stack([np.ones_like(dr), dr])
    coef, *_ = np.linalg.lstsq(X, margin, rcond=None)
    a, b = coef
    resid = margin - (a + b * dr)
    std = float(np.std(resid))
    s = std * math.sqrt(3) / math.pi
    return float(a), float(b), max(s, 1e-6)


def fit_calibration(history: list[dict]) -> tuple[float, float, float]:
    """OLS: гол маргин ~ a + b*elo_diff (пул от ВСИЧКИ лиги — само за 1X2 калибрация,
    не за cross-league рейтинг сравнимост, вижте докстринга горе). Logistic scale s от
    остатъчната дисперсия, огледално на старата StatVantage 2026-27 elo_model.py.
    ЗА PRODUCTION (predict_1x2()/write_calibration_csv()) — тук е коректно да гледа
    ЦЯЛАТА досегашна история, "сега" легитимно я вижда изцяло. За честна ОЦЕНКА на
    минала точност вижте backtest() по-долу — той НЕ ползва тази функция директно,
    refit-ва expanding-window, за да няма lookahead в самата калибрация."""
    dr = np.array([(h["elo_home_pre"] + HOME_ADV) - h["elo_away_pre"] for h in history], dtype=float)
    margin = np.array([h["hg"] - h["ag"] for h in history], dtype=float)
    return _fit_dr_margin(dr, margin)


def logistic_cdf(x: float, mu: float, s: float) -> float:
    return 1.0 / (1.0 + math.exp(-(x - mu) / s))


def elo_1x2_probs(elo_home: float, elo_away: float, a: float, b: float, s: float) -> tuple[float, float, float]:
    dr = (elo_home + HOME_ADV) - elo_away
    mu = a + b * dr
    p_home = 1.0 - logistic_cdf(0.5, mu, s)
    p_away = logistic_cdf(-0.5, mu, s)
    p_draw = max(0.0, 1.0 - p_home - p_away)
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


MIN_LEAGUE_CALIB_N = 30  # под този брой мачове per-league калибрацията е твърде
                        # шумна (вижте докстринга горе) — пада на pooled "ALL" ред


def write_calibration_csv(path: Path, all_history: list[dict]) -> None:
    """Per-league калибрация, с pooled ('ALL') ред като fallback за лиги под
    MIN_LEAGUE_CALIB_N мача — предстоящите фикстури в тези лиги (повечето от
    10-те активни към 2026-08-24) четат 'ALL' реда, докато натрупат история."""
    rows = []
    a, b, s = fit_calibration(all_history)
    rows.append({"league": "ALL", "a": a, "b": b, "s": s, "n": len(all_history)})
    by_league: dict[str, list] = {}
    for h in all_history:
        by_league.setdefault(h["league"], []).append(h)
    for league, hist in by_league.items():
        if len(hist) >= MIN_LEAGUE_CALIB_N:
            a, b, s = fit_calibration(hist)
            rows.append({"league": league, "a": a, "b": b, "s": s, "n": len(hist)})
    write_csv(path, ["league", "a", "b", "s", "n"], rows)


_CURRENT_CACHE: dict[tuple[str, str], float] | None = None
_CALIB_CACHE: dict[str, tuple[float, float, float]] | None = None


def _load_caches() -> None:
    """ПОПРАВКА (2026-09-04): липсващ CSV (build_opta_elo.py още не е пускан)
    хвърляше FileNotFoundError, а липсващ 'ALL' ред по-долу — TypeError при
    разопаковането. И двете се поглъщаха от try/except-а в next_fixtures.py и
    се показваха в отчета като подвеждащото "отбор още няма изигран мач тази
    сезон". Сега кешовете просто остават празни и predict_1x2() връща None с
    ясна причина."""
    global _CURRENT_CACHE, _CALIB_CACHE
    if _CURRENT_CACHE is not None:
        return
    _CURRENT_CACHE = {}
    cur_path = DATA_DIR / "opta_elo_current.csv"
    if cur_path.exists():
        with open(cur_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                _CURRENT_CACHE[(r["league"], r["team"])] = float(r["elo"])
    _CALIB_CACHE = {}
    calib_path = DATA_DIR / "opta_elo_calibration.csv"
    if calib_path.exists():
        with open(calib_path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                _CALIB_CACHE[r["league"]] = (float(r["a"]), float(r["b"]), float(r["s"]))


def predict_1x2(league: str, home: str, away: str) -> dict | None:
    """За next_fixtures.py — league е показваното име от make_team_report.py's
    LEAGUES (напр. "Eliteserien"/"SerieA"), тук се сравнява lower()-нато
    срещу вътрешните ключове на build_opta_elo.py. Връща None, ако някой от
    отборите няма елo рейтинг още (напр. новобранец в базата, никога не е
    минал през run_league())."""
    _load_caches()
    key_league = league.lower()
    elo_h = _CURRENT_CACHE.get((key_league, home))
    elo_a = _CURRENT_CACHE.get((key_league, away))
    if elo_h is None or elo_a is None:
        return None
    calib = _CALIB_CACHE.get(key_league) or _CALIB_CACHE.get("ALL")
    if calib is None:
        return None  # няма калибрация още (празен/липсващ opta_elo_calibration.csv)
    a, b, s = calib
    p_home, p_draw, p_away = elo_1x2_probs(elo_h, elo_a, a, b, s)
    return {"p_home": p_home, "p_draw": p_draw, "p_away": p_away,
           "elo_home": round(elo_h, 1), "elo_away": round(elo_a, 1)}


def backtest(all_history: list[dict]) -> None:
    """Истински walk-forward бектест (2026-08-24, поправка по искане на потребителя
    след code review) — калибрацията (a,b,s) се REFIT-ва на ВСЯКА стъпка, само от
    мачове с ПО-РАННА дата от оценявания (глобално подредени по дата, през всички
    лиги), не еднократно върху целия датасет. Старата версия fit-ваше калибрацията
    ЕДНОКРАТНО върху ВСИЧКИ мачове, после я ползваше и за оценка на тези същите
    мачове — лек in-sample "leak" (елo рейтингите САМИ бяха walk-forward-safe, но
    калибрационният слой, който ги превръща в 1X2 %, беше видял бъдещето). Логиката
    огледава predict_1x2()'s избор (own-league калибрация, ако има >=
    MIN_LEAGUE_CALIB_N предходни мача от СЪЩАТА лига, иначе pooled 'ALL' от всички
    лиги предходни мачове) — само дето тук "предходни" значи СТРОГО преди текущия
    мач по дата, не "всички някога", за честна извадка.

    ЗАБЕЛЕЖКА: production пътят (predict_1x2()/write_calibration_csv(), реалните
    прогнози за предстоящи мачове) НЕ се променя от този фикс — там "сега"
    легитимно вижда цялата досегашна история, няма lookahead проблем по дефиниция
    (предсказваме бъдещ мач, не преоценяваме минал)."""
    ordered = sorted(all_history, key=lambda h: (h["date"], h["league"], h["home"]))

    pooled_pts: list[tuple[float, float]] = []
    per_league_pts: dict[str, list[tuple[float, float]]] = {}
    picks_all: list[tuple[float, bool]] = []
    picks_by_league: dict[str, list[tuple[float, bool]]] = {}

    for h in ordered:
        league = h["league"]
        lg_pts = per_league_pts.setdefault(league, [])
        dr_point = (h["elo_home_pre"] + HOME_ADV) - h["elo_away_pre"]
        margin_point = float(h["hg"] - h["ag"])

        if len(lg_pts) >= MIN_LEAGUE_CALIB_N:
            src = lg_pts
        elif len(pooled_pts) >= MIN_LEAGUE_CALIB_N:
            src = pooled_pts
        else:
            src = None  # още недостатъчно ИСТОРИЯ изобщо за смислена калибрация

        if src is not None:
            a, b, s = _fit_dr_margin(np.array([p[0] for p in src]), np.array([p[1] for p in src]))
            p_h, p_d, p_a = elo_1x2_probs(h["elo_home_pre"], h["elo_away_pre"], a, b, s)
            probs = {"H": p_h, "D": p_d, "A": p_a}
            k = max(probs, key=probs.get)
            actual = "H" if h["hg"] > h["ag"] else ("A" if h["ag"] > h["hg"] else "D")
            pick = (probs[k], actual == k)
            picks_all.append(pick)
            picks_by_league.setdefault(league, []).append(pick)

        pooled_pts.append((dr_point, margin_point))
        lg_pts.append((dr_point, margin_point))

    def report(label: str, picks: list[tuple[float, bool]]) -> None:
        n = len(picks)
        if n == 0:
            return
        wins = sum(1 for _, w in picks if w)
        print(f"{label:16}: {wins}/{n} = {wins/n:.1%}")
        for lo, hi, name in [(0.33, 0.45, "33-45%"), (0.45, 0.55, "45-55%"),
                             (0.55, 0.65, "55-65%"), (0.65, 1.01, "65%+")]:
            sub = [(c, w) for c, w in picks if lo <= c < hi]
            if sub:
                w = sum(1 for _, won in sub if won)
                print(f"    {name:8} {w}/{len(sub)} = {w/len(sub):.0%}")

    print(f"\n1X2 ФАВОРИТ (Opta-style Elo, ИСТИНСКИ walk-forward — калибрацията "
          f"refit-ната на всяка стъпка само от ПО-РАННИ мачове, own-league ако "
          f">={MIN_LEAGUE_CALIB_N}, иначе pooled 'ALL' до момента):")
    report("ВСИЧКИ ЛИГИ", picks_all)
    for league in LEAGUES:
        picks = picks_by_league.get(league, [])
        if picks:
            print()
            report(league, picks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backtest", action="store_true", help="принтира accuracy по confidence bucket")
    args = ap.parse_args()

    all_current = []
    all_history = []
    for league in LEAGUES:
        db_path = DATA_DIR / f"sofa_{league}.db"
        if not db_path.exists():
            print(f"Пропускам {league} — липсва {db_path}")
            continue
        matches = read_matches(db_path)
        if not matches:
            continue
        ratings, history = run_league(league, matches)
        all_history.extend(history)
        for team, elo in ratings.items():
            all_current.append({"league": league, "team": team, "elo": round(elo, 2)})
        n_xg = sum(1 for h in history if h["s_xg"] != "")
        print(f"{league:14} {len(matches):4} мача обработени  ({n_xg}/{len(matches)} с xG-претегляне)")

    write_csv(DATA_DIR / "opta_elo_current.csv", ["league", "team", "elo"], all_current)
    write_csv(DATA_DIR / "opta_elo_history.csv",
              ["league", "date", "round", "home", "away", "elo_home_pre", "elo_away_pre",
               "e_home", "s_actual", "s_xg", "elo_change", "hg", "ag", "xg_home", "xg_away"],
              all_history)
    write_calibration_csv(DATA_DIR / "opta_elo_calibration.csv", all_history)
    print(f"\nЗаписано: {len(all_current)} отборни рейтинга, {len(all_history)} мача в историята")

    if args.backtest:
        backtest(all_history)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
