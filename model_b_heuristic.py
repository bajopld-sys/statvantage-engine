"""Модел Б — паралелен, независим модел (spec на потребителя), само за сравнение
с Dixon-Coles модела (simulate_sofa.py / make_team_report.py). Не участва в HTML
report-а, чисто конзолен/CSV изход. Никакви букмейкърски коефициенти в изчислението.

Метод (изграден итеративно с потребителя, виж разговора за пълния spec):

1X2 — 3 групи, всяка сравнява ранг на домакин срещу ранг на гост в съответно
      league-wide класиране:
  a) Форма — последни 5 (вътре: последни 3 = 60%, други 2 = 40%) + последни 10
     (W-D-L точки). Съгласие между 5 и 10 = силен сигнал; разминаване = неутрално
     (освен ако единия гап е много по-голям — тогава лек lean към него).
  b) Домакин/Гост — точки, venue-специфично (само домакинските/гостуващите мачове), цял сезон.
  c) Полувремена (1-во и 2-ро) — голове + голове/мач, ОБЩО за сезона (не venue-специфично —
     реалната референтна таблица на потребителя показва всички мачове, не само домакин/гост).
     Swap между половините → флаг "обръщалка", сила = min(гап1, гап2).

  H2H (до 10 преки срещи, последни 5 = 60%, стари 5 = 40%, тежест = min(1, брой/10)):
    - при 1-1-неутрално мнозинство → H2H решава посоката
    - при 2-1/3-0 мнозинство → H2H (и swap флагът) само модулират увереността
      (съгласен → "твърд", несъгласен → "лек превес")

Над2.5 / ГГ:
  1. Собствен ранг на всеки отбор в съответното класиране (честота Над2.5 / ГГ)
     — близки рангове (гап <= бр.отбори/3) = съгласие = директен сигнал
  2. Venue-специфична форма за вкарване на гол (домакин у дома, гост навън) — винаги проверявана
  3. При голям гап (разминаване) → H2H между конкретните 2 отбора (over/BTTS честота в
     преките им срещи, същата тежест min(1,бр./10)) решава

Флагове (информативни, не влизат в основната математика):
  - "Обръщалка" — swap в полувремената (сила по гап)
  - "Склонност към обръщалки" — >=1 мач този сезон с поведа >=2 гола (по всяко време
    от мача, чрез goal_events), но без победа

Употреба:
    python engine/model_b_heuristic.py --league ELITESERIEN --home "Sarpsborg 08" --away HamKam
    python engine/model_b_heuristic.py --league ELITESERIEN --round 19
"""
import argparse
import sqlite3
from pathlib import Path

from config import DATA_DIR

LEAGUES = {
    "EPL": DATA_DIR / "sofa.db",
    "LALIGA": DATA_DIR / "sofa_laliga.db",
    "BUNDESLIGA": DATA_DIR / "sofa_bundesliga.db",
    "LIGUE1": DATA_DIR / "sofa_ligue1.db",
    "ELITESERIEN": DATA_DIR / "sofa_eliteserien.db",
}

H2H_RECENT_W, H2H_OLDER_W = 0.8, 0.2  # калибрирано чрез grid-search (виж calibrate_model_b)
FORM5_LAST3_W, FORM5_OTHER2_W = 0.6, 0.4
AGREEMENT_FRACTION = 1 / 3  # гап <= бр.отбори * тази фракция => "съгласие"
MARKET_NEUTRAL_BAND = 0.15  # ширина на неутралната зона около средата на
# класирането, като дял от броя отбори: среден ранг в рамките на ±0.15*n от
# средата => няма реален сигнал, не се взима страна за Над2.5/ГГ.
#
# Калибрирано ЕМПИРИЧНО (2026-09-04), не аналитично: прекият математически
# аналог на OU_BTTS_THRESHOLD=0.55 (5пп от 50%) е band=0.05, но той филтрира
# едва 0-6% от мачовете, докато v1/v2 реално пропускат 19-30% (проверено в
# model_predictions.db). Причината е, че средният ранг на два отбора рядко
# пада точно около средата — ранговете са дискретни и разпръснати, докато
# вероятностите се трупат около 50%. 0.15 изравнява СЕЛЕКТИВНОСТТА (~20-25%
# пропуснати), за да е сравнението на hit rate между моделите честно.
# Забележка: това изравнява само дела пропуснати, НЕ е оптимизирано по
# точност — не е backtest-калибрирана стойност в смисъла на RHO/NB_ALPHA.
#
# Измерено на последните 8 кръга Eliteserien (64 мача, 2026-09-04):
#   band  Над2.5 пропуснати/точност   ГГ пропуснати/точност
#   0.15        8% / 73%                  27% / 60%     <- избрано
#   0.18       19% / 73%                  41% / 63%
#   0.20       31% / 75%                  53% / 63%
# 0.15 е компромисът: вкарва ГГ в целевия диапазон (19-30%), без да изхвърля
# половината пикове. По-стегнатите ленти купуват само +2-3пп точност срещу
# драстично по-малка извадка. Над2.5 остава по-слабо филтриран, защото
# ранговете там реално се разделят повече — не е дефект, а свойство на сигнала.


