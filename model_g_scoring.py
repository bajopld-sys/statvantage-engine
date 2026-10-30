"""Модел Г — точков модел (100 точки общо), по спецификация на потребителя
(2026-08-06): 6 фактора, всеки дава АБСОЛЮТНИ точки на домакин и гост
(за разлика от Модел Б, който сравнява РАНГОВЕ — тук е сурова точкова сума).

Фактори и тежести:
  Форма (последни 5 мача)        25
  Домакинство                    20
  Гол баланс (последни 5 мача)   20
  Състав/контузии                15  — АВТОМАТИЧНО от брой отсъстващи титуляри на живи
                                      мачове (2026-08-09, виж squad_points()); РЪЧЕН вход
                                      (--home-squad/--away-squad) го измества, ако е подаден
  Психология/мотивация           10  — АВТОМАТИЧНО от последните 5 мача (2026-08-09, виж
                                      psychology_points()); РЪЧЕН вход (--home-psych/
                                      --away-psych) го измества, ако е подаден
  H2H (последни 5 преки срещи)   10

За backtest (по избор на потребителя, 2026-08-06): Състав/контузии се дава НЕУТРАЛНО (7.5/7.5)
— тества се обективната част, защото няма ИСТОРИЧЕСКА injury информация в базата (Sofascore's
injury списък е само за ТЕКУЩИ/бъдещи мачове). За жив мач Състав/контузии вече се смята
автоматично от next_fixtures.py (брой отсъстващи ОБИЧАЙНИ титуляри, виж
lineup_impact.missing_regular_starters). Психология/мотивация (2026-08-09, по искане на потребителя:
"пак се гледат последните 5 мача, ако домакинът има 3W1D1L ще считаме че имат настроение и
мотивация") — автоматично от резултатите в последните 5 мача (W=3/D=1/L=0 точки, дял от
PSYCH_MAX), работи еднакво в backtest и на живо (за разлика от Състав/контузии, тук ИМА
историческа информация — резултатите от мачове). Ръчен вход продължава да измества
автоматиката, ако е подаден изрично.

1X2 превръщане (стъпка 3 от спецификацията на потребителя): точките на
двата отбора се нормализират спрямо сумата им, после фиксиран DRAW_SHARE
(20%, средата на предложения от потребителя диапазон 20-25%) отива за
равенство, а останалите 80% се делят пропорционално между домакин/гост.

Головият баланс (10 +/- 1.5т/гол, ограничено в [0,20]) е РАЗУМНА
ИНТЕРПОЛАЦИЯ на примерите на потребителя (+3 гола -> ~14т, -2 гола -> ~8т) —
той не даде точна формула, само илюстрация, затова числата тук са мое
попълване на празнината, не буквална негова спецификация.

Употреба:
    python engine/model_g_scoring.py --league ELITESERIEN --home "SK Brann" --away Rosenborg
    python engine/model_g_scoring.py --league ELITESERIEN --round 19
    python engine/model_g_scoring.py --league ELITESERIEN --backtest 30   # walk-forward от кръг 30, срещу Модел Б
"""
import argparse
import sys
from pathlib import Path
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_b_heuristic import (LEAGUES, load_matches, load_h2h_all, filter_h2h,
                               team_matches, result, PTS, all_teams,
                               analyze as analyze_b)
from dixon_coles import fair

# Калибрирани (2026-08-06) чрез calibrate_model_g.py - координатно-извиващо
# решетъчно търсене (Brier score) върху пул от 225 walk-forward мача
# (Eliteserien r12+/r4+, Allsvenskan r4+). Стойностите опряха в ръба на
# решетката дори след удвояването й (сигнал за overfitting върху малка
# извадка) - Brier се подобри (0.6299->0.6146), но пулната точност леко
# спадна (50.2%->49.8%), с разнопосочни резултати по прозорец (Eliteserien
# r4+ +2.0pp, Eliteserien r12+ -5.8pp, Allsvenskan r4+ -1.0pp). Потребителят
# реши изрично да ги приеме заради подобрението при r4+ ("щом от кръг 4 на
# сам се е вдигнал % значи е добре") - виж пълния анализ преди тази промяна.
DRAW_SHARE_BASE = 0.17
DRAW_SHARE_SWING = 0.24  # обхват на динамичната корекция около базата (виж draw_share())