# --------------------------------------------------------------------- данни

def load_matches(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT match_id, date, round, home, away, hg, ag, home_ht, away_ht, sofa_event_id "
        "FROM matches WHERE hg IS NOT NULL AND ag IS NOT NULL ORDER BY date"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def load_goal_events(db_path: Path) -> dict[str, list[dict]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT match_id, minute, is_home, home_score, away_score "
        "FROM goal_events ORDER BY match_id, minute"
    ).fetchall()
    con.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["match_id"], []).append(dict(r))
    return out


def load_h2h_all(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT team_a, team_b, date, home, away, hg, ag FROM h2h_matches"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def filter_h2h(h2h_all: list[dict], team_a: str, team_b: str,
              before_date: str | None = None) -> list[dict]:
    rows = [r for r in h2h_all
           if {r["team_a"], r["team_b"]} == {team_a, team_b}
           and (before_date is None or r["date"] < before_date)]
    return sorted(rows, key=lambda r: r["date"], reverse=True)


def all_teams(matches: list[dict]) -> list[str]:
    return sorted({m["home"] for m in matches} | {m["away"] for m in matches})


def team_matches(matches: list[dict], team: str) -> list[dict]:
    return [m for m in matches if m["home"] == team or m["away"] == team]


def result(m: dict, team: str) -> str:
    gf, ga = (m["hg"], m["ag"]) if m["home"] == team else (m["ag"], m["hg"])
    return "W" if gf > ga else ("L" if gf < ga else "D")


PTS = {"W": 3, "D": 1, "L": 0}


# ---------------------------------------------------------------- класирания

def rank_table(scores: dict[str, float]) -> dict[str, int]:
    """По-висок score = по-добър ранг (1 = най-добър). Равни score стойности
    получават ЕДИН и същ ранг. ПОПРАВКА (2026-07): преди sorted() над
    азбучно подредения all_teams() списък (stable sort) тихо даваше на
    по-ранния по азбучен ред отбор по-добър ранг при всяко реално
    равенство — систематично в кръг 1 (всички входове 0 — form/venue/half
    все още нямат история) и всеки път когато два отбора реално са
    изравнени по-нататък в сезона."""
    uniq_sorted = sorted(set(scores.values()), reverse=True)
    rank_of_value = {v: i + 1 for i, v in enumerate(uniq_sorted)}
    return {team: rank_of_value[score] for team, score in scores.items()}


def form_score(matches: list[dict], team: str) -> tuple[float, float]:
    """(форма5_score, форма10_score) — score5 е weighted точки/мач (0..3),
    score10 е общо точки от последните 10."""
    tm = team_matches(matches, team)[-10:]
    last10 = tm
    last5 = tm[-5:]
    if not last5:
        return 0.0, 0.0
    last3 = last5[-3:]
    other2 = last5[:-3] if len(last5) > 3 else []
    p3 = sum(PTS[result(m, team)] for m in last3) / max(len(last3), 1)
    p2 = sum(PTS[result(m, team)] for m in other2) / max(len(other2), 1) if other2 else p3
    score5 = FORM5_LAST3_W * p3 + FORM5_OTHER2_W * p2
    score10 = sum(PTS[result(m, team)] for m in last10)
    return score5, score10


def venue_points(matches: list[dict], team: str, venue: str) -> float:
    """venue: 'home' или 'away' — точки само от мачовете в тази роля."""
    tm = [m for m in matches if m[venue] == team]
    return sum(PTS[result(m, team)] for m in tm)


def venue_wdl(matches: list[dict], team: str, venue: str) -> tuple[int, int, int]:
    """W-D-L само от мачовете в тази роля (домакин/гост) — по искане на
    потребителя (2026-08-01), за да се вижда представянето зад точките,
    не само класирането по ранг."""
    tm = [m for m in matches if m[venue] == team]
    w = d = l = 0
    for m in tm:
        r = result(m, team)
        if r == "W":
            w += 1
        elif r == "D":
            d += 1
        else:
            l += 1
    return w, d, l


def season_points(matches: list[dict], team: str) -> int:
    """Точки за ЦЯЛ сезон (домакин+гост) — база за общото класиране, за да
    можем да делим venue-специфичните мачове по сила на съперника."""
    return sum(PTS[result(m, team)] for m in team_matches(matches, team))


def table_ranks(matches: list[dict]) -> dict[str, int]:
    """Общо класиране (1 = първо място) по точки за целия сезон, всички
    отбори — използва се само за да преценим кой съперник е 'над' или
    'под' дадения отбор, не влиза пряко в 1X2/Над2.5/ГГ математиката."""
    teams = all_teams(matches)
    pts = {t: season_points(matches, t) for t in teams}
    return rank_table(pts)


def venue_wdl_by_tier(matches: list[dict], team: str, venue: str,
                      own_rank: int, ranks: dict[str, int]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Разбива venue-специфичния W-D-L на 'срещу съперници над' (по-добър
    ранг, число < own_rank) и 'срещу съперници под' (число > own_rank) —
    по искане на потребителя (2026-08-01, конкретен пример: Бран бие
    отборите под него у дома, но има проблем срещу отборите над него)."""
    tm = [m for m in matches if m[venue] == team]

    def wdl(rows: list[dict]) -> tuple[int, int, int]:
        w = d = l = 0
        for m in rows:
            r = result(m, team)
            if r == "W":
                w += 1
            elif r == "D":
                d += 1
            else:
                l += 1
        return w, d, l

    above, below = [], []
    for m in tm:
        opp = m["away"] if venue == "home" else m["home"]
        opp_rank = ranks.get(opp)
        if opp_rank is None:
            continue
        (above if opp_rank < own_rank else below).append(m)
    return wdl(above), wdl(below)


def half_goals(matches: list[dict], team: str, half: int) -> tuple[float, float]:
    """(общо голове, голове/мач) на отбора в дадено полувреме, ЦЯЛ сезон, всички мачове."""
    tm = team_matches(matches, team)
    total, played = 0, 0
    for m in tm:
        if m["home_ht"] is None or m["away_ht"] is None:
            continue
        played += 1
        if half == 1:
            gf = m["home_ht"] if m["home"] == team else m["away_ht"]
        else:
            ft_gf = m["hg"] if m["home"] == team else m["ag"]
            ht_gf = m["home_ht"] if m["home"] == team else m["away_ht"]
            gf = ft_gf - ht_gf
        total += gf
    return total, (total / played if played else 0.0)


def venue_goals_scored_avg(matches: list[dict], team: str, venue: str) -> float:
    tm = [m for m in matches if m[venue] == team]
    if not tm:
        return 0.0
    gf = sum((m["hg"] if m["home"] == team else m["ag"]) for m in tm)
    return gf / len(tm)


def over25_rate(matches: list[dict], team: str) -> tuple[int, int]:
    tm = team_matches(matches, team)
    over = sum(1 for m in tm if m["hg"] + m["ag"] > 2)
    return over, len(tm)


def btts_rate(matches: list[dict], team: str) -> tuple[int, int]:
    tm = team_matches(matches, team)
    both = sum(1 for m in tm if m["hg"] > 0 and m["ag"] > 0)
    return both, len(tm)


# --------------------------------------------------------------------- H2H

def h2h_weight(n: int) -> float:
    return min(1.0, n / 10)


def h2h_win_lean(h2h: list[dict], home: str, away: str) -> tuple[str | None, float]:
    """Връща (кой води, тежест). h2h е сортиран DESC по дата (най-нови първи)."""
    if not h2h:
        return None, 0.0
    recent5, older5 = h2h[:5], h2h[5:10]

    def wins(rows, team):
        return sum(1 for r in rows if result(r, team) == "W")

    score_home = H2H_RECENT_W * wins(recent5, home) + H2H_OLDER_W * wins(older5, home)
    score_away = H2H_RECENT_W * wins(recent5, away) + H2H_OLDER_W * wins(older5, away)
    w = h2h_weight(len(h2h))
    if score_home > score_away:
        return home, w
    if score_away > score_home:
        return away, w
    return None, w


def h2h_over_btts_rate(h2h: list[dict]) -> tuple[float, float, int]:
    """Претеглен процент (последни 5 = H2H_RECENT_W, стари 5 = H2H_OLDER_W),
    същия принцип като h2h_win_lean — преди го смяташе плоско, без тежест."""
    if not h2h:
        return 0.0, 0.0, 0
    recent5, older5 = h2h[:5], h2h[5:10]

    def rate(rows, pred):
        return sum(1 for r in rows if pred(r)) / len(rows) if rows else None

    over_pred = lambda r: r["hg"] + r["ag"] > 2
    both_pred = lambda r: r["hg"] > 0 and r["ag"] > 0

    def blend(rows_recent, rows_older, pred):
        rec = rate(rows_recent, pred)
        old = rate(rows_older, pred)
        if old is None:
            return rec if rec is not None else 0.0
        return H2H_RECENT_W * rec + H2H_OLDER_W * old

    n = len(h2h)
    return (blend(recent5, older5, over_pred),
           blend(recent5, older5, both_pred), n)


# ------------------------------------------------------------------- флагове

def blown_lead_flag(matches: list[dict], team: str, goal_events: dict) -> int:
    """Брой мачове тази година, в които отборът е повеждал с >=2 гола (по всяко
    време), но накрая не е спечелил."""
    count = 0
    for m in team_matches(matches, team):
        events = goal_events.get(m["match_id"])
        final = result(m, team)
        if final == "W":
            continue
        max_lead = 0
        if events:
            for ev in events:
                margin = ev["home_score"] - ev["away_score"]
                team_margin = margin if m["home"] == team else -margin
                max_lead = max(max_lead, team_margin)
        else:
            ht_margin = (m["home_ht"] - m["away_ht"]) if m["home"] == team else (m["away_ht"] - m["home_ht"])
            max_lead = max(max_lead, ht_margin or 0)
        if max_lead >= 2:
            count += 1
    return count


def halves_swap_flag(h1_home, h1_away, h2_home, h2_away):
    """(home по-добър 1-во И away по-добър 2-ро) или обратното → swap.
    Връща (флаг: bool, по_силна_страна: 'home'/'away'/None, сила: 'силен'/'слаб')."""
    home_better_1h = h1_home < h1_away
    away_better_2h = h2_away < h2_home
    home_better_2h = h2_home < h2_away
    away_better_1h = h1_away < h1_home

    gap1 = abs(h1_home - h1_away)
    gap2 = abs(h2_home - h2_away)

    if home_better_1h and away_better_2h:
        strong_side = "away" if gap2 > gap1 else "home"
        strength = "силен" if min(gap1, gap2) >= 5 else "слаб"
        return True, strong_side, strength, gap1, gap2
    if away_better_1h and home_better_2h:
        strong_side = "home" if gap2 > gap1 else "away"
        strength = "силен" if min(gap1, gap2) >= 5 else "слаб"
        return True, strong_side, strength, gap1, gap2
    return False, None, None, gap1, gap2


# ----------------------------------------------------------------- анализ

def analyze(matches: list[dict], goal_events: dict, h2h_all: list[dict],
           home: str, away: str, before_date: str | None = None, verbose: bool = True) -> dict:
    """verbose=True (CLI, поведение непроменено) принтира всичко като преди.
    Резултатният dict вече носи ВСИЧКИ междинни числа (не само финалните
    picks) — по искане на потребителя (2026-08-01), за да може целият
    диагностичен панел да се рендира и в HTML отчета, не само в конзолата."""
    teams = all_teams(matches)
    n = len(teams)

    if verbose:
        print(f"\n{'='*60}\n{home}  (домакин)   срещу   {away}  (гост)\n{'='*60}")

    # ---- H2H ----
    h2h = filter_h2h(h2h_all, home, away, before_date)
    h2h_lean, h2h_w = h2h_win_lean(h2h, home, away)
    h2h_role = "home" if h2h_lean == home else ("away" if h2h_lean == away else None)
    h2h_over, h2h_btts, h2h_n = h2h_over_btts_rate(h2h)
    if verbose:
        print(f"\nH2H ({h2h_n} срещи, тежест={h2h_w:.0%}): "
              f"lean={h2h_lean or 'няма'}  Над2.5={h2h_over:.0%}  ГГ={h2h_btts:.0%}")

    # ---- Флагове ----
    home_blown = blown_lead_flag(matches, home, goal_events)
    away_blown = blown_lead_flag(matches, away, goal_events)
    if verbose:
        if home_blown:
            print(f"⚑ ФЛАГ: {home} има {home_blown} мач(а) с поведа >=2 гола без победа")
        if away_blown:
            print(f"⚑ ФЛАГ: {away} има {away_blown} мач(а) с поведа >=2 гола без победа")

    # ---- Форма ----
    form_scores5 = {t: form_score(matches, t)[0] for t in teams}
    form_scores10 = {t: form_score(matches, t)[1] for t in teams}
    r5, r10 = rank_table(form_scores5), rank_table(form_scores10)
    h5, a5 = r5[home], r5[away]
    h10, a10 = r10[home], r10[away]
    form5_winner = "home" if h5 < a5 else ("away" if a5 < h5 else None)
    form10_winner = "home" if h10 < a10 else ("away" if a10 < h10 else None)
    if form5_winner == form10_winner and form5_winner:
        form_winner, form_note = form5_winner, "съгласие 5≈10"
    else:
        gap5, gap10 = abs(h5 - a5), abs(h10 - a10)
        if gap5 > gap10 * 1.5 and form5_winner:
            form_winner, form_note = form5_winner, f"разминаване, лек lean по 5-те (гап {gap5} срещу {gap10})"
        elif gap10 > gap5 * 1.5 and form10_winner:
            form_winner, form_note = form10_winner, f"разминаване, лек lean по 10-те (гап {gap10} срещу {gap5})"
        else:
            form_winner, form_note = None, "разминаване → неутрално"
    if verbose:
        print(f"\nФорма: 5 мача — {home}={h5}, {away}={a5} | 10 мача — {home}={h10}, {away}={a10}")
        print(f"  → {form_note}, winner={form_winner}")

    # ---- Домакин/Гост ----
    home_pts = {t: venue_points(matches, t, "home") for t in teams}
    away_pts = {t: venue_points(matches, t, "away") for t in teams}
    rh, ra = rank_table(home_pts), rank_table(away_pts)
    hr, ar = rh[home], ra[away]
    ha_winner = "home" if hr < ar else ("away" if ar < hr else None)
    home_wdl = venue_wdl(matches, home, "home")
    away_wdl = venue_wdl(matches, away, "away")
    if verbose:
        print(f"\nДомакин/Гост: {home}(домакин)={hr} [{home_wdl[0]}-{home_wdl[1]}-{home_wdl[2]} у дома]  "
              f"срещу  {away}(гост)={ar} [{away_wdl[0]}-{away_wdl[1]}-{away_wdl[2]} навън]  → winner={ha_winner}")

    # ---- Разбивка по сила на съперника (2026-08-01) ----
    overall_ranks = table_ranks(matches)
    home_overall_rank = overall_ranks.get(home)
    away_overall_rank = overall_ranks.get(away)
    h_above = h_below = a_above = a_below = None
    relevant_side = relevant_wdl = relevant_note = None
    if home_overall_rank is not None:
        h_above, h_below = venue_wdl_by_tier(matches, home, "home", home_overall_rank, overall_ranks)
        if verbose:
            print(f"  {home} у дома срещу над него={h_above[0]}-{h_above[1]}-{h_above[2]}  "
                  f"срещу под него={h_below[0]}-{h_below[1]}-{h_below[2]}  (общо класиране: #{home_overall_rank})")
    if away_overall_rank is not None:
        a_above, a_below = venue_wdl_by_tier(matches, away, "away", away_overall_rank, overall_ranks)
        if verbose:
            print(f"  {away} навън срещу над него={a_above[0]}-{a_above[1]}-{a_above[2]}  "
                  f"срещу под него={a_below[0]}-{a_below[1]}-{a_below[2]}  (общо класиране: #{away_overall_rank})")
    # Директно релевантно за ТОЗИ мач: съперникът над или под е?
    if home_overall_rank is not None and away_overall_rank is not None:
        if away_overall_rank < home_overall_rank:
            relevant_side = "above"
            relevant_wdl = venue_wdl_by_tier(matches, home, "home", home_overall_rank, overall_ranks)[0]
            relevant_note = f"⚑ {away} е НАД {home} в класирането"
        else:
            relevant_side = "below"
            relevant_wdl = venue_wdl_by_tier(matches, home, "home", home_overall_rank, overall_ranks)[1]
            relevant_note = f"{away} е ПОД {home} в класирането"
        if verbose:
            w, d, l = relevant_wdl
            print(f"  {relevant_note} — релевантна форма на {home} у дома срещу такива съперници: {w}-{d}-{l}")

    # ---- Полувремена ----
    h1 = {t: half_goals(matches, t, 1)[1] for t in teams}
    h2 = {t: half_goals(matches, t, 2)[1] for t in teams}
    r_h1, r_h2 = rank_table(h1), rank_table(h2)
    h1h, a1h = r_h1[home], r_h1[away]
    h2h_h, h2h_a = r_h2[home], r_h2[away]
    swap, strong_side, strength, gap1, gap2 = halves_swap_flag(h1h, a1h, h2h_h, h2h_a)
    if swap:
        half_winner = None
        if verbose:
            print(f"\nПолувремена: 1-во — {home}={h1h}, {away}={a1h} | 2-ро — {home}={h2h_h}, {away}={h2h_a}")
            print(f"  ⚑ ФЛАГ ОБРЪЩАЛКА ({strength}) — по-силна страна: {strong_side} "
                  f"(гап1={gap1}, гап2={gap2})")
    else:
        avg_h = (h1h + h2h_h) / 2
        avg_a = (a1h + h2h_a) / 2
        half_winner = "home" if avg_h < avg_a else ("away" if avg_a < avg_h else None)
        if verbose:
            print(f"\nПолувремена: 1-во — {home}={h1h}, {away}={a1h} | 2-ро — {home}={h2h_h}, {away}={h2h_a}")
            print(f"  → без swap, winner={half_winner}")

    # ---- 1X2 (само диагностично — backtest на 3 лиги показа 44% навсякъде,
    # системно под Dixon-Coles (48-51%). Не се препоръчва като pick, само
    # групите по-горе остават полезни за контекст на самия мач.) ----
    votes = [v for v in (form_winner, ha_winner, half_winner) if v]
    home_votes = votes.count("home")
    away_votes = votes.count("away")
    if home_votes == away_votes:
        direction = h2h_role
        conf = "H2H tie-breaker" if h2h_role else "нерешено (X?)"
    else:
        direction = "home" if home_votes > away_votes else "away"
        disagree = (h2h_role and h2h_role != direction) or (swap and strong_side and strong_side != direction)
        conf = "лек превес" if disagree else "твърд"
    pick = {"home": "1", "away": "2", None: "X"}[direction]
    if verbose:
        print(f"\n(диагностично, НЕ препоръчан pick) 1X2: {pick}  ({conf})  "
             f"[гласове: домакин={home_votes}, гост={away_votes}] — за 1X2 ползвай Dixon-Coles")

    # ---- Над2.5 / ГГ ----
    over_counts = {t: over25_rate(matches, t) for t in teams}
    btts_counts = {t: btts_rate(matches, t) for t in teams}
    over_scores = {t: (c / m if m else 0) for t, (c, m) in over_counts.items()}
    btts_scores = {t: (c / m if m else 0) for t, (c, m) in btts_counts.items()}
    r_over, r_btts = rank_table(over_scores), rank_table(btts_scores)
    ho, ao = r_over[home], r_over[away]
    hb, ab = r_btts[home], r_btts[away]
    gap_thresh = max(1, round(n * AGREEMENT_FRACTION))

    def market_call(rh_, ra_, h2h_rate, label):
        """ПОПРАВКА (2026-09-04, по искане на потребителя): преди ВИНАГИ
        връщаше "ДА"/"НЕ", без праг на увереност — за разлика от DC/v1/v2,
        които записват pick само над OU_BTTS_THRESHOLD=0.55 (виж
        record_predictions.py). Заради това Модел Б влизаше в track record-а
        и с чисти ези-тура повиквания (среден ранг точно около средата на
        класирането), а сравнението на hit rate между моделите беше ябълки
        към круши. Сега неутралната зона (± MARKET_NEUTRAL_BAND от средата)
        връща pick=None — мачът просто не се брои за този пазар."""
        gap = abs(rh_ - ra_)
        agree = gap <= gap_thresh
        avg_rank = (rh_ + ra_) / 2
        midpoint = n / 2
        band = MARKET_NEUTRAL_BAND * n  # 0.05*n рангови позиции = аналог на 0.55 праг
        if not agree and h2h_n:
            # H2H fallback — тук прагът е директен, върху самата честота
            if abs(h2h_rate - 0.5) <= MARKET_NEUTRAL_BAND:
                base, src = None, f"H2H твърде близо до 50% ({h2h_rate:.0%}) — без pick"
            else:
                base = "ДА" if h2h_rate >= 0.5 else "НЕ"
                src = f"H2H fallback ({h2h_rate:.0%})"
        elif abs(avg_rank - midpoint) <= band:
            base = None
            src = (f"среден ранг {avg_rank:.1f} твърде близо до средата "
                   f"({midpoint:.1f} ± {band:.1f}) — без pick")
        else:
            base = "ДА" if avg_rank <= midpoint else "НЕ"
            src = "съгласие в ранговете" if agree else "разминаване, без H2H данни"
        if verbose:
            print(f"{label}: {home}={rh_}, {away}={ra_} (гап={gap}) → {base or '—'}  [{src}]")
        return {"pick": base, "home_rank": rh_, "away_rank": ra_, "gap": gap, "source": src}

    if verbose:
        print()
    over_result = market_call(ho, ao, h2h_over, "Над2.5")
    btts_result = market_call(hb, ab, h2h_btts, "ГГ")

    home_goals_avg = venue_goals_scored_avg(matches, home, "home")
    away_goals_avg = venue_goals_scored_avg(matches, away, "away")
    if verbose:
        print(f"\nVenue-специфична форма за вкарване: "
              f"{home}(у дома)={home_goals_avg:.2f} гола/мач  |  "
              f"{away}(навън)={away_goals_avg:.2f} гола/мач")

    return {
        "home": home, "away": away,
        "h2h": {"n": h2h_n, "weight": h2h_w, "lean": h2h_lean, "over25_pct": h2h_over, "btts_pct": h2h_btts},
        "flags": {"home_blown": home_blown, "away_blown": away_blown},
        "form": {"home_rank5": h5, "away_rank5": a5, "home_rank10": h10, "away_rank10": a10,
                "winner": form_winner, "note": form_note},
        "venue": {"home_rank": hr, "away_rank": ar, "home_wdl": home_wdl, "away_wdl": away_wdl,
                 "winner": ha_winner},
        "venue_tier": {"home_overall_rank": home_overall_rank, "away_overall_rank": away_overall_rank,
                      "home_above": h_above, "home_below": h_below,
                      "away_above": a_above, "away_below": a_below,
                      "relevant_side": relevant_side, "relevant_wdl": relevant_wdl, "relevant_note": relevant_note},
        "halves": {"home_rank_h1": h1h, "away_rank_h1": a1h, "home_rank_h2": h2h_h, "away_rank_h2": h2h_a,
                  "swap": swap, "strong_side": strong_side, "strength": strength, "winner": half_winner},
        "pick_1x2": pick, "confidence": conf, "votes_home": home_votes, "votes_away": away_votes,
        "over25": over_result, "btts": btts_result,
        "venue_goals": {"home_avg": home_goals_avg, "away_avg": away_goals_avg},
    }


def actual_result_str(m: dict) -> str:
    pick = "1" if m["hg"] > m["ag"] else ("2" if m["ag"] > m["hg"] else "X")
    over = "ДА" if m["hg"] + m["ag"] > 2 else "НЕ"
    btts = "ДА" if m["hg"] > 0 and m["ag"] > 0 else "НЕ"
    return f"{pick}  Над2.5={over}  ГГ={btts}  ({m['hg']}:{m['ag']})"


def backtest_from_round(db_path: Path, start_round: int, csv_out: Path | None = None) -> None:
    matches_all = load_matches(db_path)
    goal_events_all = load_goal_events(db_path)
    h2h_all = load_h2h_all(db_path)

    rounds = sorted({m["round"] for m in matches_all if m["round"] >= start_round})
    stats = {"total": 0, "1x2_hit": 0, "over_hit": 0, "btts_hit": 0,
             "over_n": 0, "btts_n": 0}   # *_n = само мачовете с реален pick
    records = []

    for rd in rounds:
        fixtures = [m for m in matches_all if m["round"] == rd]
        for fx in fixtures:
            prior = [m for m in matches_all if m["round"] < rd]
            result_ = analyze(prior, goal_events_all, h2h_all,
                              fx["home"], fx["away"], before_date=fx["date"])
            actual = actual_result_str(fx)
            print(f"РЕАЛЕН РЕЗУЛТАТ: {actual}")

            stats["total"] += 1
            actual_pick = "1" if fx["hg"] > fx["ag"] else ("2" if fx["ag"] > fx["hg"] else "X")
            actual_over = "ДА" if fx["hg"] + fx["ag"] > 2 else "НЕ"
            actual_btts = "ДА" if fx["hg"] > 0 and fx["ag"] > 0 else "НЕ"
            stats["1x2_hit"] += result_["pick_1x2"] == actual_pick
            # ПОПРАВКА (2026-09-04): pick=None (неутрална зона) не е нито хит,
            # нито мис — брои се отделен знаменател за всеки пазар.
            if result_["over25"]["pick"] is not None:
                stats["over_n"] += 1
                stats["over_hit"] += result_["over25"]["pick"] == actual_over
            if result_["btts"]["pick"] is not None:
                stats["btts_n"] += 1
                stats["btts_hit"] += result_["btts"]["pick"] == actual_btts

            records.append({
                "date": fx["date"], "home": fx["home"], "away": fx["away"],
                "hg": fx["hg"], "ag": fx["ag"],
                "b_pick_1x2": result_["pick_1x2"], "b_over25": result_["over25"]["pick"],
                "b_btts": result_["btts"]["pick"],
                "actual_1x2": actual_pick, "actual_over25": actual_over,
                "actual_btts": actual_btts,
            })

    t = stats["total"]
    print(f"\n{'='*60}\nОБОБЩЕНИЕ ({t} мача, от кръг {start_round}):")
    print(f"  1X2 (диагностично, не препоръчан): {stats['1x2_hit']}/{t}  ({stats['1x2_hit']/t:.0%})")
    n_o, n_b = stats.get("over_n", 0), stats.get("btts_n", 0)
    print(f"  Над2.5: {stats['over_hit']}/{n_o}  ({stats['over_hit']/n_o:.0%})" if n_o
          else "  Над2.5: няма pick-ове над прага")
    print(f"  ГГ:     {stats['btts_hit']}/{n_b}  ({stats['btts_hit']/n_b:.0%})" if n_b
          else "  ГГ:     няма pick-ове над прага")
    skipped_o, skipped_b = t - n_o, t - n_b
    if skipped_o or skipped_b:
        print(f"  (пропуснати като неуверени: Над2.5 {skipped_o}, ГГ {skipped_b} от {t})")

    if csv_out:
        import csv
        with open(csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            w.writeheader()
            w.writerows(records)
        print(f"\nCSV записан: {csv_out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--home")
    ap.add_argument("--away")
    ap.add_argument("--round", type=int,
                    help="backtest режим: всички мачове от този кръг до края, "
                         "с данни само ОТ ПРЕДИ съответния кръг (без look-ahead)")
    ap.add_argument("--csv-out", help="запис на per-match резултатите в CSV (backtest режим)")
    args = ap.parse_args()

    key = args.league.upper()
    db_path = LEAGUES.get(key)
    if db_path is None:
        raise SystemExit(f"Непозната лига '{args.league}'. Известни: {', '.join(LEAGUES)}")

    if args.round is not None:
        csv_out = Path(args.csv_out) if args.csv_out else None
        backtest_from_round(db_path, args.round, csv_out)
    elif args.home and args.away:
        matches = load_matches(db_path)
        goal_events = load_goal_events(db_path)
        h2h_all = load_h2h_all(db_path)
        analyze(matches, goal_events, h2h_all, args.home, args.away)
    else:
        raise SystemExit("Подай или --home/--away, или --round N")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