FORM_MAX = 25
FORM_RECENT_W, FORM_OLDER_W = 0.15, 0.85  # калибрирано - по-старите 2 мача сега тежат повече
HOME_ADV_MAX = 20
HOME_ADV_DIVISOR = 1.0  # виж home_advantage_points() - калибрирано, почти бинарно домакинско предимство
GOAL_BAL_MAX = 20
GOAL_BAL_SCALE = 6.0  # т./гол адаптирана разлика, виж goal_balance_points() - калибрирано, сатурира бързо
SQUAD_MAX = 15
SQUAD_DIVISOR = 6.0  # виж squad_points() - начална, некалибрирана стойност (огледално
                     # на HOME_ADV_DIVISOR преди калибрация)
SQUAD_DEFAULT_RATING = 6.8  # типичен среден Sofascore мачов рейтинг - fallback тегло
                            # за отсъстващ играч без изчислен рейтинг (виж _missing_weight())
PSYCH_MAX = 10
H2H_MAX = 10


def form_points(matches: list[dict], team: str, recent_w: float = FORM_RECENT_W) -> float:
    """Последните 5 мача, но НЕ равнопоставени (2026-08-06, поправка #2) -
    последните 3 тежат `recent_w`, по-старите 2 тежат (1-recent_w), огледално
    на Модел Б's form_score() - победа отпреди 5 мача не бива да тежи колкото
    победа миналата седмица. `recent_w` е калибрируем (виж calibrate_model_g.py)."""
    last5 = team_matches(matches, team)[-5:]
    if not last5:
        return FORM_MAX / 2  # без история - неутрално, не 0
    last3 = last5[-3:]
    older2 = last5[:-3] if len(last5) > 3 else []
    p3 = sum(PTS[result(m, team)] for m in last3) / (3 * max(len(last3), 1))
    p2 = sum(PTS[result(m, team)] for m in older2) / (3 * len(older2)) if older2 else p3
    return (recent_w * p3 + (1 - recent_w) * p2) * FORM_MAX


def team_goal_averages(matches: list[dict], team: str) -> tuple[float, float]:
    """Средно вкарани/допуснати за отбора от ВСИЧКИ негови мачове до момента
    (не само последните 5) - служи като "типична сила" референция, когато
    ИМЕННО ТОЗИ отбор е бил опонент на някого другиго в goal_balance_points().
    Без история -> ~1.3 (типична стойност гол/мач в европейски лиги), не 0."""
    tm = team_matches(matches, team)
    if not tm:
        return 1.3, 1.3
    gf = sum(m["hg"] if m["home"] == team else m["ag"] for m in tm)
    ga = sum(m["ag"] if m["home"] == team else m["hg"] for m in tm)
    return gf / len(tm), ga / len(tm)


def goal_balance_points(matches: list[dict], team: str, scale: float = GOAL_BAL_SCALE) -> float:
    """Opponent-adjusted (2026-08-06, поправка #1, замества плоското GF-GA) -
    за всеки от последните 5 мача сравнява реалния резултат с ТИПИЧНОТО
    очаквано срещу конкретния опонент (неговата средна допусната/вкарана до
    момента), не самата бройка голове. 5-0 срещу отбор, който обичайно
    допуска 3, тежи по-малко от 1-0 срещу отбор, който обичайно допуска 0.3
    — точно сигналът, който opponent-adjusted xG вече доказа, че бие плоската
    собствена честота при по-ранните backtest-ове в този проект.
    `scale` (т./гол адаптирана разлика) е калибрируем (виж calibrate_model_g.py)."""
    last5 = team_matches(matches, team)[-5:]
    if not last5:
        return GOAL_BAL_MAX / 2
    total_adj = 0.0
    for m in last5:
        opp = m["away"] if m["home"] == team else m["home"]
        gf = m["hg"] if m["home"] == team else m["ag"]
        ga = m["ag"] if m["home"] == team else m["hg"]
        opp_avg_scored, opp_avg_conceded = team_goal_averages(matches, opp)
        total_adj += (gf - opp_avg_conceded) + (opp_avg_scored - ga)
    avg_adj = total_adj / len(last5)
    pts = GOAL_BAL_MAX / 2 + avg_adj * scale
    return max(0.0, min(float(GOAL_BAL_MAX), pts))


def home_advantage_points(matches: list[dict], home: str, neutral_venue: bool = False,
                          divisor: float = HOME_ADV_DIVISOR) -> tuple[float, float]:
    """Тим-специфично (2026-08-06, поправка #3, замества фиксирано 20/0) -
    от РЕАЛНАТА разлика точки/мач у дома срещу навън на ДОМАКИНА, не
    абстрактна константа еднаква за всички. Отбор с голяма реална домакинска
    разлика получава по-голям дял от 20-те точки; отбор, който играе почти
    еднакво навсякъде, получава близо до 10/10. `divisor` е калибрируем
    (виж calibrate_model_g.py)."""
    if neutral_venue:
        return 15.0, 5.0
    home_matches = [m for m in matches if m["home"] == home]
    away_matches = [m for m in matches if m["away"] == home]
    if not home_matches or not away_matches:
        return float(HOME_ADV_MAX), 0.0  # недостатъчно данни - стандартното предимство
    home_ppg = sum(PTS[result(m, home)] for m in home_matches) / len(home_matches)
    away_ppg = sum(PTS[result(m, home)] for m in away_matches) / len(away_matches)
    diff = home_ppg - away_ppg  # типично положително, но не задължително (има изключения)
    share = max(0.0, min(1.0, 0.5 + diff / divisor))  # PPG обхват е [0,3], diff обхват грубо [-3,3]
    return share * HOME_ADV_MAX, (1 - share) * HOME_ADV_MAX


def h2h_points(h2h_all: list[dict], home: str, away: str, before_date: str) -> tuple[float, float]:
    """Последните 5 преки срещи (без значение кой е бил домакин тогава)."""
    rows = filter_h2h(h2h_all, home, away, before_date=before_date)[:5]
    if not rows:
        return H2H_MAX / 2, H2H_MAX / 2
    home_pts = away_pts = 0.0
    for r in rows:
        if r["hg"] > r["ag"]:
            winner = r["home"]
        elif r["ag"] > r["hg"]:
            winner = r["away"]
        else:
            winner = None
        if winner is None:
            home_pts += 1
            away_pts += 1
        elif winner == home:
            home_pts += 3
        elif winner == away:
            away_pts += 3
    n = len(rows)
    return home_pts / (3 * n) * H2H_MAX, away_pts / (3 * n) * H2H_MAX


def _missing_weight(missing: list[dict] | None) -> float:
    """Сума от тежестите на отсъстващите титуляри - всеки играч тежи
    rating/SQUAD_DEFAULT_RATING (2026-08-09, по искане на потребителя: "да
    тежи по рейтинга на самите отсъстващи... загубата на 8.0-рейтингов играч
    > загубата на 6.5-рейтингов"). Играч без изчислен рейтинг (None) тежи
    точно 1.0 (все едно е точно средният играч) - запазва старото поведение
    (чист брой) за играчи без достатъчно оценени мачове. Нормализирано около
    1.0 за СРЕДЕН играч, за да остане SQUAD_DIVISOR смислен спрямо старата,
    чисто бройна версия (не се налага нов дивизор)."""
    if not missing:
        return 0.0
    return sum((m.get("rating") or SQUAD_DEFAULT_RATING) / SQUAD_DEFAULT_RATING for m in missing)


def squad_points(home_val: float | None, away_val: float | None,
                 missing_home: list[dict] | None = None, missing_away: list[dict] | None = None,
                 divisor: float = SQUAD_DIVISOR) -> tuple[float, float]:
    """Ръчен вход (0..SQUAD_MAX всеки) печели предимство, ако е подаден изрично
    (--home-squad/--away-squad) - потребителска преценка бие автоматика.

    Иначе (2026-08-09, по искане на потребителя: "тук вече в Модел Г можем да
    оправим Състав/контузии да дава тежести", после "да тежи по рейтинга на
    самите отсъстващи") - АВТОМАТИЧНО от КАЧЕСТВОТО (не само броя) на
    отсъстващите ОБИЧАЙНИ титуляри (next_fixtures.py's missing_home/
    missing_away, вече филтрирани и с прикачен рейтинг от ЕДНО зареждане
    през lineup_impact.missing_regular_starters) - виж _missing_weight(). Огледално
    на home_advantage_points(): диференциал в тежест -> дял от SQUAD_MAX,
    `divisor` калибрируем (виж calibrate_model_g.py). Без ръчен вход И без
    данни за липсващи (backtest - няма историческа injury информация в
    базата) -> неутрално SQUAD_MAX/2 за двата, както преди."""
    if home_val is not None or away_val is not None:
        h = SQUAD_MAX / 2 if home_val is None else max(0.0, min(float(SQUAD_MAX), home_val))
        a = SQUAD_MAX / 2 if away_val is None else max(0.0, min(float(SQUAD_MAX), away_val))
        return h, a
    home_w, away_w = _missing_weight(missing_home), _missing_weight(missing_away)
    if home_w == 0 and away_w == 0:
        return SQUAD_MAX / 2, SQUAD_MAX / 2
    diff = away_w - home_w  # положително = домакинът е по-комплектован/по-леко засегнат
    share = max(0.0, min(1.0, 0.5 + diff / divisor))
    return share * SQUAD_MAX, (1 - share) * SQUAD_MAX


def _psych_from_form(matches: list[dict], team: str) -> float:
    """Последните 5 мача, W=3/D=1/L=0 точки, дял от PSYCH_MAX (макс. 15 точки
    възможни от 5 победи) - огледално на h2h_points()'s W-D-L->точки схема.
    Без история -> неутрално PSYCH_MAX/2, не 0."""
    last5 = team_matches(matches, team)[-5:]
    if not last5:
        return PSYCH_MAX / 2
    pts = sum(PTS[result(m, team)] for m in last5)
    return pts / (3 * len(last5)) * PSYCH_MAX


def psychology_points(matches: list[dict], home: str, away: str,
                      home_val: float | None = None,
                      away_val: float | None = None) -> tuple[float, float]:
    """Ръчен вход печели предимство, ако е подаден изрично (--home-psych/
    --away-psych) - потребителска преценка бие автоматика.

    Иначе (2026-08-09, по искане на потребителя: "пак се гледат последните 5
    мача. Ако от последните 5 мача домакина има 3W1D1L ще считаме че имат
    настроение и мотивация и обратното") - АВТОМАТИЧНО от резултатите в
    последните 5 мача на всеки отбор, виж _psych_from_form(). За разлика от
    Състав/контузии, тук работи и в backtest (резултатите от мачове ги имаме
    исторически, за разлика от injury данните)."""
    if home_val is not None or away_val is not None:
        h = PSYCH_MAX / 2 if home_val is None else max(0.0, min(float(PSYCH_MAX), home_val))
        a = PSYCH_MAX / 2 if away_val is None else max(0.0, min(float(PSYCH_MAX), away_val))
        return h, a
    return _psych_from_form(matches, home), _psych_from_form(matches, away)


def draw_share(total_home: float, total_away: float, base: float = DRAW_SHARE_BASE,
               swing: float = DRAW_SHARE_SWING) -> float:
    """Динамично (2026-08-06, поправка #4, замества фиксираните 20%) - расте
    когато отборите са близки по точки (изравнени мачове реално дърпат
    повече равенства), намалява при голяма разлика. Обхват грубо
    [base-swing/2, base+swing/2] = [12%, 28%] с базовите стойности.
    `base`/`swing` са калибрируеми (виж calibrate_model_g.py)."""
    grand = total_home + total_away
    if grand == 0:
        return base
    closeness = 1 - abs(total_home - total_away) / grand  # 1.0 = идентични точки
    return base + (closeness - 0.5) * swing


def analyze(matches: list[dict], h2h_all: list[dict], home: str, away: str,
           before_date: str, *, neutral_venue: bool = False,
           home_squad: float | None = None, away_squad: float | None = None,
           missing_home: list[dict] | None = None, missing_away: list[dict] | None = None,
           home_psych: float | None = None, away_psych: float | None = None,
           form_recent_w: float = FORM_RECENT_W, goal_bal_scale: float = GOAL_BAL_SCALE,
           home_adv_divisor: float = HOME_ADV_DIVISOR, draw_base: float = DRAW_SHARE_BASE,
           draw_swing: float = DRAW_SHARE_SWING, squad_divisor: float = SQUAD_DIVISOR) -> dict:
    f_h, f_a = form_points(matches, home, form_recent_w), form_points(matches, away, form_recent_w)
    v_h, v_a = home_advantage_points(matches, home, neutral_venue, home_adv_divisor)
    g_h, g_a = (goal_balance_points(matches, home, goal_bal_scale),
               goal_balance_points(matches, away, goal_bal_scale))
    s_h, s_a = squad_points(home_squad, away_squad, missing_home, missing_away, squad_divisor)
    p_h, p_a = psychology_points(matches, home, away, home_psych, away_psych)
    hh_h, hh_a = h2h_points(h2h_all, home, away, before_date)

    total_home = f_h + v_h + g_h + s_h + p_h + hh_h
    total_away = f_a + v_a + g_a + s_a + p_a + hh_a
    grand = total_home + total_away

    raw_home = total_home / grand if grand else 0.5
    raw_away = total_away / grand if grand else 0.5
    p_draw = draw_share(total_home, total_away, draw_base, draw_swing)
    p_home = raw_home * (1 - p_draw)
    p_away = raw_away * (1 - p_draw)

    # Праг за "lean" (2026-08-09, по искане на потребителя — виж SIDE_PICK_THRESHOLD
    # в record_predictions.py, огледално тук за панела "lean: X" в отчета): страна
    # се "избира" само ако реално мине 41%, иначе "X" (равен) като fallback, дори да
    # не е реално най-високото число.
    if p_home > 0.41 or p_away > 0.41:
        pick = "1" if p_home >= p_away else "2"
    else:
        pick = "X"

    return {
        "factors_home": {"форма": f_h, "домакинство": v_h, "гол_баланс": g_h,
                        "състав": s_h, "психология": p_h, "h2h": hh_h, "общо": total_home},
        "factors_away": {"форма": f_a, "домакинство": v_a, "гол_баланс": g_a,
                        "състав": s_a, "психология": p_a, "h2h": hh_a, "общо": total_away},
        "p_home": p_home, "p_draw": p_draw, "p_away": p_away, "pick_1x2": pick,
    }


def _load_h2h_safe(db_path: Path) -> list[dict]:
    """load_h2h_all(), но толерантно към лиги без h2h_matches таблица изобщо
    (напр. Allsvenskan - H2H fetch никога не е пускан там) - празен списък
    вместо гръмване, h2h_points() вече дефолтва на неутрално при 0 срещи."""
    try:
        return load_h2h_all(db_path)
    except Exception:
        return []


def actual_result_str(m: dict) -> str:
    return "1" if m["hg"] > m["ag"] else ("2" if m["ag"] > m["hg"] else "X")


def predict_matches(matches_all: list[dict], h2h_all: list[dict], start_round: int,
                    **g_params) -> list[dict]:
    """Walk-forward Модел Г предсказания за всички мачове от start_round
    нататък. `**g_params` се препращат директно в analyze() (form_recent_w,
    goal_bal_scale, home_adv_divisor, draw_base, draw_swing) - използва се и
    от backtest_from_round(), и от calibrate_model_g.py (обективна функция)."""
    rounds = sorted({m["round"] for m in matches_all if m["round"] >= start_round})
    out = []
    for rd in rounds:
        fixtures = [m for m in matches_all if m["round"] == rd]
        prior = [m for m in matches_all if m["round"] < rd]
        for fx in fixtures:
            g_res = analyze(prior, h2h_all, fx["home"], fx["away"], before_date=fx["date"],
                           **g_params)
            out.append({"res": g_res, "actual": actual_result_str(fx)})
    return out


def backtest_from_round(db_path: Path, start_round: int) -> None:
    """Walk-forward сравнение Модел Г срещу Модел Б върху ЕДНИ и същи мачове
    (директно съпоставими числа, не различни исторически прозорци)."""
    matches_all = load_matches(db_path)
    goal_events_all = {}  # Модел Б трябва goal_events за flag-овете, не за pick_1x2 - празно е ОК тук
    h2h_all = _load_h2h_safe(db_path)

    g_preds = predict_matches(matches_all, h2h_all, start_round)
    g_hit = sum(p["res"]["pick_1x2"] == p["actual"] for p in g_preds)
    total = len(g_preds)

    rounds = sorted({m["round"] for m in matches_all if m["round"] >= start_round})
    b_hit = 0
    for rd in rounds:
        fixtures = [m for m in matches_all if m["round"] == rd]
        prior = [m for m in matches_all if m["round"] < rd]
        for fx in fixtures:
            b_res = analyze_b(prior, goal_events_all, h2h_all, fx["home"], fx["away"],
                              before_date=fx["date"], verbose=False)
            b_hit += b_res["pick_1x2"] == actual_result_str(fx)

    print(f"\n{'='*60}\nОБОБЩЕНИЕ ({total} мача, от кръг {start_round}), 1X2 точност:")
    print(f"  Модел Г (точков):     {g_hit}/{total}  ({g_hit/total:.1%})")
    print(f"  Модел Б (ранг, диагностичен): {b_hit}/{total}  ({b_hit/total:.1%})")
    print(f"\n(За справка: Dixon-Coles на исторически 320-мачов EPL backtest дава ~49% —")
    print(f" различен data window, не директно сравнимо число с горните две, само ориентир.)")


def print_single(res: dict, home: str, away: str) -> None:
    print(f"{home} vs {away}\n")
    print(f"{'Фактор':<14} {home:>10} {away:>10}")
    for key, label in (("форма", "Форма"), ("домакинство", "Домакинство"),
                       ("гол_баланс", "Гол баланс"), ("състав", "Състав"),
                       ("психология", "Психология"), ("h2h", "H2H")):
        print(f"{label:<14} {res['factors_home'][key]:>10.1f} {res['factors_away'][key]:>10.1f}")
    print(f"{'-'*36}")
    print(f"{'Общо':<14} {res['factors_home']['общо']:>10.1f} {res['factors_away']['общо']:>10.1f}")
    print(f"\n1={res['p_home']:.1%} ({fair(res['p_home'])})  "
         f"X={res['p_draw']:.1%} ({fair(res['p_draw'])})  "
         f"2={res['p_away']:.1%} ({fair(res['p_away'])})")
    print(f"Pick: {res['pick_1x2']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--home")
    ap.add_argument("--away")
    ap.add_argument("--round", type=int)
    ap.add_argument("--backtest", type=int, metavar="START_ROUND")
    ap.add_argument("--neutral-venue", action="store_true")
    ap.add_argument("--home-squad", type=float, default=None)
    ap.add_argument("--away-squad", type=float, default=None)
    ap.add_argument("--home-missing", type=int, default=0,
                    help="брой отсъстващи титуляри на домакина, без известен рейтинг "
                         "(тежат SQUAD_DEFAULT_RATING всеки) - игнорира се, ако --home-squad е подаден")
    ap.add_argument("--away-missing", type=int, default=0)
    ap.add_argument("--home-psych", type=float, default=None)
    ap.add_argument("--away-psych", type=float, default=None)
    args = ap.parse_args()

    # CLI-то подава само БРОЙ (няма имена/рейтинги под ръка тук) - синтетични
    # записи с rating=None, squad_points()/_missing_weight() ги тежи по
    # SQUAD_DEFAULT_RATING всеки (все едно е точно средният играч).
    missing_home_cli = [{"rating": None}] * args.home_missing
    missing_away_cli = [{"rating": None}] * args.away_missing

    # model_b_heuristic.LEAGUES няма Allsvenskan (позната липса) - fallback на
    # изчислен път, огледално на next_fixtures.py, вместо твърд речник.
    db_path = LEAGUES.get(args.league.upper()) or (DATA_DIR / f"sofa_{args.league.lower()}.db")
    if not db_path.exists():
        raise SystemExit(f"Няма база за '{args.league}' ({db_path}).")

    if args.backtest is not None:
        backtest_from_round(db_path, args.backtest)
        return

    matches_all = load_matches(db_path)
    h2h_all = _load_h2h_safe(db_path)

    if args.round is not None:
        fixtures = [m for m in matches_all if m["round"] == args.round]
        if not fixtures:
            raise SystemExit(f"Няма мачове в кръг {args.round}.")
        for fx in fixtures:
            prior = [m for m in matches_all if m["round"] < args.round]
            res = analyze(prior, h2h_all, fx["home"], fx["away"], before_date=fx["date"],
                         neutral_venue=args.neutral_venue,
                         home_squad=args.home_squad, away_squad=args.away_squad,
                         missing_home=missing_home_cli, missing_away=missing_away_cli,
                         home_psych=args.home_psych, away_psych=args.away_psych)
            print_single(res, fx["home"], fx["away"])
            print()
        return

    if not args.home or not args.away:
        raise SystemExit("Трябва --home/--away, --round, или --backtest.")

    latest_date = max((m["date"] for m in matches_all), default="9999-12-31")
    res = analyze(matches_all, h2h_all, args.home, args.away, before_date=latest_date,
                 neutral_venue=args.neutral_venue,
                 home_squad=args.home_squad, away_squad=args.away_squad,
                 missing_home=missing_home_cli, missing_away=missing_away_cli,
                 home_psych=args.home_psych, away_psych=args.away_psych)
    print_single(res, args.home, args.away)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
