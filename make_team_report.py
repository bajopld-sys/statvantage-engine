"""Генерира team_report.html — статичен справочник за няколко лиги (падащо
меню горе), ВСИЧКИ от Sofascore (виж fetch_sofa.py/parse_sofa_api.py) —
Опта пътят (opta.db) вече не се ползва тук (2026-07-22, потребителско решение:
работим само със Sofascore занапред).

Раздели за всяка лига:
  1. Форма (последни 5 / последни 10 мача)
  2. Домакин срещу гост
  3. Голове по полувреме — реални за бази с home_ht (parse_sofa_api.py,
     incidents.json); xG fallback за старите vault-парснати бази.
  4. Над 2.5 / BTTS честота
  5. Повел на почивката, но не спечелил — само за бази с home_ht.
  6. Съдии — картони на мач
  7. Ранно изплащане (2-голов аванс, но не спечелил накрая)

Избирачът на 2 отбора носи:
  - H2H таблица — само текущия сезон в sofa_<лига>.db (списък с дати/резултати)
  - H2H обобщение (fetch_h2h.py, Sofascore /event/{{id}}/h2h) — последни <=10
    срещи ВЪВ ВСИЧКИ турнири/сезони, но само W-D-L бройка, без резултати;
    покрива ВСЯКА двойка в лигата (fetch_h2h.py сканира целия сезонен
    график, не само изтегления до момента диапазон — виж докстринга му за
    защо: закотвя дори на неизигран бъдещ fixture при нужда). Bundesliga/
    Ligue1 (все още vault-парснати, извън LEAGUES речника на fetch_sofa.py)
    остават без тази таблица
  - БЪРЗА СИМУЛАЦИЯ до бутона "Изчисти": 1X2/Над2.5/Г-Г от xG-Dixon-Coles
    модела на profiles_sofa.py, изцяло смятана в браузъра (страницата е
    статична, работи офлайн) — Отбор 1 = домакин, Отбор 2 = гост. H2H
    корекцията тук е ПО-СЛАБА от CLI simulate_sofa.py (h2h.py) — там е
    goal-based от football-data CSV-та (EPL само); тук е W-D-L-based от
    горното обобщение, капната ±12% и смалена при малка извадка (виж JS
    h2hMultiplier()) — по-широк обхват (всички 4 лиги, при налични данни),
    но по-плитък сигнал.

Употреба:  python engine/make_team_report.py
"""
import datetime
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from config import DATA_DIR, REPORT_PATH

sys.path.insert(0, str(Path(__file__).resolve().parent))
from referees import referee_table
from early_payout import gather as gather_early_payout
from next_fixtures import build_comparison as build_model_comparison
from profiles_sofa import build_profiles as build_sofa_profiles
from goal_timeline import has_goal_events
from model_predictions_log import DB_PATH as PRED_DB_PATH, MODELS as PRED_MODELS, MODEL_LABELS as PRED_MODEL_LABELS

# Модел В v2 (атрибути на играчи от Sofascore) е СКРИТ от сайта от 2026-10-26 по решение на
# потребителя: Sofascore е блокиран, атрибути няма и v2 не се записва от 18.09. Кодът и
# старите прогнози в model_predictions.db остават; върни True, ако атрибутите се върнат.
SHOW_V2 = False
from lineup_impact import top_players_json

OUT = REPORT_PATH

# (показвано име, sofa db файл)
LEAGUES = [
    # Eliteserien (Норвегия), Allsvenskan (Швеция) и LaLiga2 махнати от сайта по
    # решение на потребителя (2026-10-03) — данните остават локално (sofa_*.db,
    # fotmob_test_allsvenskan.db), само изключени от активния LEAGUES списък/
    # отчета. Връщат се с разкоментиране на редовете (и в update_all.py).
    # ("Eliteserien", DATA_DIR / "sofa_eliteserien.db"),
    # ("Allsvenskan", DATA_DIR / "sofa_allsvenskan.db"),
    ("Bundesliga", DATA_DIR / "sofa_bundesliga.db"),  # Германия 1во ниво — активирана 2026-10-16
                                                      # (от FotMob, 5 изиграни кръга; старият 25/26 файл е архивиран)
    ("Bundesliga2", DATA_DIR / "sofa_bundesliga2.db"),  # 2. Bundesliga, добавена 2026-08-08
    ("Championship", DATA_DIR / "sofa_championship.db"),  # Англия 2ро ниво, добавена 2026-08-14
    ("BelgiumPL", DATA_DIR / "sofa_belgiumpl.db"),  # Белгия 1во ниво (Pro League), добавена 2026-08-15
    ("LaLiga", DATA_DIR / "sofa_laliga.db"),  # Испания 1во ниво, реактивирана 2026-08-16
                                              # (26/27 сезон, чист файл — старият 25/26
                                              # архивиран в data/archive/sofa_laliga_25-26.db)
    # ("LaLiga2", DATA_DIR / "sofa_laliga2.db"),  # Испания 2ро ниво, добавена 2026-08-16, махната 2026-10-03
    ("EPL", DATA_DIR / "sofa_epl.db"),  # Англия 1во ниво — активирана 2026-08-24
                                        # (26/27 старт 21.08, кръг 1 вече изигран)
    ("Ligue1", DATA_DIR / "sofa_ligue1.db"),  # Франция 1во ниво — активирана 2026-08-24
                                              # (26/27 старт 21.08, кръг 1 вече изигран)
    ("SerieA", DATA_DIR / "sofa_seriea.db"),  # Италия 1во ниво — добавена и активирана
                                              # 2026-08-24 (26/27 старт ~23.08, кръг 1 в ход)
    # Bundesliga (1во ниво) — конфигурирана (LEAGUE_COUNTRY/LEAGUE_SOFA_IDS вече
    # попълнени), НО умишлено оставена закоментирана тук (2026-08-24) — 26/27
    # сезонът стартира 28.08, все още не е започнал (auto_upper_round връща 0
    # кръга, проверено чрез Sofascore). Разкоментирай реда долу щом update_all.py
    # действително изтегли завършени мачове за нея.
    # ("Bundesliga", DATA_DIR / "sofa_bundesliga.db"),  # старият vault sofa_bundesliga.db
    #                                                   # архивиран в data/archive/
    #                                                   # sofa_bundesliga_old-vault.db —
    #                                                   # 26/27 тръгва чисто на API пайплайна
    # Faroe (Besta deild karla) премахната 2026-08-08 по решение на потребителя,
    # след пробния период (2026-08-03) — данните остават локално (sofa_faroe.db),
    # само изключена от активния LEAGUES списък/пайплайна.
]

# Държава по лига — само за показвания текст в picker-а (по искане на
# потребителя, 2026-07: държавата е по-разчитаема от името на лигата).
# Вътрешният ключ (league_name/data-league/JSON ключовете по-долу) остава
# самото име на лигата навсякъде другаде — не се пипа, за да не се чупи
# съпоставянето в JS (h2hData[league] и т.н.).
LEAGUE_COUNTRY = {
    "Eliteserien": "Норвегия",
    "Allsvenskan": "Швеция",
    "Bundesliga2": "2. Bundesliga",  # по искане на потребителя (2026-08-08) — самото
                                     # име на лигата вместо само "Германия", за да не се
                                     # бърка с 1во ниво, ако то се върне в списъка
    "Championship": "Championship",  # Англия 2ро ниво — самото име, за да не се бърка
                                     # с EPL, ако то се върне в списъка (2026-08-14)
    "BelgiumPL": "Белгия",  # Pro League, 1во ниво (2026-08-15)
    "LaLiga": "LaLiga",  # Испания 1во ниво, реактивирана 2026-08-16 (26/27 сезон)
    "LaLiga2": "LaLiga2",  # Испания 2ро ниво (2026-08-16)
    "EPL": "Висшата лига",  # Англия 1во ниво — активирана 2026-08-24
    "Bundesliga": "Бундеслига",  # Германия 1во ниво — конфигурирано 2026-08-16,
                                 # сезонът още не е започнал (виж бележката в LEAGUES по-горе)
    "Ligue1": "Лига 1",  # Франция 1во ниво — активирана 2026-08-24
    "SerieA": "Серия А",  # Италия 1во ниво — добавена и активирана 2026-08-24
}

# tournament_id/season_id (Sofascore) — за next_fixtures.py (модел-сравнение
# на реалните предстоящи мачове). Само за активните лиги в LEAGUES по-горе.
LEAGUE_SOFA_IDS = {
    "Eliteserien": (20, 87809),
    "Allsvenskan": (40, 87925),
    "Bundesliga2": (44, 97406),
    "Championship": (18, 97037),
    "BelgiumPL": (38, 96616),
    "LaLiga": (8, 97268),
    "LaLiga2": (54, 97280),
    "EPL": (17, 96668),
    "Bundesliga": (35, 97464),
    "Ligue1": (34, 96127),
    "SerieA": (23, 95836),
}


# ── Общи агрегации (работят върху нормализиран списък (home,away,hg,ag)) ──

def form_table(matches: list[tuple]) -> tuple[list, list]:
    by_team: dict[str, list] = {}
    for date, h, a, hg, ag in matches:
        for team, opp, gf, ga, venue in ((h, a, hg, ag, "домакин"), (a, h, ag, hg, "гост")):
            res = "W" if gf > ga else ("D" if gf == ga else "L")
            by_team.setdefault(team, []).append((date, res, opp, gf, ga, venue))

    rows = []
    for team, games in by_team.items():
        games.sort(key=lambda g: g[0])
        last5, last10 = games[-5:], games[-10:]

        def summarize(gs):
            w = sum(1 for g in gs if g[1] == "W")
            d = sum(1 for g in gs if g[1] == "D")
            l = sum(1 for g in gs if g[1] == "L")
            form_str = "".join(g[1] for g in gs)
            # Детайл за hover title (2026-08-24, по искане на потребителя: "като
            # сложа курсора върху W/D/L да ми показва резултата и срещу кого") —
            # едно към едно с form_str по индекс, вижте colorize_form().
            detail = [{"opponent": g[2], "gf": g[3], "ga": g[4], "venue": g[5]} for g in gs]
            return w, d, l, w * 3 + d, form_str, detail

        w5, d5, l5, pts5, f5, det5 = summarize(last5)
        w10, d10, l10, pts10, f10, det10 = summarize(last10)
        rows.append({"team": team, "w5": w5, "d5": d5, "l5": l5, "pts5": pts5,
                     "form5": f5, "form5_detail": det5,
                     "w10": w10, "d10": d10, "l10": l10, "pts10": pts10,
                     "form10": f10, "form10_detail": det10})
    rows5 = sorted(rows, key=lambda r: (-r["pts5"], -r["w5"]))
    rows10 = sorted(rows, key=lambda r: (-r["pts10"], -r["w10"]))
    return rows5, rows10


def home_away_table(matches: list[tuple]) -> tuple[list, list]:
    home_stats, away_stats = {}, {}

    def bump(stats, team, gf, ga):
        s = stats.setdefault(team, {"mp": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0})
        s["mp"] += 1
        s["gf"] += gf
        s["ga"] += ga
        s["w" if gf > ga else ("d" if gf == ga else "l")] += 1

    for _, h, a, hg, ag in matches:
        bump(home_stats, h, hg, ag)
        bump(away_stats, a, ag, hg)

    def finalize(stats):
        out = [{"team": t, **s, "pts": s["w"] * 3 + s["d"], "gd": s["gf"] - s["ga"]}
               for t, s in stats.items()]
        out.sort(key=lambda r: (-r["pts"], -r["gd"]))
        return out

    return finalize(home_stats), finalize(away_stats)


def home_away_half_table(con) -> tuple[list, list, list, list]:
    """Домакин/Гост разбивка по полувреме (добавено 2026-08-02, по искане на
    потребителя) — само за бази с реален HT резултат (виж has_real_half).
    За разлика от sofa_half_goals()/sofa_half_1x2() (които смесват
    домакинските и гостуващите мачове на отбора в едно класиране), тук всеки
    отбор участва само с домакинските си мачове в "Домакин" таблиците, и само
    с гостуващите в "Гост" таблиците — venue-специфично, като основната
    home_away_table(), но разцепено по полувреме."""
    rows = con.execute(
        "SELECT home, away, home_ht, away_ht, hg, ag FROM matches "
        "WHERE home_ht IS NOT NULL").fetchall()

    home_fh, home_sh, away_fh, away_sh = {}, {}, {}, {}

    def bump(target, team, scored, conceded, wdl):
        s = target.setdefault(team, {"mp": 0, "gf": 0, "ga": 0, "w": 0, "d": 0, "l": 0})
        s["mp"] += 1
        s["gf"] += scored
        s["ga"] += conceded
        s["w"] += wdl[0]
        s["d"] += wdl[1]
        s["l"] += wdl[2]

    def wdl(diff_a, diff_b):
        if diff_a > diff_b:
            return (1, 0, 0)
        if diff_a < diff_b:
            return (0, 0, 1)
        return (0, 1, 0)

    for home, away, hht, aht, hg, ag in rows:
        bump(home_fh, home, hht, aht, wdl(hht, aht))
        bump(home_sh, home, hg - hht, ag - aht, wdl(hg - hht, ag - aht))
        bump(away_fh, away, aht, hht, wdl(aht, hht))
        bump(away_sh, away, ag - aht, hg - hht, wdl(ag - aht, hg - hht))

    def finalize(stats):
        out = [{"team": t, **s, "pts": s["w"] * 3 + s["d"], "gd": s["gf"] - s["ga"]}
               for t, s in stats.items()]
        out.sort(key=lambda r: (-r["pts"], -r["gd"]))
        return out

    return finalize(home_fh), finalize(home_sh), finalize(away_fh), finalize(away_sh)


def over_btts_table(matches: list[tuple]) -> tuple[list, list]:
    stats = {}
    for _, h, a, hg, ag in matches:
        over, btts = (hg + ag) > 2.5, (hg > 0 and ag > 0)
        for team in (h, a):
            s = stats.setdefault(team, {"mp": 0, "over": 0, "btts": 0})
            s["mp"] += 1
            s["over"] += int(over)
            s["btts"] += int(btts)
    out = [{"team": t, "mp": s["mp"], "over": s["over"], "over_pct": s["over"] / s["mp"],
           "btts": s["btts"], "btts_pct": s["btts"] / s["mp"]} for t, s in stats.items()]
    return (sorted(out, key=lambda r: -r["over_pct"]),
            sorted(out, key=lambda r: -r["btts_pct"]))


# ── Sofascore лиги (отборно ниво) ──

def sofa_matches(con) -> list[tuple]:
    return [(date, h, a, hg, ag) for match_id, date, h, a, hg, ag in con.execute(
        "SELECT match_id, date, home, away, hg, ag FROM matches ORDER BY date")]


def sofa_half_xg(con):
    """Пази и допуснатата xG (опонентският ред за същия match_id+period),
    по искане на потребителя (2026-07) — не само вкараната."""
    rows = con.execute(
        "SELECT s1.team, s1.period, s1.xg AS own_xg, s2.xg AS opp_xg "
        "FROM stats s1 JOIN stats s2 "
        "  ON s2.match_id = s1.match_id AND s2.period = s1.period AND s2.team != s1.team "
        "WHERE s1.period IN ('A','H1','B','H2')").fetchall()
    fh_acc, sh_acc = {}, {}
    for team, period, own_xg, opp_xg in rows:
        acc = fh_acc if period in ("A", "H1") else sh_acc
        # ПОПРАВКА (2026-07): мач, минал и през двата пайплайна (стар
        # parse_sofa.py с period='H1'/'H2', нов parse_sofa_api.py с
        # period='A'/'B') преди даваше ДВА отделни реда за един и същ
        # отбор/полувреме — плоският dict presence го презаписваше тихо
        # (загуба на ~половината мачове, потвърдено на живо в Ligue1: и
        # 18-те отбора имат редове под двата етикета). Сега сумираме
        # (xg*мачове, мачове) и делим накрая, вместо да презаписваме ред с
        # ред — реална претеглена средна по ВСИЧКИ мачове на отбора.
        total_own, total_opp, total_mp = acc.get(team, (0.0, 0.0, 0))
        acc[team] = (total_own + (own_xg or 0), total_opp + (opp_xg or 0), total_mp + 1)
    fh = {t: (round(s / m, 2), round(o / m, 2), m, s / m, o / m) for t, (s, o, m) in fh_acc.items() if m}
    sh = {t: (round(s / m, 2), round(o / m, 2), m, s / m, o / m) for t, (s, o, m) in sh_acc.items() if m}
    teams = sorted(fh.keys())
    return fh, sh, sorted(teams, key=lambda t: -fh[t][3]), sorted(teams, key=lambda t: -sh[t][3])


def has_real_half(con) -> bool:
    """home_ht/away_ht съществуват само за бази, минали през новия
    parse_sofa_api.py (incidents.json → реален HT резултат от Sofascore).
    Старите vault-парснати бази (Bundesliga/Ligue1) нямат тази колона."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    if "home_ht" not in cols:
        return False
    return con.execute(
        "SELECT COUNT(*) FROM matches WHERE home_ht IS NOT NULL").fetchone()[0] > 0


def sofa_half_goals(con):
    """Реални голове по полувреме (не xG) — от home_ht/away_ht, попълнени от
    Sofascore-овите incidents 'HT'/'FT' маркери. Огледално на старата
    Опта-специфична opta_half_goals(), но по-просто — HT резултатът вече е
    на реда на мача, не изисква join към играчова таблица по полувреме.
    Пази и допуснатите голове (опонентският резултат в същото полувреме),
    по искане на потребителя (2026-07) — не само вкараните."""
    rows = con.execute(
        "SELECT home, away, home_ht, away_ht, hg, ag FROM matches "
        "WHERE home_ht IS NOT NULL").fetchall()
    fh, sh = {}, {}

    def bump(target, team, scored, conceded):
        g, ga, mp = target.get(team, (0, 0, 0))
        target[team] = (g + scored, ga + conceded, mp + 1)

    for home, away, hht, aht, hg, ag in rows:
        bump(fh, home, hht, aht)
        bump(fh, away, aht, hht)
        bump(sh, home, hg - hht, ag - aht)
        bump(sh, away, ag - aht, hg - hht)

    fh = {t: (g, ga, mp, g / mp if mp else 0, ga / mp if mp else 0) for t, (g, ga, mp) in fh.items()}
    sh = {t: (g, ga, mp, g / mp if mp else 0, ga / mp if mp else 0) for t, (g, ga, mp) in sh.items()}
    teams = sorted(fh.keys())
    return fh, sh, sorted(teams, key=lambda t: -fh[t][3]), sorted(teams, key=lambda t: -sh[t][3])


def sofa_half_1x2(con):
    """1X2 (W-D-L) резултат по полувреме за всеки отбор — само за бази с реален
    HT резултат (виж has_real_half). 1-во полувреме: кой води на почивката.
    2-ро полувреме: сравнява голове, вкарани САМО във второто полувреме
    (hg-hht срещу ag-aht) — огледално на sofa_half_goals(), НЕ е кумулативен
    резултат, а изходът само от съответния сегмент."""
    rows = con.execute(
        "SELECT home, away, home_ht, away_ht, hg, ag FROM matches "
        "WHERE home_ht IS NOT NULL").fetchall()
    fh, sh = {}, {}

    def bump(target, team, w, d, l):
        tw, td, tl, mp = target.get(team, (0, 0, 0, 0))
        target[team] = (tw + w, td + d, tl + l, mp + 1)

    def outcome(diff_a, diff_b, team_a, team_b, target):
        if diff_a > diff_b:
            bump(target, team_a, 1, 0, 0)
            bump(target, team_b, 0, 0, 1)
        elif diff_a < diff_b:
            bump(target, team_a, 0, 0, 1)
            bump(target, team_b, 1, 0, 0)
        else:
            bump(target, team_a, 0, 1, 0)
            bump(target, team_b, 0, 1, 0)

    for home, away, hht, aht, hg, ag in rows:
        outcome(hht, aht, home, away, fh)
        outcome(hg - hht, ag - aht, home, away, sh)

    return fh, sh


def sofa_blown_lead(con) -> list:
    """Повел на почивката, но ЗАГУБИЛ (не и наравно) — от реалния HT резултат
    в matches (виж has_real_half). HT е директно на реда, не изисква
    отделен join към играчова таблица по полувреме."""
    rows = con.execute(
        "SELECT date, home, away, home_ht, away_ht, hg, ag FROM matches "
        "WHERE home_ht IS NOT NULL ORDER BY date").fetchall()
    counts, details = {}, {}
    for date, home, away, hht, aht, hg, ag in rows:
        for team, opp, t_ht, o_ht, t_ft, o_ft in (
                (home, away, hht, aht, hg, ag), (away, home, aht, hht, ag, hg)):
            if t_ht > o_ht and t_ft < o_ft:
                counts[team] = counts.get(team, 0) + 1
                details.setdefault(team, []).append({
                    "opp": opp, "date": date, "ht": f"{t_ht}:{o_ht}",
                    "ft": f"{t_ft}:{o_ft}", "og": False})
    out = [{"team": t, "count": c, "matches": details[t]} for t, c in counts.items()]
    out.sort(key=lambda r: -r["count"])
    return out


def sofa_late_goals(con) -> list:
    """Класиране по голове, вкарани от 85-та минута нататък (вкл. добавеното
    време) — от goal_events (виж goal_timeline.has_goal_events), точни
    минути от Sofascore incidents. Чисто информационна справка по изрична
    молба на потребителя ("не го вкарвай в моделите. то си е за мен") — НЕ
    се подава на нито един от симулационните модели (DC/v1/v2/Model B)."""
    rows = con.execute(
        "SELECT m.date, m.home, m.away, ge.is_home, ge.minute, ge.added_time "
        "FROM goal_events ge JOIN matches m ON ge.match_id = m.match_id "
        "WHERE ge.minute >= 85 ORDER BY m.date").fetchall()
    counts, details = {}, {}
    for date, home, away, is_home, minute, added in rows:
        team = home if is_home else away
        opp = away if is_home else home
        minute_label = f"{minute}'+{added}" if added else f"{minute}'"
        counts[team] = counts.get(team, 0) + 1
        details.setdefault(team, []).append({
            "opp": opp, "date": date, "minute": minute_label, "venue": "домакин" if is_home else "гост"})
    out = [{"team": t, "count": c, "matches": details[t]} for t, c in counts.items()]
    out.sort(key=lambda r: -r["count"])
    return out


EARLY_BUCKETS = (10, 15, 20, 30)


def sofa_early_goals(con) -> list:
    """Класиране по РАННИ голове — колко е вкарал всеки отбор до 10-та, 15-та,
    20-та и 30-та минута. Кофите са НАТРУПВАЩИ СЕ (гол в 8-та минута влиза и в
    четирите колони), затова "до 15′" винаги е >= "до 10′".

    Подрежда се по голове до 30′ НА МАЧ, а не по абсолютен брой — отборите
    имат различен брой изиграни мачове (13-15 в момента), така че суровата
    бройка би дала предимство на изиграл повече мачове отбор.

    Информационна справка, огледално на sofa_late_goals() — НЕ влиза в
    нито един от прогнозните модели."""
    played: dict[str, int] = {}
    for home, away in con.execute("SELECT home, away FROM matches"):
        played[home] = played.get(home, 0) + 1
        played[away] = played.get(away, 0) + 1

    counts: dict[str, list[int]] = {}
    for home, away, is_home, minute in con.execute(
            "SELECT m.home, m.away, ge.is_home, ge.minute "
            "FROM goal_events ge JOIN matches m ON ge.match_id = m.match_id "
            f"WHERE ge.minute <= {max(EARLY_BUCKETS)}"):
        team = home if is_home else away
        arr = counts.setdefault(team, [0] * len(EARLY_BUCKETS))
        for i, bucket in enumerate(EARLY_BUCKETS):
            if minute <= bucket:
                arr[i] += 1

    out = []
    for team, mp in played.items():
        arr = counts.get(team, [0] * len(EARLY_BUCKETS))
        out.append({"team": team, "mp": mp, "buckets": arr,
                    "rate": arr[-1] / mp if mp else 0.0})
    out.sort(key=lambda r: (-r["rate"], -r["buckets"][-1]))
    return out


def sim_model_dataset(con) -> dict | None:
    """Параметри за клиентския (JS) xG-Dixon-Coles модел — виж profiles_sofa.py
    за формулите. Само основните полета, нужни за simulate_xg() без H2H
    (h2h.py изисква многосезонни football-data CSV-та, не се вграждат тук)."""
    try:
        prof = build_sofa_profiles(con)
    except (SystemExit, ValueError, KeyError, ZeroDivisionError):
        # ПОПРАВКА (2026-07): лига с 0 ненулеви xg редове (тотална дупка в
        # Sofascore покритието — виж Allsvenskan бележката в
        # simulate_lineup_v1.py) прави np.average(..., weights=...) в
        # profiles_sofa.py да гръмне с ZeroDivisionError, не с нещо от
        # старата хваната тройка — иначе цялото main() пада (всички лиги),
        # не само засегнатата.
        return None
    t, lg = prof["teams"], prof["league"]
    teams = {}
    for team, r in t.iterrows():
        if pd.isna(r["own_xg"]) or pd.isna(r["xg_conceded"]):
            continue
        teams[team] = {
            "own_xg": round(float(r["own_xg"]), 4),
            "xg_conceded": round(float(r["xg_conceded"]), 4),
            "att_form": round(float(r["att_form"]), 4),
            "def_form": round(float(r["def_form"]), 4),
            "rating_form": round(float(r["rating_form"]), 4),
        }
    return {"avg_xg": round(float(lg["avg_xg"]), 4),
           "home_adv": round(float(lg["home_adv"]), 4), "teams": teams}


def sofa_h2h_dataset(con) -> dict:
    """H2H само от текущия сезон в собствената sofa.db (нямаме
    многосезонен източник за не-английски лиги)."""
    rows = con.execute("SELECT date, home, away, hg, ag FROM matches "
                       "ORDER BY date DESC").fetchall()
    out: dict[str, list] = {}
    for date, h, a, hg, ag in rows:
        key = "|||".join(sorted([h, a]))
        out.setdefault(key, []).append({"date": date, "home": h, "away": a,
                                        "hg": hg, "ag": ag})
    return out


def sofa_h2h_summary(con) -> dict:
    """Последни <=10 срещи, ВСИЧКИ турнири и сезони (fetch_h2h.py,
    /event/{id}/h2h) — само W-D-L бройка, без дати/резултати. Покрива ВСЯКА
    двойка в лигата (fetch_h2h.py сканира целия сезонен график, не само
    изтегления диапазон). По-широк обхват от sofa_h2h_dataset (само текущия
    сезон в нашата лига), но по-плитък (само бройка, не списък мачове)."""
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='h2h_summary'"
    ).fetchone()
    if not exists:
        return {}
    out: dict[str, dict] = {}
    for team_a, team_b, wins_a, wins_b, draws, total, _, as_of in con.execute(
            "SELECT team_a, team_b, wins_a, wins_b, draws, total, as_of_event, as_of_date "
            "FROM h2h_summary"):
        key = "|||".join(sorted([team_a, team_b]))
        out[key] = {"team_a": team_a, "team_b": team_b, "wins_a": wins_a,
                   "wins_b": wins_b, "draws": draws, "total": total, "as_of": as_of}
    return out


def sofa_h2h_matches(con) -> dict:
    """Реален мач-по-мач H2H (fetch_h2h_matches.py, Sofascore
    /team/{id}/events/last/{page}) — дати, резултати, ТУРНИР, през ВСИЧКИ
    сезони/турнири (лига, купа, суперкупа...), не само тазгодишните мачове в
    нашата база. Потребителско искане (2026-07): "резултатът да ми върши
    работа, не само WDL". Заменя sofa_h2h_summary() като първичен източник
    навсякъде, където е наличен — тя остава само fallback за лиги без
    h2h_matches (Bundesliga/Ligue1, все още без fetch_sofa.py team_id-та)."""
    exists = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='h2h_matches'"
    ).fetchone()
    if not exists:
        return {}
    out: dict[str, list] = {}
    for team_a, team_b, date, tournament, home, away, hg, ag in con.execute(
            "SELECT team_a, team_b, date, tournament, home, away, hg, ag "
            "FROM h2h_matches ORDER BY date DESC"):
        key = "|||".join(sorted([team_a, team_b]))
        out.setdefault(key, []).append({
            "date": date, "tournament": tournament, "home": home, "away": away,
            "hg": hg, "ag": ag})
    return out


# ── Рендиране ──

def esc(s):
    # ПОПРАВКА (2026-07): не ескейпваше '"', но резултатът се вгражда и
    # вътре в двойно-кавичени HTML атрибути (data-team="...", <option
    # value="...">) — име с буквален '"' би прекъснало атрибута предсрочно.
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def rank_cls(i: int) -> str:
    """Градирано открояване на топ 3 (rank-1 най-силно) — вместо плоско top3."""
    return f" rank-{i}" if i <= 3 else ""


def colorize_form(form_str, detail=None):
    """detail (по избор) — списък от form_table()'s "form5_detail"/"form10_detail",
    едно към едно по индекс с form_str — вгражда title="..." hover текст на всяка
    буква (2026-08-24, по искане на потребителя): резултат + съперник + дом/гост."""
    cls = {"W": "res-w", "D": "res-d", "L": "res-l"}
    if not detail:
        return "".join(f"<span class='{cls[c]}'>{c}</span>" for c in form_str)
    label = {"W": "Победа", "D": "Равенство", "L": "Загуба"}
    out = []
    for c, d in zip(form_str, detail):
        title = f"{label[c]} {d['gf']}:{d['ga']} ({d['venue']}) срещу {d['opponent']}"
        out.append(f"<span class='{cls[c]}' title=\"{esc(title)}\">{c}</span>")
    return "".join(out)


def wdl_pill(w, d, l):
    """W-D-L (или 1X2 по полувреме) като цветни пил-сегменти, вместо суров
    текст "10-0-3" — зелено/сиво/червено, огледално на colorize_form()."""
    return (f"<span class='wdlPill'><b class='w'>{w}</b>"
            f"<b class='d'>{d}</b><b class='l'>{l}</b></span>")


def home_away_html(rows, league, venue):
    # venue ('home'/'away') маркира реда с data-venue, за да може JS picker-ът
    # (apply() в render()) да highlight-не САМО избрания домакин в "Домакин"
    # таблиците и САМО избрания гост в "Гост" таблиците, вместо да маркира и
    # двата избрани отбора във всяка от двете (bug, докладван от потребителя
    # 2026-08-24 — screenshot показваше Малмьо highlight-нат в "Гост" И
    # Юргорден highlight-нат в "Домакин", макар само единият да е реално
    # домакин/гост в избрания мач).
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        out.append(
            f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(r['team'])}\" data-venue=\"{venue}\">"
            f"<td>{i}</td><td class='tname'>{esc(r['team'])}</td><td>{r['mp']}</td>"
            f"<td>{wdl_pill(r['w'], r['d'], r['l'])}</td>"
            f"<td>{r['gf']}:{r['ga']} ({r['gd']:+d})</td><td>{r['pts']}</td></tr>")
    return "\n".join(out)


def form_html(rows, form_key, w_key, d_key, l_key, pts_key, league):
    detail_key = form_key + "_detail"
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        out.append(
            f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
            f"<td>{i}</td><td class='tname'>{esc(r['team'])}</td>"
            f"<td class='form'>{colorize_form(r[form_key], r.get(detail_key))}</td>"
            f"<td>{wdl_pill(r[w_key], r[d_key], r[l_key])}</td><td>{r[pts_key]}</td></tr>")
    return "\n".join(out)


def half_html(sorted_teams, data, league, is_xg, wdl=None):
    rows = []
    for i, t in enumerate(sorted_teams, 1):
        val, conceded, mp, avg, avg_conceded = data[t]
        cls = rank_cls(i)
        val_disp = f"{val:.2f}" if is_xg else val
        conceded_disp = f"{conceded:.2f}" if is_xg else conceded
        avg_disp = f"{avg:.2f}"
        avg_conceded_disp = f"{avg_conceded:.2f}"
        # Ред на колоните (по искане на потребителя, 2026-07): първо мачовете
        # като контекст, после вкарани (общо + на мач), после допуснати
        # (общо + на мач) — вместо старото "Голове" без уточнение каква посока.
        wdl_td = ""
        if wdl is not None:
            w, d, l = wdl.get(t, (0, 0, 0))[:3]
            wdl_td = f"<td class='wdl'>{wdl_pill(w, d, l)}</td>"
        rows.append(f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(t)}\">"
                    f"<td>{i}</td><td class='tname'>{esc(t)}</td><td>{mp}</td>"
                    f"<td>{val_disp}</td><td>{avg_disp}</td>"
                    f"<td>{conceded_disp}</td><td>{avg_conceded_disp}</td>{wdl_td}</tr>")
    return "\n".join(rows)


def over_btts_html(sorted_list, key, pct_key, league):
    rows = []
    for i, r in enumerate(sorted_list, 1):
        cls = rank_cls(i)
        rows.append(f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
                    f"<td>{i}</td><td class='tname'>{esc(r['team'])}</td>"
                    f"<td>{r[key]}</td><td>{r['mp']}</td><td>{r[pct_key]*100:.0f}%</td></tr>")
    return "\n".join(rows)


def early_payout_html(rows):
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        match_list = "; ".join(
            f"({m['date']}) поведе <b>{m['score_at']}</b> на {m['minute']}' срещу {esc(m['opp'])} → FT <b>{m['final']}</b>"
            for m in r.get("matches", []))
        out.append(
            f"<tr class='ref-row{cls}'><td>{i}</td><td class='tname'>{esc(r['team'])}</td>"
            f"<td>{r['no_win']}</td><td>{r['led2']}</td><td>{r['pct']*100:.0f}%</td>"
            f"<td class='detail'>{match_list}</td></tr>")
    return "\n".join(out)


def comeback_html(rows):
    """Огледално на early_payout_html — изоставал с 2 гола, но НЕ загубил."""
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        match_list = "; ".join(
            f"изоставаше <b>{esc(m['score_at'])}</b> на {m['minute']}' срещу {esc(m['opp'])} → FT <b>{m['final']}</b>"
            for m in r.get("matches", []))
        out.append(
            f"<tr class='ref-row{cls}'><td>{i}</td><td class='tname'>{esc(r['team'])}</td>"
            f"<td>{r['not_lost']}</td><td>{r['trailed2']}</td><td>{r['pct']*100:.0f}%</td>"
            f"<td class='detail'>{match_list}</td></tr>")
    return "\n".join(out)


def referees_html(rows):
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        out.append(
            f"<tr class='ref-row{cls}'><td>{i}</td><td class='tname'>{esc(r['referee'])}</td>"
            f"<td>{r['matches']}</td><td>{r['yellows_per_match']:.2f}</td>"
            f"<td>{r['reds_per_match']:.2f}</td><td>{r['cards_per_match']:.2f}</td></tr>")
    return "\n".join(out)


def goalkeeper_saves_table(tour: int, season: int, min_apps: int = 1, top_n: int = 10) -> list[dict]:
    """Топ вратари по спасявания на мач за сезона (Sofascore top-players/overall,
    ключ 'saves') — кеширано през lineup_impact.top_players_json (2026-09-19,
    по искане на потребителя докато анализираше вратарски пазари за залози —
    "кои вратари имат най-много спасявания на мач"). Чисто справочна секция,
    НЕ участва в нито един прогнозен модел. Best-effort: връща [] при
    липсващи данни/грешка, никога не гърми целия отчет (виж gather_sofa()
    за try/except обвивката отвън, огледално на model_comparison по-долу)."""
    data = top_players_json(tour, season)
    rows = (data or {}).get("topPlayers", {}).get("saves", [])
    out = []
    for r in rows:
        stats = r.get("statistics", {})
        apps = stats.get("appearances", 0)
        saves = stats.get("saves", 0)
        if apps < min_apps:
            continue
        out.append({
            "player": r.get("player", {}).get("name", "?"),
            "team": r.get("team", {}).get("name", "?"),
            "saves": saves, "apps": apps,
            "per_match": saves / apps if apps else 0.0,
        })
    out.sort(key=lambda x: -x["per_match"])
    return out[:top_n]


def team_shots_table(con) -> list[dict]:
    """Удари и точни удари по отбор — за и срещу, на мач — плюс реалните спасявания
    на собствения вратар (2026-10-16, по искане на потребителя: "да мога лесно да
    видя къде може да има потенциал за много вратарски спасявания"). Всичко от
    нашата локална `stats` (period='full'), т.е. работи и върху FotMob-слетите мачове.

    Ключовата колона е "Точни срещу/мач": вратарят на отбора получава точно тези
    удари; колкото повече, толкова повече спасявания са възможни. Сортира се по нея.
    Чисто справочна таблица — НЕ участва в нито един прогнозен модел."""
    rows = con.execute("""
        SELECT m.match_id, m.home, m.away, m.hg, m.ag,
               sh.shots, sh.sot, sh.saves, sa.shots, sa.sot
        FROM matches m
        JOIN stats sh ON sh.match_id = m.match_id AND sh.team = m.home AND sh.period = 'full'
        JOIN stats sa ON sa.match_id = m.match_id AND sa.team = m.away AND sa.period = 'full'
        WHERE m.hg IS NOT NULL
    """).fetchall()
    agg: dict[str, dict] = {}

    def add(team, shots_f, sot_f, shots_a, sot_a, saves, conceded):
        a = agg.setdefault(team, {"n": 0, "shots_f": 0.0, "shots_a": 0.0, "sot_f": 0.0,
                                  "sot_a": 0.0, "saves": 0.0, "conceded": 0.0, "n_sot": 0})
        a["n"] += 1
        a["shots_f"] += shots_f or 0
        a["shots_a"] += shots_a or 0
        if sot_f is not None and sot_a is not None:
            a["n_sot"] += 1
            a["sot_f"] += sot_f
            a["sot_a"] += sot_a
            a["saves"] += saves or 0
            a["conceded"] += conceded or 0

    for _mid, home, away, hg, ag, h_sh, h_sot, h_sv, a_sh, a_sot in rows:
        # saves на вратаря на отбора = колоната saves от СОБСТВЕНИЯ ред на отбора
        a_sv = con.execute("SELECT saves FROM stats WHERE match_id=? AND team=? AND period='full'",
                           (_mid, away)).fetchone()
        add(home, h_sh, h_sot, a_sh, a_sot, h_sv, ag)
        add(away, a_sh, a_sot, h_sh, h_sot, a_sv[0] if a_sv else None, hg)

    out = []
    for team, a in agg.items():
        n, ns = a["n"], max(a["n_sot"], 1)
        out.append({"team": team, "n": n,
                    "shots_f": a["shots_f"] / n, "shots_a": a["shots_a"] / n,
                    "sot_f": a["sot_f"] / ns, "sot_a": a["sot_a"] / ns,
                    "saves": a["saves"] / ns})
    out.sort(key=lambda r: (-r["sot_a"], -r["shots_a"]))
    return out


def team_shots_html(rows, league):
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        out.append(
            f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
            f"<td>{i}</td><td class='tname'>{esc(r['team'])}</td><td>{r['n']}</td>"
            f"<td>{r['shots_f']:.1f}</td><td>{r['shots_a']:.1f}</td>"
            f"<td>{r['sot_f']:.1f}</td><td><b>{r['sot_a']:.1f}</b></td>"
            f"<td>{r['saves']:.1f}</td></tr>")
    return "\n".join(out)


def league_side_sot(con) -> tuple[float, float]:
    """Средно точни удари на мач на домакините и на гостите в лигата (домакин/гост
    фактор за прогнозата на спасяванията)."""
    r = con.execute("""
        SELECT AVG(sh.sot), AVG(sa.sot) FROM matches m
        JOIN stats sh ON sh.match_id = m.match_id AND sh.team = m.home AND sh.period = 'full'
        JOIN stats sa ON sa.match_id = m.match_id AND sa.team = m.away AND sa.period = 'full'
        WHERE m.hg IS NOT NULL AND sh.sot IS NOT NULL AND sa.sot IS NOT NULL""").fetchone()
    return (r[0] or 0.0), (r[1] or 0.0)


def team_keepers(con) -> dict:
    """Текущият вратар на отбора (от последния му мач) + СОБСТВЕНИТЕ му спасявания в
    последните до 5 мача (хронологично, най-старият първо) — като „Играч / Последни 5“
    при букмейкърите (2026-10-16, по искане на потребителя). Вратар = позиция 'G'
    (Sofascore) или '11' (FotMob); полевите играчи в Sofascore имат saves=0.0, затова
    не стига `saves IS NOT NULL`. НЕ влиза в числото на прогнозата: смесването на
    последните 5 с модела влошаваше бектеста при всяко тегло."""
    rows = con.execute("""
        SELECT p.team, p.name, p.saves, m.date, m.home, m.away, m.hg, m.ag
        FROM player_match p
        JOIN matches m ON m.match_id = p.match_id
        WHERE p.position IN ('G', '11') AND p.saves IS NOT NULL AND p.minutes >= 45
          AND m.hg IS NOT NULL
        ORDER BY m.date, m.match_id""").fetchall()
    import unicodedata

    _TRANSLIT = {"đ": "dj", "ø": "o", "æ": "ae", "ł": "l", "ß": "ss", "œ": "oe", "ð": "d", "þ": "th"}

    def _key(n: str) -> str:
        # същият вратар е записан с различно изписване в различните източници
        # (Sofascore „Lukáš Horníček“ срещу FotMob „Lukás Hornícek“, „Đorđe“ срещу
        # „Djordje“, „Jörgensen“ срещу „Jørgensen“) — без нормализация историята
        # му се разцепва на двама „вратари“
        d = unicodedata.normalize("NFKD", (n or "").lower())
        d = "".join(ch for ch in d if not unicodedata.combining(ch))
        return "".join(_TRANSLIT.get(ch, ch) for ch in d).strip()

    def _same(k1: str, k2: str) -> bool:
        t1, t2 = set(k1.split()), set(k2.split())
        return k1 == k2 or t1 <= t2 or t2 <= t1       # „Alisson“ ⊂ „Alisson Becker“

    per: dict[str, dict[str, list]] = {}
    spell: dict[tuple, str] = {}
    current: dict[str, str] = {}
    def _info(team, date, home, away, hg, ag):
        # „срещу кого“ за подсказката над числото: ден.месец, противник, дом/гост, резултат
        # от гледна точка на отбора на вратаря
        d = f"{date[8:10]}.{date[5:7]}" if date and len(date) >= 10 else (date or "")
        if team == home:
            return f"{d} · срещу {away} (дома) · {hg}:{ag}"
        return f"{d} · срещу {home} (гост) · {ag}:{hg}"

    for team, name, saves, date, home, away, hg, ag in rows:
        k = _key(name)
        groups = per.setdefault(team, {})
        k = next((g for g in groups if _same(g, k)), k)     # към вече познат вратар на отбора
        groups.setdefault(k, []).append((int(saves), _info(team, date, home, away, hg, ag)))
        old_s = spell.get((team, k))
        if old_s is None or sum(ord(c) > 127 for c in name) > sum(ord(c) > 127 for c in old_s):
            spell[(team, k)] = name
        current[team] = k
    out = {}
    for t in per:
        last = per[t][current[t]][-5:]
        out[t] = {"gk": spell[(t, current[t])], "last5": [v for v, _ in last],
                  "info5": [i for _, i in last]}
    return out


# Миналият сезон като предсезонна сила за прогнозата на спасяванията (2026-10-16).
# Само там, където имаме архив с точни удари; останалите лиги работят както преди.
SAVES_PRIOR_ARCHIVE = {
    "EPL": "sofa_epl_25-26.db",
    "LaLiga": "sofa_laliga_25-26.db",
    "Ligue1": "sofa_ligue1_25-26.db",
    "Bundesliga": "sofa_bundesliga_old-vault.db",   # други имена на отборите -> fuzzy съпоставяне
}


def prior_season_shots(league: str, current_teams: list[str]) -> tuple[dict, bool]:
    """({отбор: (точни удари за, точни удари срещу)} спрямо средното на лигата през
    миналия сезон, има ли архив изобщо). Отбор с <15 мача в архива (напр. гост от
    европейски турнир) или без съвпадение на името (новак) липсва в речника."""
    fname = SAVES_PRIOR_ARCHIVE.get(league)
    path = DATA_DIR / "archive" / fname if fname else None
    if not path or not path.exists():
        return {}, False
    con = sqlite3.connect(path)
    try:
        rows = con.execute("""
            SELECT m.home, m.away, sh.sot, sa.sot FROM matches m
            JOIN stats sh ON sh.match_id = m.match_id AND sh.team = m.home AND sh.period = 'full'
            JOIN stats sa ON sa.match_id = m.match_id AND sa.team = m.away AND sa.period = 'full'
            WHERE m.hg IS NOT NULL AND sh.sot IS NOT NULL AND sa.sot IS NOT NULL""").fetchall()
    finally:
        con.close()
    agg: dict[str, list] = {}
    for h, a, hs, as_ in rows:
        for t, sf, sa_ in ((h, hs, as_), (a, as_, hs)):
            x = agg.setdefault(t, [0, 0.0, 0.0])
            x[0] += 1; x[1] += sf; x[2] += sa_
    n_all = sum(x[0] for x in agg.values())
    if not n_all:
        return {}, False
    lm = sum(x[1] for x in agg.values()) / n_all
    old = {t: (x[1] / x[0] / lm, x[2] / x[0] / lm) for t, x in agg.items() if x[0] >= 15}
    # имената: точно съвпадение, иначе fuzzy (старата Бундеслига е с други имена)
    missing = [t for t in current_teams if t not in old]
    nmap = {}
    if missing:
        try:
            from fotmob_adapter import build_name_map
            nmap = build_name_map(missing, sorted(old)) or {}
        except Exception:
            nmap = {}
    out = {}
    for t in current_teams:
        k = t if t in old else nmap.get(t)
        if k in old:
            out[t] = old[k]
    return out, True


def _team_saves_entry(r: dict, prior: dict, has_prior: bool) -> dict:
    e = {"n": r["n"], "sf": round(r["sot_f"], 3), "sa": round(r["sot_a"], 3),
         "sv": round(r["saves"], 3)}
    if r["team"] in prior:
        e["pf"], e["pa"] = round(prior[r["team"]][0], 3), round(prior[r["team"]][1], 3)
    elif has_prior:
        e["nw"] = 1          # няма го в миналогодишния архив = новак в лигата
    return e


def saves_forecast_data(rows: list[dict], keepers: dict | None = None,
                        side_sot: tuple[float, float] | None = None,
                        prior: dict | None = None, has_prior: bool = False) -> dict:
    """Входни данни за клиентската прогноза на вратарските спасявания (2026-10-16).
    Отборни средни на мач (за/срещу точни удари, спасявания) + лигови средни за
    свиване към средното. Самата формула е в JS (renderSaves): мултипликативна,
    с домакин/гост фактор. Бектест walk-forward върху 3 362 вратарски изхода:
    MAE 1.48 спасявания срещу 1.51 за лигово средно."""
    rows = [r for r in rows if r.get("n")]
    if not rows:
        return {}
    tot_n = sum(r["n"] for r in rows)
    mu = sum(r["sot_f"] * r["n"] for r in rows) / tot_n          # точни удари на отбор/мач
    faced = sum(r["sot_a"] * r["n"] for r in rows)
    p0 = (sum(r["saves"] * r["n"] for r in rows) / faced) if faced else 0.7
    mh, ma = side_sot if side_sot and side_sot[0] and side_sot[1] else (mu, mu)
    return {"mu": round(mu, 3), "p0": round(p0, 4), "mh": round(mh, 3), "ma": round(ma, 3),
            "k": keepers or {},
            "t": {r["team"]: _team_saves_entry(r, prior or {}, has_prior) for r in rows}}


def goalkeeper_saves_html(rows):
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        out.append(
            f"<tr class='ref-row{cls}'><td>{i}</td><td class='tname'>{esc(r['player'])}</td>"
            f"<td>{esc(r['team'])}</td><td>{r['apps']}</td><td>{r['saves']}</td>"
            f"<td>{r['per_match']:.2f}</td></tr>")
    return "\n".join(out)


def blown_html(blown, league):
    # 'event-row', НЕ 'team-row' (2026-08-16): тази таблица показва само
    # отборите, на които се е случило събитието — почти никога не съвпада с
    # избрания чифт в picker-а, значи 'team-row' би избледнявал ЦЕЛИЯТ ред
    # (tr.dim { opacity: .25 }) почти винаги, независимо от контраста на
    # текста. Огледално и в late_goals_html() по-долу.
    rows = []
    for r in blown:
        match_list = "; ".join(
            f"{esc(m['opp'])} ({m['date']}) <b>HT {m['ht']}→FT {m['ft']}</b>{' *' if m['og'] else ''}"
            for m in r["matches"])
        rows.append(f"<tr class='event-row' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
                    f"<td class='tname'>{esc(r['team'])}</td><td>{r['count']}</td>"
                    f"<td class='detail'>{match_list}</td></tr>")
    return "\n".join(rows)


def late_goals_html(rows, league):
    # ГРУПИРАНО ПО МАЧ (2026-09-19, по искане на потребителя): при 2+ голa
    # от 85' нататък в СЪЩИЯ мач, отборът/датата/страната се пишат само
    # веднъж, минутите се редят със запетая — вместо "Отбор (дата) 87' —
    # домакин; Отбор (дата) 89' — домакин; ..." за всеки отделен гол.
    # r["matches"] е плосък списък ЕДИН РЕД НА ГОЛ (виж sofa_late_goals()),
    # групираме тук по (opp, date, venue), пазейки реда на първа поява.
    out = []
    for r in rows:
        home_n = sum(1 for m in r["matches"] if m["venue"] == "домакин")
        away_n = len(r["matches"]) - home_n
        grouped: dict[tuple, list[str]] = {}
        for m in r["matches"]:
            grouped.setdefault((m["opp"], m["date"], m["venue"]), []).append(m["minute"])
        match_list = "; ".join(
            f"{esc(opp)} ({date}) <b>{','.join(minutes)}</b> — {venue}"
            for (opp, date, venue), minutes in grouped.items())
        out.append(f"<tr class='event-row' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
                   f"<td class='tname'>{esc(r['team'])}</td><td>{r['count']}</td>"
                   f"<td>{home_n}-{away_n}</td>"
                   f"<td class='detail'>{match_list}</td></tr>")
    return "\n".join(out)


def early_goals_html(rows, league):
    out = []
    for i, r in enumerate(rows, 1):
        cls = rank_cls(i)
        cells = "".join(f"<td>{c}</td>" for c in r["buckets"])
        out.append(f"<tr class='team-row{cls}' data-league=\"{league}\" data-team=\"{esc(r['team'])}\">"
                   f"<td>{i}</td><td class='tname'>{esc(r['team'])}</td><td>{r['mp']}</td>"
                   f"{cells}<td>{r['rate']:.2f}</td></tr>")
    return "\n".join(out)


def _pct(p) -> str:
    return f"{p:.0%}" if p is not None else "—"


def _pct_odds(p) -> str:
    """% + честен (break-even) коефициент 1/вероятност до него — по искане на
    потребителя (2026-08-24), огледално на fairOdds()/"71% (1.42)" конвенцията
    вече добавена в JS бързата симулация, и на dixon_coles.fair()'s CLI формат."""
    if p is None:
        return "—"
    odds = f"{1 / p:.2f}" if p > 0.001 else "—"
    return f"{p:.0%} <span class='modelOdds'>({odds})</span>"


def _model_row(label: str, m: dict | None, reason: str = "няма данни", n_suffix: str = "") -> str:
    if m is None:
        return f"<tr><td class='mLabel'>{label}</td><td colspan='5' class='note'>{reason}</td></tr>"
    over = _pct_odds(m.get("p_over25")) if "p_over25" in m else "—"
    btts = _pct_odds(m.get("p_btts")) if "p_btts" in m else "—"
    return (f"<tr><td class='mLabel'>{label}{n_suffix}</td>"
           f"<td>{_pct_odds(m['p_home'])}</td><td>{_pct_odds(m['p_draw'])}</td><td>{_pct_odds(m['p_away'])}</td>"
           f"<td>{over}</td><td>{btts}</td></tr>")


def model_b_panel_html(mb: dict | None) -> str:
    """Пълен диагностичен панел на Модел Б (потребителският heuristic) —
    добавен като разгъваем <details> под всяка карта с прогноза, по искане
    на потребителя (2026-08-01, вариант 2 — пази целия контекст, не само
    финалните Над2.5/ГГ числа)."""
    if mb is None:
        return ""
    home, away = mb["home"], mb["away"]
    h2h, flags, form, venue = mb["h2h"], mb["flags"], mb["form"], mb["venue"]
    vt, halves = mb["venue_tier"], mb["halves"]
    over, btts = mb["over25"], mb["btts"]
    vg = mb["venue_goals"]

    flag_lines = []
    if flags["home_blown"]:
        flag_lines.append(f"<div class='mbFlag'>⚑ {esc(home)} има {flags['home_blown']} мач(а) с поведа ≥2 гола без победа</div>")
    if flags["away_blown"]:
        flag_lines.append(f"<div class='mbFlag'>⚑ {esc(away)} има {flags['away_blown']} мач(а) с поведа ≥2 гола без победа</div>")

    tier_lines = []
    if vt["home_overall_rank"] is not None:
        w, d, l = vt["home_above"]
        w2, d2, l2 = vt["home_below"]
        tier_lines.append(f"<div>{esc(home)} у дома — срещу над него: {w}-{d}-{l} · срещу под него: {w2}-{d2}-{l2} "
                          f"<span class='note'>(общо класиране #{vt['home_overall_rank']})</span></div>")
    if vt["away_overall_rank"] is not None:
        w, d, l = vt["away_above"]
        w2, d2, l2 = vt["away_below"]
        tier_lines.append(f"<div>{esc(away)} навън — срещу над него: {w}-{d}-{l} · срещу под него: {w2}-{d2}-{l2} "
                          f"<span class='note'>(общо класиране #{vt['away_overall_rank']})</span></div>")
    if vt["relevant_note"]:
        w, d, l = vt["relevant_wdl"]
        tier_lines.append(f"<div class='mbFlag'>{esc(vt['relevant_note'])} — релевантна форма на {esc(home)} "
                          f"у дома срещу такива съперници: {w}-{d}-{l}</div>")

    swap_line = ""
    if halves["swap"]:
        swap_line = (f"<div class='mbFlag'>⚑ ФЛАГ ОБРЪЩАЛКА ({esc(halves['strength'])}) — "
                    f"по-силна страна: {esc(halves['strong_side'] or '—')}</div>")

    pick_label = {"1": home, "2": away, "X": "Равен"}[mb["pick_1x2"]]
    # Модел Б вече може да няма pick (неутрална зона) — показва се "—", не "None"
    over_lbl = over["pick"] or "—"
    btts_lbl = btts["pick"] or "—"
    summary = (f"Модел Б (диагностично) — Над2.5: <b>{over_lbl}</b> · "
              f"ГГ: <b>{btts_lbl}</b> · 1X2 lean: {esc(pick_label)} ({esc(mb['confidence'])})")

    return f"""
<details class="mbPanel">
<summary>{summary}</summary>
<div class="mbBody">
  <div><b>H2H</b> ({h2h['n']} срещи, тежест={h2h['weight']:.0%}): lean={esc(h2h['lean'] or 'няма')}
       · Над2.5={h2h['over25_pct']:.0%} · ГГ={h2h['btts_pct']:.0%}</div>
  {''.join(flag_lines)}
  <div><b>Форма:</b> 5 мача — {esc(home)}={form['home_rank5']}, {esc(away)}={form['away_rank5']}
       | 10 мача — {esc(home)}={form['home_rank10']}, {esc(away)}={form['away_rank10']}
       → {esc(form['note'])}, winner={esc(form['winner'] or '—')}</div>
  <div><b>Домакин/Гост:</b> {esc(home)}(дом)={venue['home_rank']}
       [{venue['home_wdl'][0]}-{venue['home_wdl'][1]}-{venue['home_wdl'][2]} у дома] срещу
       {esc(away)}(гост)={venue['away_rank']}
       [{venue['away_wdl'][0]}-{venue['away_wdl'][1]}-{venue['away_wdl'][2]} навън] → winner={esc(venue['winner'] or '—')}</div>
  {''.join(tier_lines)}
  <div><b>Полувремена:</b> 1-во — {esc(home)}={halves['home_rank_h1']}, {esc(away)}={halves['away_rank_h1']}
       | 2-ро — {esc(home)}={halves['home_rank_h2']}, {esc(away)}={halves['away_rank_h2']}
       {'' if halves['swap'] else f"→ без swap, winner={esc(halves['winner'] or '—')}"}</div>
  {swap_line}
  <div class="note">(диагностично, НЕ препоръчан pick) 1X2: {mb['pick_1x2']} ({esc(mb['confidence'])})
       [гласове: домакин={mb['votes_home']}, гост={mb['votes_away']}] — за 1X2 ползвай Dixon-Coles</div>
  <div><b>Над2.5:</b> {esc(home)}={over['home_rank']}, {esc(away)}={over['away_rank']} (гап={over['gap']})
       → {over['pick'] or '—'} <span class="note">[{esc(over['source'])}]</span></div>
  <div><b>ГГ:</b> {esc(home)}={btts['home_rank']}, {esc(away)}={btts['away_rank']} (гап={btts['gap']})
       → {btts['pick'] or '—'} <span class="note">[{esc(btts['source'])}]</span></div>
  <div class="note">Venue-специфична форма за вкарване: {esc(home)}(у дома)={vg['home_avg']:.2f} гола/мач
       | {esc(away)}(навън)={vg['away_avg']:.2f} гола/мач</div>
</div>
</details>"""


def model_g_panel_html(mg: dict | None, home: str, away: str) -> str:
    """Разгъваем панел за точковата разбивка на Модел Г (по 6-те фактора),
    огледално на model_b_panel_html() - потребителят вижда И крайния 1X2 ред
    в главната таблица (модел Г дава РЕАЛНИ вероятности, за разлика от
    диагностичния Модел Б), И пълния разбор кой фактор колко точки е дал."""
    if mg is None:
        return ""
    fh, fa = mg["factors_home"], mg["factors_away"]
    pick_label = {"1": home, "2": away, "X": "Равен"}[mg["pick_1x2"]]
    summary = (f"Модел Г (точков разбор) — lean: <b>{esc(pick_label)}</b> "
              f"({_pct(mg['p_home'])} / {_pct(mg['p_draw'])} / {_pct(mg['p_away'])})")
    rows = "".join(
        f"<tr><td class='mLabel'>{label}</td><td>{fh[key]:.1f}</td><td>{fa[key]:.1f}</td></tr>"
        for key, label in (("форма", "Форма (25)"), ("домакинство", "Домакинство (20)"),
                          ("гол_баланс", "Гол баланс (20)"), ("състав", "Състав/контузии (15)"),
                          ("психология", "Психология/мотивация (10)"), ("h2h", "H2H (10)")))
    return f"""
<details class="mbPanel">
<summary>{summary}</summary>
<div class="mbBody">
<table class="predTable">
<tr><th>Фактор</th><th>{esc(home)}</th><th>{esc(away)}</th></tr>
{rows}
<tr class="avgRow"><td class="mLabel">Общо</td><td>{fh['общо']:.1f}</td><td>{fa['общо']:.1f}</td></tr>
</table>
<div class="note">Състав/контузии вече се смята автоматично от отсъстващите титуляри
(виж флага под таблицата по-горе, ако има) — неутрално (7.5/7.5) само ако никой от
двата отбора няма отсъстващ титуляр. Психология/мотивация вече се смята автоматично
от резултатите в последните 5 мача (W=3/D=1/L=0 точки, дял от 10) — ръчен вход
(--home-psych/--away-psych през CLI) продължава да го измества, ако е подаден.</div>
</div>
</details>"""


LOW_CONFIDENCE_THRESHOLD = 0.40  # по искане на потребителя (2026-08-09): ако И 1, И 2
# паднат под тази граница в средния ред, никоя страна няма реално предимство — не се
# "избира" механично по-голямото от двете близки числа, флагва се като неясно/възможно X.


def _avg_1x2_row(*models: dict | None) -> str:
    """Среден % по 1/X/2 през наличните модели (DC/v1/v2/Модел Г) — по искане на
    потребителя (2026-08-01), само за 1X2 знаците, не за Над2.5/ГГ. Флагва ниска
    увереност (2026-08-09), ако и 1, и 2 са под LOW_CONFIDENCE_THRESHOLD — конкретен
    пример на потребителя: IFK Гьотеборг–Калмар, DC 35/29/36, средно 38/26/36 —
    нито 1, нито 2 реално води, механичното "по-голямото от двете" подвежда."""
    vals = [m for m in models if m is not None]
    if not vals:
        return ""
    avg_home = sum(m["p_home"] for m in vals) / len(vals)
    avg_draw = sum(m["p_draw"] for m in vals) / len(vals)
    avg_away = sum(m["p_away"] for m in vals) / len(vals)
    low_conf = avg_home < LOW_CONFIDENCE_THRESHOLD and avg_away < LOW_CONFIDENCE_THRESHOLD
    flag = (f" <span class='lowConf' title='И двете страни под {LOW_CONFIDENCE_THRESHOLD:.0%} — "
           f"никоя няма реално предимство, не се избира механично'>⚠ неясно</span>") if low_conf else ""
    return (f"<tr class='avgRow{' lowConfRow' if low_conf else ''}'><td class='mLabel'>Средно{flag}</td>"
           f"<td>{_pct_odds(avg_home)}</td><td>{_pct_odds(avg_draw)}</td><td>{_pct_odds(avg_away)}</td>"
           f"<td>—</td><td>—</td></tr>")


def missing_players_html(missing_home: list[dict] | None, missing_away: list[dict] | None,
                         home: str, away: str) -> str:
    """Контузени/наказани ОБИЧАЙНИ титуляри (2026-08-09, по искане на
    потребителя: "искам missingPlayers да се взима предвид, така ще знаем ако
    добър играч е контузен") — вече филтрирано до титуляри в next_fixtures.py
    (lineup_impact.missing_regular_starters), тук само рендерира. Показва се директно под
    таблицата, не скрито в панел — потребителят иска да го вижда веднага."""
    if not missing_home and not missing_away:
        return ""
    lines = []
    for team, missing in ((home, missing_home), (away, missing_away)):
        if not missing:
            continue
        names = "; ".join(f"{esc(m['name'])} ({esc(m['reason'])})" for m in missing)
        lines.append(f"<div class='mbFlag'>⚑ {esc(team)} без титуляр(и): {names}</div>")
    return f"<div class='note' style='margin-top:8px'>{''.join(lines)}</div>"


AGREED_MIN_PROB = 0.50   # и двата модела (DC и Elo) трябва да дават СЪГЛАСНИЯ знак с вероятност над това (2026-10-26)


def agreed_1x2_rows(con, leagues, where_sql: str, params: tuple, limit: int = 5) -> list[tuple]:
    """Топ мачове за 1X2, при които Dixon-Coles и Opta-Elo са СЪГЛАСНИ за знака (2026-10-26, по
    искане на потребителя: "когато двата модела са съгласни, тогава да се зачита като прогноза").
    Знакът на всеки модел е най-вероятният от 1/X/2; подрежда се по СРЕДНАТА вероятност на двата
    за този знак. Backtest (492 мача): съгласни в 65% от мачовете, познати 51.6% (DC самостоятелно
    46.5%); при средна вероятност над 70% — 87% (n=31). Връща (лига, дом, гост, дата, знак, p_dc,
    p_elo, p_средно). `where_sql` е върху dc реда `d` (напр. статус/дата/recorded_at)."""
    if not leagues:
        return []
    ph = ",".join("?" * len(leagues))
    rows = con.execute(
        f"""SELECT d.league, d.home, d.away, d.match_date,
                   d.p_home, d.p_draw, d.p_away, e.p_home, e.p_draw, e.p_away
            FROM model_predictions d
            JOIN model_predictions e ON e.event_id = d.event_id AND e.model = 'opta_elo'
            WHERE d.model = 'dc' AND d.p_home IS NOT NULL AND e.p_home IS NOT NULL
              AND d.league IN ({ph}) AND {where_sql}""",
        (*leagues, *params)).fetchall()
    out = []
    for lg, h, a, d, dh, dd, da, eh, ed, ea in rows:
        sd = max(("1", dh), ("X", dd), ("2", da), key=lambda x: x[1])
        se = max(("1", eh), ("X", ed), ("2", ea), key=lambda x: x[1])
        if sd[0] != se[0] or sd[1] <= AGREED_MIN_PROB or se[1] <= AGREED_MIN_PROB:
            continue
        out.append((lg, h, a, d, sd[0], sd[1], se[1], (sd[1] + se[1]) / 2))
    out.sort(key=lambda r: -r[7])
    return out[:limit]


def leagues_min_rounds(min_rounds: int = 5) -> set[str]:
    """Лиги с поне `min_rounds` реално изиграни кръга (същото като MIN_ROUNDS в top_picks_alert.py)."""
    out = set()
    for lg, db in LEAGUES:
        if not Path(db).exists():
            continue
        c = sqlite3.connect(db)
        try:
            n = c.execute("SELECT COUNT(DISTINCT round) FROM matches WHERE round IS NOT NULL").fetchone()[0]
        finally:
            c.close()
        if n is not None and n >= min_rounds:
            out.add(lg)
    return out


def agreed_combo_rows(league_data: dict, leagues: set, kind: str, days: int = 7, limit: int = 5) -> list[tuple]:
    """Топ мачове за "знак И Над 2.5" (kind='over25') или "знак И Г/Г" (kind='btts') — 2026-10-26,
    по искане на потребителя. Знакът е този, на който Dixon-Coles и Opta-Elo са СЪГЛАСНИ (като при
    топ 1X2); вероятността е СЪВМЕСТНАТА от Dixon-Coles матрицата (знак и пазар заедно, не
    произведение на двете). Само предстоящи мачове до `days` дни. Връща (лига, дом, гост, дата, знак,
    p_съвместна, p_знак_DC, p_знак_Elo, модел_за_головете). `league_data` е резултатът на gather_sofa (model_comparison)."""
    import time as _t
    now = _t.time()
    out = []
    for lg, d in league_data.items():
        if lg not in leagues:
            continue
        for r in d.get("model_comparison") or []:
            dc, el = r.get("dc"), r.get("opta_elo")
            ts = r.get("kickoff_ts")
            if not dc or not el or not ts or ts < now or ts > now + days * 86400:
                continue
            # пазарът на головете: Модел В v1, когато го има (има състав), иначе Dixon-Coles —
            # по искане на потребителя (2026-10-26): v1 е по-добър за Над2.5/Г-Г (186 общи мача:
            # Над2.5 57.5% срещу 52.7%, Г/Г 51.6% срещу 51.1%, по-нисък Brier и на двата пазара)
            v1 = r.get("v1")
            if v1 and v1.get("joint"):
                joint, src = v1["joint"], "v1"
            elif dc.get("joint"):
                joint, src = dc["joint"], "DC"
            else:
                continue
            sd = max(("1", dc["p_home"]), ("X", dc["p_draw"]), ("2", dc["p_away"]), key=lambda x: x[1])
            se = max(("1", el["p_home"]), ("X", el["p_draw"]), ("2", el["p_away"]), key=lambda x: x[1])
            if sd[0] != se[0] or sd[1] <= AGREED_MIN_PROB or se[1] <= AGREED_MIN_PROB:
                continue
            out.append((lg, r["home"], r["away"], r["date"], sd[0], joint[kind][sd[0]], sd[1], se[1], src))
    out.sort(key=lambda x: -x[5])
    return out[:limit]


def agreement_calibration() -> tuple[list[tuple], int]:
    """Дял познати знаци 1X2, когато Dixon-Coles и Opta-Elo са съгласни, по ниво на средната им
    вероятност (<50%, 50-60%, 60-70%, 70%+) — за графиката в началото на страницата (2026-10-27).
    Чете сверените прогнози; връща ([(етикет, % познати, брой)], общо мачове)."""
    if not PRED_DB_PATH.exists():
        return [], 0
    con = sqlite3.connect(PRED_DB_PATH)
    try:
        rows = con.execute(
            """SELECT d.p_home, d.p_draw, d.p_away, e.p_home, e.p_draw, e.p_away, d.actual_hg, d.actual_ag
               FROM model_predictions d JOIN model_predictions e
                 ON e.event_id = d.event_id AND e.model = 'opta_elo'
               WHERE d.model = 'dc' AND d.status = 'resolved' AND e.status = 'resolved'
                 AND d.p_home IS NOT NULL AND e.p_home IS NOT NULL AND d.actual_hg IS NOT NULL""").fetchall()
    finally:
        con.close()
    edges = [(0.0, 0.5, "под 50%"), (0.5, 0.6, "50–60%"), (0.6, 0.7, "60–70%"), (0.7, 1.01, "над 70%")]
    acc = {lab: [0, 0] for _, _, lab in edges}
    total = 0
    for dh, dd, da, eh, ed, ea, hg, ag in rows:
        sd = max(("1", dh), ("X", dd), ("2", da), key=lambda x: x[1])
        se = max(("1", eh), ("X", ed), ("2", ea), key=lambda x: x[1])
        if sd[0] != se[0]:
            continue
        res = "1" if hg > ag else ("X" if hg == ag else "2")
        avg = (sd[1] + se[1]) / 2
        for lo, hi, lab in edges:
            if lo <= avg < hi:
                acc[lab][0] += (sd[0] == res)
                acc[lab][1] += 1
                break
        total += 1
    return [(lab, (h / n * 100 if n else 0.0), n) for lab, (h, n) in acc.items()], total


def hero_html(n_leagues: int) -> str:
    """Начален екран: заглавие + доказателство (светеща графика на калибрацията) — 2026-10-27."""
    pts, total = agreement_calibration()
    resolved = 0
    if PRED_DB_PATH.exists():
        c = sqlite3.connect(PRED_DB_PATH)
        try:
            resolved = c.execute("SELECT COUNT(*) FROM model_predictions WHERE status='resolved'").fetchone()[0]
        finally:
            c.close()
    resolved_s = f"{resolved:,}".replace(",", " ")
    n_models = len(PRED_MODELS) - (0 if SHOW_V2 else 1)
    svg = ""
    if len(pts) >= 2 and total:
        W, H, L, R, T, B = 520, 280, 40, 26, 26, 52
        iw, ih = W - L - R, H - T - B
        X = lambda i: L + iw * (i / (len(pts) - 1))
        Y = lambda v: T + ih * (1 - v / 100)
        grid = "".join(f"<line class='gl' x1='{L}' x2='{W - R}' y1='{Y(t):.1f}' y2='{Y(t):.1f}'/>"
                       f"<text class='ax' x='{L - 8}' y='{Y(t) + 4:.1f}' text-anchor='end'>{t}%</text>" for t in (0, 25, 50, 75, 100))
        line = " ".join(f"{'L' if i else 'M'}{X(i):.1f},{Y(v):.1f}" for i, (_, v, _) in enumerate(pts))
        area = f"{line} L{X(len(pts) - 1):.1f},{Y(0):.1f} L{X(0):.1f},{Y(0):.1f} Z"
        dots = "".join(f"<circle cx='{X(i):.1f}' cy='{Y(v):.1f}' r='{6 if i == len(pts) - 1 else 4}' fill='var(--surface)' stroke='var(--primary)' stroke-width='2.4'/>"
                       for i, (_, v, _) in enumerate(pts))
        labels = "".join(f"<text class='ax' x='{X(i):.1f}' y='{H - 26}' text-anchor='middle'>{lab}</text>"
                         f"<text class='ax' x='{X(i):.1f}' y='{H - 10}' text-anchor='middle' opacity='.75'>n={n}</text>"
                         for i, (lab, _, n) in enumerate(pts))
        last = pts[-1]
        svg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Дял познати знаци по ниво на средната вероятност">'
               '<defs><linearGradient id="hrAr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--primary)" stop-opacity=".34"/><stop offset="1" stop-color="var(--primary)" stop-opacity="0"/></linearGradient>'
               '<filter id="hrGw" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="4.5"/></filter></defs>'
               f'{grid}'
               f'<line x1="{L}" x2="{W - R}" y1="{Y(33.3):.1f}" y2="{Y(33.3):.1f}" stroke="var(--amber)" stroke-width="1" stroke-dasharray="4 5" opacity=".8"/>'
               f'<text class="ax" x="{W - R}" y="{Y(33.3) - 6:.1f}" text-anchor="end" style="fill:var(--amber)">случаен избор ≈ 33%</text>'
               f'<path d="{area}" fill="url(#hrAr)"/>'
               f'<path d="{line}" fill="none" stroke="var(--primary)" stroke-width="5" stroke-linejoin="round" stroke-linecap="round" filter="url(#hrGw)" opacity=".7"/>'
               f'<path d="{line}" fill="none" stroke="var(--primary)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>'
               f'{dots}'
               f'<text class="ptl" x="{X(len(pts) - 1) - 12:.1f}" y="{Y(last[1]) - 14:.1f}" text-anchor="end">{last[1]:.0f}% при {last[2]} мача</text>'
               f'{labels}</svg>')
    chart = (f'<div class="card chartcard"><h3>Колкото по-уверени са двата модела, толкова по-често излиза</h3>'
             f'<p class="sub">Знак 1X2, когато Dixon-Coles и Opta-Elo са съгласни · {total} мача · по средна вероятност</p>{svg}</div>'
             if svg else "")
    return f"""
<section class="hero">
<div>
<div class="eyebrow"><i></i> Футболни вероятности · {n_leagues} лиги</div>
<h1>Вероятности с <em>честни коефициенти</em>.</h1>
<p class="lead">Без съвети и без обещания. За всеки мач виждаш какво смятат моделите, колко са съгласни и как са се справяли досега.</p>
<div class="facts"><span><b>{resolved_s}</b>сверени прогнози</span><span><b>{n_models}</b>модела</span><span><b>FotMob</b>източник на данните</span></div>
<div class="heroBtns"><a class="btn" href="#topPicksSection">Виж топ прогнозите</a><a class="btn ghost" href="#predSectionTop">Прогнози за кръга</a></div>
</div>
{chart}
</section>"""


def top_picks_page_html(league_data: dict | None = None) -> str:
    """Топ 5 мача за 1X2 (Dixon-Coles), Над 2.5 и Г/Г — същите класации като Telegram
    съобщението (top_picks_alert.py), но на страницата (2026-10-26, по искане на
    потребителя). Само за предстоящи мачове до 7 дни напред, записани в
    model_predictions.db (`record_predictions.py`, т.е. при обновяване с --record), и само
    лиги с поне 5 изиграни кръга. Над 2.5/Г/Г: Модел В v1 подрежда по вероятност, Модел Б
    трябва да е съгласен със същия pick (както в Telegram). Чете САМО SQL, затова не е в
    кеша на gather_sofa и се обновява при всяко сглобяване на страницата."""
    if not PRED_DB_PATH.exists():
        return ""
    leagues = leagues_min_rounds()
    if not leagues:
        return ""
    now = datetime.datetime.now()
    lo, hi = now.strftime("%Y-%m-%d %H:%M"), (now + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    ph = ",".join("?" * len(leagues))
    con = sqlite3.connect(PRED_DB_PATH)
    try:
        rows_1x2 = agreed_1x2_rows(
            con, leagues, "d.status='pending' AND d.match_date >= ? AND d.match_date <= ?", (lo, hi))

        def market(prob_col, pick_col, pick_value):
            return con.execute(
                f"""SELECT pm.league, pm.home, pm.away, pm.match_date, pm.{prob_col}
                    FROM model_predictions pm
                    JOIN model_predictions mb ON mb.event_id = pm.event_id AND mb.model = 'model_b'
                    WHERE pm.model='v1' AND pm.status='pending' AND pm.match_date >= ? AND pm.match_date <= ?
                      AND pm.{prob_col} IS NOT NULL AND mb.{pick_col} = ? AND pm.league IN ({ph})
                    ORDER BY pm.{prob_col} DESC LIMIT 5""",
                (lo, hi, pick_value, *leagues)).fetchall()
        rows_over = market("p_over25", "pick_over25", "OVER")
        rows_btts = market("p_btts", "pick_btts", "YES")
    finally:
        con.close()
    rows_so = agreed_combo_rows(league_data or {}, leagues, "over25")
    rows_sb = agreed_combo_rows(league_data or {}, leagues, "btts")
    if not (rows_1x2 or rows_over or rows_btts or rows_so or rows_sb):
        return ""

    def lg_name(l):
        return esc(LEAGUE_COUNTRY.get(l, l))

    def li(i, home, away, league, date, p, extra="", sign=None):
        extra_html = f" · {esc(extra)}" if extra else ""
        sign_html = f"<span class='sign'>{esc(sign)}</span>" if sign else ""
        odds = f"{1 / p:.2f}" if p > 0.001 else "—"
        return (f"<li style='--w:{min(p * 100 * (1.0 if sign and not extra.startswith('знак') else 1.6), 100):.0f}%'>"
                f"<span class='tpN'>{i}</span><span class='tpMatch'><b>{sign_html}{esc(home)} — {esc(away)}</b>"
                f"<span class='tpMeta'>{lg_name(league)} · {esc(date[8:10])}.{esc(date[5:7])} {esc(date[11:16])}{extra_html}</span></span>"
                f"<span class='tpVal'><strong>{p * 100:.0f}%</strong><small>{odds}</small></span></li>")

    def block(title, note, items):
        body = "".join(items) if items else "<li class='tpEmpty'>Няма мачове, които да отговарят на условията.</li>"
        return (f"<div class='tpCol'><h3>{title}</h3><ul class='tpList'>{body}</ul>"
                f"<p class='note'>{note}</p></div>")

    def odds_pct(p):
        return f"{p * 100:.0f}% <span class='odds'>({1 / p:.2f})</span>" if p > 0.001 else "—"

    items_1x2 = [li(i, h, a, lg, d, p_avg, f"DC {p_dc * 100:.0f}% · Elo {p_el * 100:.0f}%", sign=sign)
                 for i, (lg, h, a, d, sign, p_dc, p_el, p_avg) in enumerate(rows_1x2, 1)]

    def combo_items(rows, label):
        return [li(i, h, a, lg, d, p, f"знак: DC {pd_ * 100:.0f}% · Elo {pe_ * 100:.0f}% · голове: {src}", sign=sg)
                for i, (lg, h, a, d, sg, p, pd_, pe_, src) in enumerate(rows, 1)]
    items_so, items_sb = combo_items(rows_so, "Над 2.5"), combo_items(rows_sb, "Г/Г")
    items_over = [li(i, h, a, lg, d, p) for i, (lg, h, a, d, p) in enumerate(rows_over, 1)]
    items_btts = [li(i, h, a, lg, d, p) for i, (lg, h, a, d, p) in enumerate(rows_btts, 1)]
    return f"""
<section class="card topPicks" id="topPicksSection">
<h2>Топ прогнози за предстоящите мачове</h2>
<div class="tpGrid">
{block("Знак 1X2 (DC + Elo съгласни)", "Знак, на който Dixon-Coles и Opta-Elo са съгласни и двата над 50%; подредено по средната им вероятност.", items_1x2)}
{block("Над 2.5", "Модел В v1 по вероятност, Модел Б съгласен.", items_over)}
{block("Г/Г", "Модел В v1 по вероятност, Модел Б съгласен.", items_btts)}
{block("Знак и Над 2.5", "Знак, на който DC и Elo са съгласни; и двата модела над 50%; вероятността е съвместната (знак и над 2.5 заедно) от Модел В v1, а когато мачът още няма състав — от Dixon-Coles.", items_so)}
{block("Знак и Г/Г", "Знак, на който DC и Elo са съгласни; и двата модела над 50%; вероятността е съвместната (знак и двата да вкарат заедно) от Модел В v1, а когато мачът още няма състав — от Dixon-Coles.", items_sb)}
</div>
<p class="note">Само мачове до 7 дни напред, от лиги с поне 5 изиграни кръга; вероятност и честен коефициент (без марж).
Обновява се при всяко обновяване на лигите с записване на прогнозите — същите класации идват и в Telegram.</p>
</section>"""


def track_record_html() -> str:
    """Обобщена точност на всеки модел досега (2026-08-09, по искане на
    потребителя — "как може да направим страницата по-жива", избрано заедно с
    countdown-а по-долу). Чете model_predictions.db (ВСИЧКИ някога разрешени
    прогнози, всички лиги заедно) — ЕДНА глобална секция, не по лига, за
    разлика от predSection. Същата SQL логика като resolve_predictions.py's
    конзолен обобщаващ печат, само рендерирана в HTML вместо в терминала."""
    if not PRED_DB_PATH.exists():
        return ""
    con = sqlite3.connect(PRED_DB_PATH)
    try:
        rows = con.execute(
            "SELECT model, COUNT(*), SUM(hit_1x2), SUM(hit_over25), SUM(hit_btts), "
            "SUM(CASE WHEN pick_over25 IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pick_btts IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM model_predictions WHERE status='resolved' GROUP BY model"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return ""
    by_model = {r[0]: r for r in rows}
    items = []
    for m in PRED_MODELS:
        if m == 'v2' and not SHOW_V2:
            continue
        r = by_model.get(m)
        if not r:
            continue
        _, n, hit1x2, hit_ou, hit_bt, n_ou, n_bt = r
        items.append((PRED_MODEL_LABELS.get(m, m),
                      [((hit1x2 or 0) / n if n else None, hit1x2 or 0, n),
                       ((hit_ou or 0) / n_ou if n_ou else None, hit_ou or 0, n_ou),
                       ((hit_bt or 0) / n_bt if n_bt else None, hit_bt or 0, n_bt)]))
    if not items:
        return ""
    best = [max((it[1][k][0] or 0) for it in items) for k in range(3)]

    def meter(cell, k, label):
        v, h, n = cell
        if v is None:
            return f"<div class='meter' data-l='{label}'><span class='tx'>—</span></div>"
        cls = "fl best" if v == best[k] else "fl"
        return (f"<div class='meter' data-l='{label}'><div class='tr'><div class='{cls}' style='width:{v * 100:.0f}%'></div></div>"
                f"<span class='tx'>{v * 100:.0f}%</span><small>{h}/{n}</small></div>")
    body = "".join(f"<div class='arow'><span class='mn'>{esc(name)}</span>{meter(c[0], 0, '1X2')}{meter(c[1], 1, 'Над 2.5')}{meter(c[2], 2, 'Г/Г')}</div>"
                   for name, c in items)
    return f"""
<section class="card trackSection">
<h2>Точност на моделите досега</h2>
<div class="acc"><div class="arow h"><span>Модел</span><span>1X2</span><span>Над 2.5</span><span>Г/Г</span></div>{body}</div>
<p class="note">Всяка сверена прогноза от всички лиги досега, не само подбрани "pick"-ове — виж
<code>record_predictions.py</code>/<code>resolve_predictions.py</code>. Над 2.5 и Г/Г се броят само когато моделът е дал
прогноза (над 55% или под 45%). За 1X2 случаен избор познава около 33%.</p>
</section>"""


def model_comparison_html(rows: list[dict], all_teams: list[str] | None = None) -> str:
    """Карта на мач, с отделен ред на модел (DC/v1/v2) — по-четимо от
    сбит ред с <br>. DC няма Над2.5/ГГ колони (backtest показа, че не
    добавя нищо съществено там). Виж next_fixtures.py докстринга за spec.

    Чекбоксът пред домакина зарежда двойката в picker-а отдолу. Имената в
    картите идват от Sofascore (next_fixtures.py), а опциите в picker-а — от
    базата; ако конкретно име не съвпада, select.value тихо не сработва —
    затова проверяваме предварително и деактивираме чекбокса вместо да
    оставим бутон, който не прави нищо."""
    if not rows:
        return "<p class='note'>Няма предстоящи фикстури в кеша.</p>"
    known = set(all_teams or [])
    out = []
    for r in rows:
        # Три състояния, не две — виж бележката в next_fixtures.py: липсващ
        # състав преди се показваше като "прогнозен", което подвеждаше защо
        # v1/v2 нямат данни.
        if r.get("confirmed"):
            status = "потвърден състав"
        elif r.get("has_lineup"):
            status = "прогнозен състав"
        else:
            status = "още няма състав"
        # v1/v2 симулират от конкретния стартов състав — без него нямат вход.
        # DC ползва отборни средни, затова продължава да работи.
        lineup_reason = ("няма данни" if r.get("has_lineup")
                         else "чака състав")   # (2026-10-09) без име на източника — вече е FotMob/Sofascore
        # Чекбоксът е премахнат (2026-08-09, по искане на потребителя: "махни
        # тика, само като кликна на мача автоматично да зарежда отборите") -
        # pickable вече е data-атрибут на самата <details>, JS слуша нейния
        # 'toggle' и зарежда picker-а директно при отваряне на картата.
        pickable = r["home"] in known and r["away"] in known
        pick_attrs = (f' data-home="{esc(r["home"])}" data-away="{esc(r["away"])}"'
                     if pickable else "")
        # Показваме броя играчи, от които е изчислено attack_index/n_found —
        # малка извадка (напр. 2 играча) прави числото ненадеждно, по искане
        # на потребителя (2026-08-01, само за прогнозен състав; разширено
        # 2026-08-09 и за потвърден — същият риск от липсващи данни за
        # конкретни играчи (нов трансфер/неуспешна Sofascore заявка) важи и
        # там, отчетът преди го криеше мълчаливо само защото съставът е
        # "потвърден", не защото данните са пълни).
        v1_suffix = v2_suffix = ""
        if status in ("прогнозен състав", "потвърден състав"):
            v1data = r.get("v1")
            if v1data and "home_n_attack" in v1data:
                v1_suffix = f" ({v1data['home_n_attack']}/{v1data['away_n_attack']} играча)"
            v2data = r.get("v2")
            if v2data and "home_n_found" in v2data:
                v2_suffix = f" ({v2data['home_n_found']}/{v2data['away_n_found']} играча)"
        # Цветно оцветяване на статуса на състава (2026-08-09, по искане на
        # потребителя): потвърден = зелено, прогнозен = оранжево, "още няма" -
        # без специален цвят (не носи същия сигнал за увереност).
        status_cls = {"потвърден състав": "statusConfirmed",
                     "прогнозен състав": "statusPredicted"}.get(status)
        status_html = f"<span class='{status_cls}'>{status}</span>" if status_cls else status
        # "На живо" индикатор - СПРЯН (2026-08-09): директната browser fetch
        # към Sofascore's /sport/football/events/live се оказа ненадеждна -
        # Cloudflare връща 403 в повечето случаи (потвърдено многократно на
        # живия сайт), не само от рядко пренатоварване. Оставено изключено
        # до решение с прокси (сървърна заявка през curl_cffi, вместо direct
        # browser fetch) - виж лентата с data-event-id по-долу, все още
        # запазена в HTML в случай, че се върнем към идеята.
        live_badge = ""
        # Countdown до началото (2026-08-09) - ЧИСТО клиентско изчисление от
        # kickoff_ts (Unix epoch, UTC, от Sofascore's startTimestamp) - за
        # разлика от live_badge по-горе, тук няма външна заявка изобщо, значи
        # не може да се блокира от Cloudflare/bot detection. JS-ът го крие
        # автоматично след старта на мача (виж updateCountdowns() долу).
        countdown = (f'<span class="countdown" data-kickoff="{r["kickoff_ts"]}"></span>'
                    if r.get("kickoff_ts") else "")
        # Падащо меню (2026-08-09, по искане на потребителя: "като кликна на
        # мача тогава да ми показва резултата от моделите") - <details> вместо
        # обикновен <div>, огледално на mbPanel/model_g_panel_html по-долу.
        # Затворено по подразбиране - самата карта (отбори, дата, статус на
        # състава, live резултат) остава видима без да разгъваш, само
        # таблицата с моделите и панелите се крият до клик. Отварянето на
        # картата (toggle событие) едновременно зарежда двойката в picker-а
        # долу - виж wireLeague()'s predCard toggle listener.
        out.append(f"""
<details class="predCard"{pick_attrs}>
<summary><span class="predArrow"></span>{esc(r['home'])} — {esc(r['away'])} <span class="predDate">{esc(r['date'])} · {status_html}</span>{countdown}{live_badge}</summary>
<table class="predTable">
<tr><th>Модел</th><th>1</th><th>X</th><th>2</th><th>Над2.5</th><th>ГГ</th></tr>
{_model_row("Dixon-Coles", r.get("dc"))}
{_model_row("Модел В — v1 (xg/xa)", r.get("v1"), lineup_reason, v1_suffix)}
{_model_row("Модел В — v2 (атрибути)", r.get("v2"), lineup_reason, v2_suffix) if SHOW_V2 else ""}
{_model_row("Модел Г (точков)", r.get("model_g"))}
{_model_row("Opta-Elo (бета)", r.get("opta_elo"), r.get("opta_elo_reason", "отбор още няма изигран мач тази сезон"))}
{_avg_1x2_row(r.get("dc"), r.get("v1"), r.get("v2") if SHOW_V2 else None, r.get("model_g"))}
</table>
{missing_players_html(r.get("missing_home"), r.get("missing_away"), r["home"], r["away"])}
{model_b_panel_html(r.get("model_b"))}
{model_g_panel_html(r.get("model_g"), r["home"], r["away"])}
</details>""")
    return "\n".join(out)


def render_league_block(league: str, data: dict, active: bool) -> str:
    style = "" if active else ' style="display:none"'
    team_options = "\n".join(f'<option value="{esc(t)}">{esc(t)}</option>'
                             for t in data["all_teams"])
    half_label = "xG" if data["is_xg_half"] else "Голове"
    # Малка форма за заглавията на колоните ("Вкарани голове" / "Вкарани xG")
    half_word = "xG" if data["is_xg_half"] else "голове"
    # 1X2 по полувреме само за реален HT резултат (xG няма истински W-D-L изход)
    half_wdl_th = "" if data["is_xg_half"] else "<th>1X2</th>"

    # Домакин/Гост по полувреме (добавено 2026-08-02) — само при реален HT резултат
    venue_half_section = ""
    if data.get("home_fh_rows") is not None:
        venue_half_section = f"""
<div>
<h3>Домакин — 1-во полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['home_fh_rows'], league, 'home')}
</table>
</div>
<div>
<h3>Гост — 1-во полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['away_fh_rows'], league, 'away')}
</table>
</div>
<div>
<h3>Домакин — 2-ро полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['home_sh_rows'], league, 'home')}
</table>
</div>
<div>
<h3>Гост — 2-ро полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['away_sh_rows'], league, 'away')}
</table>
</div>"""

    blown_section = ""
    if data["blown"] is not None:
        blown_section = f"""
<section class="card">
<h2 data-section="5"><span class="num">5</span> 📉 Повел на почивката, но загубил</h2>
<table class="blownTable"><tr><th>Отбор</th><th>Брой</th><th>Мачове</th></tr>
{blown_html(data['blown'], league)}
</table>
<p class="note">HT резултатът е точен, не реконструиран: за мачовете до края на септември е от Sofascore, от тогава насам — от FotMob (резултат към 45-ата минута).</p>
</section>"""
    else:
        blown_section = """
<section class="card">
<h2 data-section="5"><span class="num">5</span> 📉 Повел на почивката, но загубил</h2>
<p class="note">Недостъпно за тази лига — Sofascore базата пази само отборна статистика,
без резултат по полувреме (нужен е играчов/периоден HT резултат).</p>
</section>"""

    referees_section = ""
    if data.get("referees"):
        referees_section = f"""
<section class="card">
<h2 data-section="6"><span class="num">6</span> 🟨 Съдии — картони на мач</h2>
<table><tr><th>#</th><th>Съдия</th><th>Мачове</th><th>Ж.К./мач</th><th>Ч.К./мач</th><th>Общо/мач</th></tr>
{referees_html(data['referees'])}
</table>
<p class="note">Реални суми от нашата база (не Sofascore-овата обобщена статистика на съдията).</p>
</section>"""
    else:
        referees_section = """
<section class="card">
<h2 data-section="6"><span class="num">6</span> 🟨 Съдии — картони на мач</h2>
<p class="note">Недостъпно за тази лига — няма Sofascore данни със съдийска секция.</p>
</section>"""

    early_payout_section = ""
    if data.get("early_payout") or data.get("comeback"):
        rows_ep = [r for r in (data.get("early_payout") or []) if r["no_win"] > 0]
        rows_cb = [r for r in (data.get("comeback") or []) if r["not_lost"] > 0]
        left = (f"""<div>
<h3>Повел с 2 гола, но НЕ спечели</h3>
<table><tr><th>#</th><th>Отбор</th><th>Не спечели</th><th>От колко пъти с +2</th><th>%</th><th>Мачове</th></tr>
{early_payout_html(rows_ep)}
</table>
</div>""" if rows_ep else """<div>
<h3>Повел с 2 гола, но НЕ спечели</h3>
<p class="note">Няма засечени случаи в наличния timeline на головете.</p>
</div>""")
        right = (f"""<div>
<h3>Изостанал с 2 гола, но НЕ загуби</h3>
<table><tr><th>#</th><th>Отбор</th><th>Не загуби</th><th>От колко пъти с -2</th><th>%</th><th>Мачове</th></tr>
{comeback_html(rows_cb)}
</table>
</div>""" if rows_cb else """<div>
<h3>Изостанал с 2 гола, но НЕ загуби</h3>
<p class="note">Няма засечени случаи в наличния timeline на головете.</p>
</div>""")
        early_payout_section = f"""
<section class="card">
<h2 data-section="7"><span class="num">7</span> 💸 Повел/изостанал с 2 гола, но резултатът се обърна (early payout)</h2>
<div class="grid">
{left}
{right}
</div>
<p class="note">Букмейкърско "ранно изплащане": залог "X печели" се изплаща автоматично
при достигане на 2-голово предимство, независимо от крайния резултат. Покритието зависи
от възстановения timeline на головете (не всеки мач е включен).</p>
</section>"""
    else:
        early_payout_section = """
<section class="card">
<h2 data-section="7"><span class="num">7</span> 💸 Повел/изостанал с 2 гола, но резултатът се обърна (early payout)</h2>
<p class="note">Недостъпно за тази лига — няма възстановен timeline на головете.</p>
</section>"""

    late_goals_section = ""
    if data.get("late_goals") is not None:
        rows_lg = data["late_goals"]
        if rows_lg:
            late_goals_section = f"""
<section class="card">
<h2 data-section="8"><span class="num">8</span> ⏳ Голове от 85-та минута нататък (само за справка)</h2>
<table class="lateTable"><tr><th>Отбор</th><th>Брой</th><th>Дом.-Гост</th><th>Мачове</th></tr>
{late_goals_html(rows_lg, league)}
</table>
<p class="note">Само справочна информация — НЕ участва в нито един от прогнозните
модели (Dixon-Coles, Модел В v1, Модел Б, Модел Г).</p>
</section>"""
        else:
            late_goals_section = """
<section class="card">
<h2 data-section="8"><span class="num">8</span> ⏳ Голове от 85-та минута нататък (само за справка)</h2>
<p class="note">Няма засечени голове в този диапазон в наличните данни.</p>
</section>"""
    else:
        late_goals_section = """
<section class="card">
<h2 data-section="8"><span class="num">8</span> ⏳ Голове от 85-та минута нататък (само за справка)</h2>
<p class="note">Недостъпно за тази лига — няма goal_events данни (виж parse_sofa_api.py).</p>
</section>"""

    early_goals_section = ""
    if data.get("early_goals") is not None:
        rows_eg = data["early_goals"]
        if rows_eg:
            eg_headers = "".join(f"<th>До {b}′</th>" for b in EARLY_BUCKETS)
            early_goals_section = f"""
<section class="card">
<h2 data-section="9"><span class="num">9</span> ⚡ Отбори, вкарали рано (само за справка)</h2>
<table class="earlyTable"><tr><th>#</th><th>Отбор</th><th>Мачове</th>
{eg_headers}<th>До {EARLY_BUCKETS[-1]}′ / мач</th></tr>
{early_goals_html(rows_eg, league)}
</table>
<p class="note">Всяка колона се натрупва — гол в 8-та минута брои и в "До 10′", и в
"До 30′". Класирано по голове до {EARLY_BUCKETS[-1]}′ на мач (не по общ брой), за да са
сравними отбори с различен брой изиграни мачове. Само справочна информация — НЕ участва
в нито един от прогнозните модели (Dixon-Coles, Модел В v1, Модел Б, Модел Г).</p>
</section>"""
        else:
            early_goals_section = """
<section class="card">
<h2 data-section="9"><span class="num">9</span> ⚡ Отбори, вкарали рано (само за справка)</h2>
<p class="note">Няма засечени голове в тези диапазони в наличните данни.</p>
</section>"""
    else:
        early_goals_section = """
<section class="card">
<h2 data-section="9"><span class="num">9</span> ⚡ Отбори, вкарали рано (само за справка)</h2>
<p class="note">Недостъпно за тази лига — няма goal_events данни (виж parse_sofa_api.py).</p>
</section>"""

    gk_saves = data.get("goalkeeper_saves")
    if gk_saves:
        goalkeeper_section = f"""
<section class="card">
<h2 data-section="10"><span class="num">10</span> 🧤 Вратари — спасявания на мач</h2>
<table><tr><th>#</th><th>Вратар</th><th>Отбор</th><th>Мачове</th><th>Спасявания</th><th>На мач</th></tr>
{goalkeeper_saves_html(gk_saves)}
</table>
<p class="note">Sofascore сезонен лидерборд ("top-players"), не наша база — покрива само
играчи с достатъчно изиграно време по преценката на Sofascore. Много спасявания на мач
често значи зает/притиснат отбор, не непременно добър вратар. Чисто справочна информация
— НЕ участва в нито един от прогнозните модели.</p>
</section>"""
    else:
        goalkeeper_section = """
<section class="card">
<h2 data-section="10"><span class="num">10</span> 🧤 Вратари — спасявания на мач</h2>
<p class="note">Недостъпно за тази лига — няма Sofascore tournament/season id (виж LEAGUE_SOFA_IDS) или заявката се провали.</p>
</section>"""

    ts_rows = data.get("team_shots")
    if ts_rows:
        team_shots_section = f"""
<section class="card">
<h2 data-section="10"><span class="num">10</span> 🎯 Удари и точни удари — потенциал за вратарски спасявания</h2>
<table class="shotsTable"><thead><tr><th>#</th><th>Отбор</th><th>Мачове</th><th>Удари за</th><th>Удари срещу</th><th>Точни за</th><th class="sorted-desc">Точни срещу</th><th>Спасявания</th></tr></thead>
<tbody>
{team_shots_html(ts_rows, league)}
</tbody></table>
<p class="note">Всички числа са <b>на мач</b> от нашата база. По подразбиране подредено по <b>"Точни срещу"</b>: вратарят на този
отбор получава точно тези удари, затова най-горе са отборите с най-голям потенциал за много спасявания.
<b>Кликни върху заглавие на колона</b>, за да подредиш по нея (втори клик обръща посоката).
"Спасявания" е реалната средна на техния вратар. Чисто справочна таблица — НЕ участва в прогнозните модели.</p>
</section>"""
    else:
        team_shots_section = """
<section class="card">
<h2 data-section="10"><span class="num">10</span> 🎯 Удари и точни удари — потенциал за вратарски спасявания</h2>
<p class="note">Няма данни за удари в базата на тази лига.</p>
</section>"""

    return f"""
<div class="league-block" data-league="{league}"{style}>

<section class="card predSection" id="predSectionTop">
<h2 data-section="pred">🔮 Прогнози за предстоящия кръг</h2>
{model_comparison_html(data.get('model_comparison', []), data.get('all_teams', []))}
<p class="note">За 1X2 — три модела (Dixon-Coles, Модел В v1 — xg/xa от състава,
Модел Г — точков модел по 6 фактора) плюс Opta-Elo (бета).
За Над2.5/ГГ — само v1 (DC и Модел Г не добавят нищо съществено там). Съставът е
"прогнозен" (предвиждане на източника) или "потвърден" (~1ч преди началото). Модел Б
(под всяка карта, разгъваем панел) остава чисто диагностичен, не участва в
средното. Модел Г за живи мачове ползва неутрални Състав/Психология (виж
неговия панел) — за разлика от backtest средата, тук няма ръчен вход за тях.</p>
</section>

<div class="picker">
  <label>Отбор 1</label>
  <select class="team1"><option value="">— избери —</option>{team_options}</select>
  <label>Отбор 2</label>
  <select class="team2"><option value="">— избери —</option>{team_options}</select>
  <button class="clearPick" type="button">Изчисти</button>
  <span class="simResult"></span>
</div>

<div class="savesSection card" style="display:none;">
  <h2>🧤 Очаквани вратарски спасявания</h2>
  <table>
  <thead><tr><th>Вратар</th><th>Последни 5 (спасявания)</th><th>Очаквани удари в рамката</th><th>Спасявания (очаквани)</th>
  <th>3+</th><th>4+</th><th>5+</th><th>6+</th></tr></thead>
  <tbody class="savesBody"></tbody>
  </table>
  <p class="note savesNote"></p>
</div>

<div class="h2hSection card" style="display:none;">
  <h2 class="h2hTitle">H2H</h2>
  <p class="h2hSummary note"></p>
  <table><tr><th>Дата</th><th>Домакин</th><th>Резултат</th><th>Гост</th><th>Турнир</th></tr>
  <tbody class="h2hBody"></tbody>
  </table>
</div>

<div class="standGridSection card" style="display:none;">
  <h2 class="standGridTitle">Мачове в първенството</h2>
  <p class="note">Как всеки друг отбор в лигата се е представил срещу двата избрани отбора тази
  сезон, дом/гост разделено — по-широк контекст от директната H2H среща (често рядка/липсваща).</p>
  <div class="standGridWrap">
  <table class="standGridTable">
  <thead>
    <tr><th rowspan="2">Отбор</th><th colspan="2" class="standGridT1"></th><th colspan="2" class="standGridT2"></th><th rowspan="2" class="standGridPtsH">Точки</th></tr>
    <tr><th>Домакин</th><th>Гост</th><th>Домакин</th><th>Гост</th></tr>
  </thead>
  <tbody class="standGridBody"></tbody>
  </table>
  </div>
</div>

<section class="card">
<h2 data-section="1"><span class="num">1</span> 📊 Форма — класиране по последни мачове</h2>
<div class="grid">
<div>
<h3>Последни 5 мача</h3>
<table><tr><th>#</th><th>Отбор</th><th>Форма</th><th>W-D-L</th><th>Точки</th></tr>
{form_html(data['form5'], "form5", "w5", "d5", "l5", "pts5", league)}
</table>
</div>
<div>
<h3>Последни 10 мача</h3>
<table><tr><th>#</th><th>Отбор</th><th>Форма</th><th>W-D-L</th><th>Точки</th></tr>
{form_html(data['form10'], "form10", "w10", "d10", "l10", "pts10", league)}
</table>
</div>
</div>
</section>

<section class="card">
<h2 data-section="2"><span class="num">2</span> 🆚 Домакин срещу гост</h2>
<div class="grid venue-grid">
<div>
<h3>Най-добър домакин</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['home_rows'], league, 'home')}
</table>
</div>
<div>
<h3>Най-добър гост</h3>
<table><tr><th>#</th><th>Отбор</th><th>М</th><th>W-D-L</th><th>Г:Д</th><th>Точки</th></tr>
{home_away_html(data['away_rows'], league, 'away')}
</table>
</div>
{venue_half_section}
</div>
</section>

<section class="card">
<h2 data-section="3"><span class="num">3</span> ⏱️ {half_label} по полувреме</h2>
<div class="grid">
<div>
<h3>Най-добри в 1-во полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>Мачове</th>
<th>Вкарани {half_word}</th><th>Вк./мач</th>
<th>Допуснати {half_word}</th><th>Доп./мач</th>{half_wdl_th}</tr>
{half_html(data['fh_sorted'], data['fh'], league, data['is_xg_half'], data['fh_wdl'])}
</table>
</div>
<div>
<h3>Най-добри в 2-ро полувреме</h3>
<table><tr><th>#</th><th>Отбор</th><th>Мачове</th>
<th>Вкарани {half_word}</th><th>Вк./мач</th>
<th>Допуснати {half_word}</th><th>Доп./мач</th>{half_wdl_th}</tr>
{half_html(data['sh_sorted'], data['sh'], league, data['is_xg_half'], data['sh_wdl'])}
</table>
</div>
</div>
</section>

<section class="card">
<h2 data-section="4"><span class="num">4</span> ⚽ Над 2.5 / BTTS</h2>
<div class="grid">
<div>
<h3>Най-много мачове с Над 2.5</h3>
<table><tr><th>#</th><th>Отбор</th><th>Над2.5</th><th>Мачове</th><th>%</th></tr>
{over_btts_html(data['over_sorted'], "over", "over_pct", league)}
</table>
</div>
<div>
<h3>Най-много мачове с BTTS — да</h3>
<table><tr><th>#</th><th>Отбор</th><th>BTTS</th><th>Мачове</th><th>%</th></tr>
{over_btts_html(data['btts_sorted'], "btts", "btts_pct", league)}
</table>
</div>
</div>
</section>
{blown_section}
{referees_section}
{early_payout_section}
{late_goals_section}
{early_goals_section}
{team_shots_section}

</div>"""


def render(league_data: dict[str, dict], league_names: list[str]) -> str:
    league_options = "\n".join(
        f'<option value="{esc(l)}">{esc(LEAGUE_COUNTRY.get(l, l))}</option>' for l in league_names)
    # ПОПРАВКА (2026-07): json.dumps не escape-ва "/" — ако някой скрейпнат
    # низ (име на отбор/турнир/дата) някога съдържа "</script", браузърният
    # HTML токенайзер би затворил <script> тага предсрочно и счупил ЦЯЛАТА
    # интерактивна JS логика на страницата (league picker, H2H, quick-sim),
    # не само реда с проблемния низ. _safe_json() го предпазва.
    def _safe_json(obj) -> str:
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    # "Последно обновено" (2026-08-09) - ISO UTC timestamp, JS-ът смята
    # разликата спрямо часовника на ЗРИТЕЛЯ и я показва "преди X минути",
    # тиктакащо (setInterval) - виж скрипта долу.
    generated_at_json = _safe_json(datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))
    track_record = track_record_html()
    top_picks = top_picks_page_html(league_data)
    hero = hero_html(len(league_names))
    try:
        theme_css = (Path(__file__).resolve().parent / "site_theme.css").read_text(encoding="utf-8")
    except Exception:
        theme_css = ""

    h2h_json = _safe_json({l: d["h2h"] for l, d in league_data.items()})
    h2h_summary_json = _safe_json({l: d["h2h_summary"] for l, d in league_data.items()
                                   if d.get("h2h_summary")})
    h2h_matches_json = _safe_json({l: d["h2h_matches"] for l, d in league_data.items()
                                   if d.get("h2h_matches")})
    rounds_json = _safe_json({l: d["latest_round"] for l, d in league_data.items()})
    sim_json = _safe_json({l: d["sim_model"] for l, d in league_data.items()
                           if d.get("sim_model")})
    # "Мачове в първенството" мрежа (2026-08-14, по искане на потребителя,
    # tipster.bg-style) - ВСИЧКИ изиграни мачове тази сезон, за да може JS-ът
    # да построи дом/гост срещу-двата-избрани-отбора мрежата клиентски при
    # избор на чифт (renderStandingsGrid) - виж докстринга там.
    matches_json = _safe_json({l: d["matches_raw"] for l, d in league_data.items()})
    saves_json = _safe_json({l: saves_forecast_data(d.get("team_shots") or [], d.get("keepers"), d.get("side_sot"), *(d.get("saves_prior") or ({}, False)))
                             for l, d in league_data.items()})
    first_round = league_data[league_names[0]]["latest_round"]

    blocks = "\n".join(
        render_league_block(l, league_data[l], active=(i == 0))
        for i, l in enumerate(league_names))

    # Арбитражен калкулатор (2026-10-17) — суров фрагмент от файл, за да не се бъркат
    # скобите му с f-string-а на шаблона; липсващ файл не бива да чупи отчета.
    try:
        arb_fragment = (Path(__file__).resolve().parent / "arb_calculator.html").read_text(encoding="utf-8")
    except Exception as e:
        arb_fragment = ""
        print(f"арбитражен калкулатор пропуснат: {e}")

    html = f"""<!doctype html>
<html lang="bg">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StatsVantage — отборен справочник</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root {{
    --page:        #0a0a0b;
    --page-2:      #111113;
    --surface:     #17181a;
    --surface-2:   #1e2023;
    --ink:         #f5f5f2;
    --ink-2:       #c3c2b7;
    --ink-muted:   #8b8a85;
    --grid-line:   #2a2c2f;
    --border:      rgba(255,255,255,0.08);
    --good:        #22c55e;
    --critical:    #f0605f;
    --cat-blue:    #4f9dff;
    --cat-blue-2:  #7c6bff;
    --cat-orange:  #ff8a3d;
    --rank-1:      rgba(34,197,94,0.20);
    --rank-2:      rgba(34,197,94,0.12);
    --rank-3:      rgba(34,197,94,0.06);
    --shadow:      0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.55);
  }}
  * {{ box-sizing: border-box; }}
  ::selection {{ background: rgba(79,157,255,0.35); color: #fff; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background:
      radial-gradient(900px 500px at 15% -10%, rgba(79,157,255,0.10), transparent 60%),
      radial-gradient(700px 420px at 100% 0%, rgba(124,107,255,0.08), transparent 55%),
      var(--page);
    background-attachment: fixed;
    color: var(--ink); margin: 0;
    padding: 32px clamp(16px, 4vw, 40px) 70px; line-height: 1.45;
    max-width: 1440px; margin-inline: auto;
    -webkit-font-smoothing: antialiased;
  }}
  .masthead {{ display: flex; align-items: baseline; justify-content: space-between;
               flex-wrap: wrap; gap: 8px 20px; margin-bottom: 22px; }}
  h1 {{
    font-size: 23px; font-weight: 750; letter-spacing: -0.3px; margin: 0;
    background: linear-gradient(100deg, var(--ink) 35%, var(--cat-blue) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  h3 {{ font-size: 11.5px; font-weight: 650; color: var(--ink-2); text-transform: uppercase;
        letter-spacing: .5px; margin: 0 0 10px; }}
  .card {{
    background: linear-gradient(180deg, rgba(255,255,255,0.025), transparent 120px), var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px; padding: 20px 24px 24px; margin-bottom: 20px;
    box-shadow: var(--shadow);
    transition: border-color .18s ease;
  }}
  .card:hover {{ border-color: rgba(255,255,255,0.14); }}
  h2 {{
    font-size: 14.5px; font-weight: 650; letter-spacing: .1px; color: var(--ink);
    margin: 0 0 16px; padding-bottom: 13px; border-bottom: 1px solid var(--grid-line);
    display: flex; align-items: center; gap: 10px; scroll-margin-top: 70px;
  }}
  h2 .num {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 22px; height: 22px; border-radius: 7px;
    background: linear-gradient(135deg, var(--cat-blue), var(--cat-blue-2));
    box-shadow: 0 2px 8px -2px rgba(79,157,255,0.5);
    color: #fff; font-size: 11px; font-weight: 700; flex: none;
  }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
  /* Числата се центрират (2026-07, по искане на потребителя); текстовите
     колони — имена на отбори/съдии, списъци с мачове, етикети на модели —
     остават отляво чрез клас, а не по позиция, защото таблиците са с
     различна структура (в част от тях името е 1-ва колона, в друга 2-ра). */
  th, td {{ padding: 9px 10px; text-align: center; border-bottom: 1px solid var(--grid-line);
            font-variant-numeric: tabular-nums; }}
  td.tname, td.detail, td.mLabel {{ text-align: left; }}
  td.wdl {{ white-space: nowrap; }}
  th:first-child, th:nth-child(2) {{ text-align: left; }}
  .predTable th:nth-child(2) {{ text-align: center; }}
  .blownTable th, .lateTable th {{ text-align: left; }}
  .blownTable th:nth-child(2), .lateTable th:nth-child(2) {{ text-align: center; }}
  .lateTable th:nth-child(3) {{ text-align: center; }}
  th {{ color: var(--ink-muted); font-weight: 600; font-size: 10.5px; text-transform: uppercase;
        letter-spacing: .5px; position: sticky; top: 0; background: var(--surface); z-index: 1; }}
  tbody tr {{ transition: background-color .12s ease; }}
  tbody tr:nth-child(even) {{ background: rgba(255,255,255,0.02); }}
  tr:hover {{ background: rgba(79,157,255,0.08) !important; }}
  .rank-1 {{ background: var(--rank-1) !important; }}
  .rank-2 {{ background: var(--rank-2) !important; }}
  .rank-3 {{ background: var(--rank-3) !important; }}
  .ref-row td:nth-child(6) {{ font-weight: 700; color: var(--ink); }}
  .form {{ font-family: ui-monospace, "SF Mono", Consolas, monospace; letter-spacing: 2.5px;
           font-weight: 700; }}
  .res-w {{ color: var(--good); }}
  .res-d {{ color: var(--ink-muted); }}
  .res-l {{ color: var(--critical); }}
  td.form span[title] {{ cursor: help; }}
  .detail {{ color: var(--ink-2); font-size: 12.5px; line-height: 1.6; }}
  .detail b {{ color: var(--ink); font-weight: 650; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }}
  @media (max-width: 860px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .grid > div + div {{ border-left: 1px solid var(--grid-line); padding-left: 30px; }}
  @media (max-width: 860px) {{ .grid > div + div {{ border-left: none; padding-left: 0; }} }}
  /* venue-grid може да има повече от 2 елемента (Домакин/Гост по полувреме,
     2026-08-02) — div+div сам не различава редовете, само nth-child(2n)
     дава коректно border-left само на дясната колона, не на всеки следващ
     елемент независимо от реда му. */
  .venue-grid > div + div {{ border-left: none; padding-left: 0; }}
  .venue-grid > div:nth-child(2n) {{ border-left: 1px solid var(--grid-line); padding-left: 30px; }}
  @media (max-width: 860px) {{ .venue-grid > div:nth-child(2n) {{ border-left: none; padding-left: 0; }} }}
  .note {{ color: var(--ink-muted); font-size: 12px; margin-top: 10px; line-height: 1.5; }}
  .predSection {{ border: 1px solid rgba(79,157,255,0.45);
                  box-shadow: var(--shadow), 0 0 0 1px rgba(79,157,255,0.08) inset; }}
  .predCard {{
    background: rgba(255,255,255,0.025); border: 1px solid var(--grid-line);
    border-radius: 12px; padding: 14px 18px; margin-bottom: 14px;
    transition: border-color .15s ease, background-color .15s ease;
  }}
  .predCard:hover {{ border-color: rgba(255,255,255,0.16); background: rgba(255,255,255,0.035); }}
  .predCard:last-child {{ margin-bottom: 0; }}
  .predCard summary {{
    margin: 0; font-size: 14.5px; font-weight: 650; color: var(--ink); cursor: pointer;
    list-style: none;
    display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  }}
  .predCard summary::-webkit-details-marker {{ display: none; }}
  .predCard[open] summary {{ margin-bottom: 10px; }}
  .predArrow {{ color: var(--cat-blue); }}
  .predArrow::before {{ content: "▸"; }}
  .predCard[open] .predArrow::before {{ content: "▾"; }}
  .predDate {{ font-size: 11.5px; font-weight: 500; color: var(--ink-muted); }}
  .statusConfirmed {{ color: var(--good); font-weight: 700; }}
  .statusPredicted {{ color: var(--cat-orange); font-weight: 700; }}
  .liveBadge {{ display: none; align-items: center; gap: 5px; font-size: 11px; font-weight: 800;
                color: var(--critical); font-variant-numeric: tabular-nums; }}
  .liveBadge.show {{ display: inline-flex; }}
  .liveBadge::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%;
                         background: var(--critical); animation: liveDot 1.4s ease-in-out infinite; }}
  @keyframes liveDot {{ 0%, 100% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: .35; transform: scale(.7); }} }}
  .countdown {{ display: none; font-size: 11px; font-weight: 700; color: var(--cat-blue);
                font-variant-numeric: tabular-nums; background: rgba(79,157,255,0.12);
                border-radius: 6px; padding: 2px 7px; }}
  .countdown.show {{ display: inline-block; }}
  #lastUpdated {{ font-variant-numeric: tabular-nums; }}
  .predCard.picked {{ border-color: var(--cat-blue); background: rgba(79,157,255,0.07); }}
  .predTable th {{ position: static; }}
  .predTable .mLabel {{ font-weight: 600; color: var(--ink); width: 34%; }}
  .predTable td:not(.mLabel):not(.note) {{ font-weight: 650; font-variant-numeric: tabular-nums; }}
  .predTable .modelOdds {{ font-weight: 500; color: var(--ink-muted); font-size: 11px; }}
  .predTable .avgRow {{ border-top: 2px solid var(--cat-blue); background: rgba(79,157,255,0.08); }}
  .predTable .avgRow td {{ color: var(--cat-blue) !important; font-weight: 800 !important; }}
  .predTable .lowConfRow {{ border-top-color: var(--cat-orange); background: rgba(255,138,61,0.10); }}
  .predTable .lowConfRow td {{ color: var(--cat-orange) !important; }}
  .lowConf {{ font-size: 10.5px; font-weight: 700; color: var(--cat-orange);
              text-transform: none; letter-spacing: 0; cursor: help; }}
  .mbPanel {{
    margin-top: 12px; border: 1px solid var(--grid-line); border-radius: 10px;
    padding: 4px 12px; background: rgba(255,255,255,0.015);
  }}
  .mbPanel summary {{
    cursor: pointer; padding: 8px 2px; font-size: 12.5px; color: var(--ink-2);
    list-style: none;
  }}
  .mbPanel summary::-webkit-details-marker {{ display: none; }}
  .mbPanel summary::before {{ content: "▸ "; color: var(--cat-blue); }}
  .mbPanel[open] summary::before {{ content: "▾ "; }}
  .mbPanel summary b {{ color: var(--ink); }}
  .mbBody {{
    padding: 4px 2px 12px; font-size: 12px; color: var(--ink-2); line-height: 1.7;
    border-top: 1px solid var(--grid-line); margin-top: 2px;
  }}
  .mbBody b {{ color: var(--ink); }}
  .mbFlag {{ color: var(--cat-orange); }}
  .mbBody .note {{ color: var(--ink-muted); }}
  .picker {{
    display: flex; align-items: center; gap: 12px; margin: 14px 0 20px;
    padding: 14px 18px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; flex-wrap: wrap; box-shadow: var(--shadow);
  }}
  .picker label {{ color: var(--ink-muted); font-size: 12px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: .3px; }}
  .picker select {{ background: var(--page-2); color: var(--ink); border: 1px solid var(--border);
                     border-radius: 8px; padding: 7px 11px; font-size: 13px; min-width: 160px;
                     transition: border-color .15s ease; }}
  .picker select:hover {{ border-color: var(--ink-2); }}
  .picker select:focus {{ outline: 2px solid var(--cat-blue); outline-offset: 1px; }}
  .picker button {{ background: transparent; color: var(--ink-muted); border: 1px solid var(--border);
                     border-radius: 8px; padding: 7px 14px; font-size: 12px; font-weight: 600;
                     cursor: pointer; transition: color .15s, border-color .15s, background-color .15s; }}
  .picker button:hover {{ color: var(--ink); border-color: var(--ink-2); background: rgba(255,255,255,0.04); }}
  .simResult {{ font-size: 13px; color: var(--ink); display: flex; align-items: center;
                gap: 8px; flex-wrap: wrap; }}
  .simResult .pill {{
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
    padding: 5px 12px; display: inline-flex; align-items: center; gap: 6px;
    font-variant-numeric: tabular-nums;
  }}
  .simResult .pill b {{ color: var(--ink-muted); font-weight: 600; font-size: 11px;
                         text-transform: uppercase; letter-spacing: .3px; }}
  .simResult .pill span.val {{ font-weight: 700; color: var(--ink); }}
  .simResult .pill span.odds {{ font-weight: 500; color: var(--ink-muted); font-size: 11px; }}
  .simResult .simNote {{ color: var(--ink-muted); font-size: 11px; }}
  .topPicks .tpGrid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }}
  .topPicks h3 {{ margin: 0 0 10px; font-size: 14px; color: var(--ink-2); }}
  .topPicks .tpList {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }}
  .topPicks .tpList li {{ display: grid; grid-template-columns: 22px 1fr auto; gap: 8px; align-items: center;
                          padding: 8px 10px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; }}
  .topPicks .tpN {{ color: var(--ink-muted); font-variant-numeric: tabular-nums; font-size: 12px; }}
  .topPicks .tpMatch {{ display: flex; flex-direction: column; min-width: 0; font-size: 13px; }}
  .topPicks .tpMeta {{ color: var(--ink-muted); font-size: 11px; }}
  .topPicks .tpVal {{ font-variant-numeric: tabular-nums; font-size: 13px; white-space: nowrap; }}
  .topPicks .tpVal .odds {{ color: var(--ink-muted); font-size: 11px; }}
  .topPicks .tpEmpty {{ display: block !important; color: var(--ink-muted); font-size: 12px; }}
  @media (max-width: 900px) {{ .topPicks .tpGrid {{ grid-template-columns: 1fr; }} }}
  .shotsTable th.sortable {{ cursor: pointer; user-select: none; }}
  .shotsTable th.sortable:hover {{ color: var(--ink); }}
  .shotsTable th.sorted-desc::after {{ content: ' ▼'; font-size: 9px; }}
  .shotsTable th.sorted-asc::after {{ content: ' ▲'; font-size: 9px; }}
  .savesSection td, .savesSection th {{ font-variant-numeric: tabular-nums; }}
  .savesSection span.odds {{ color: var(--ink-muted); font-size: 11px; }}
  .savesSection .chips {{ white-space: nowrap; }}
  .savesSection .chip[title] {{ cursor: help; }}
  .savesSection .chip {{ display: inline-block; min-width: 22px; padding: 2px 5px; margin-right: 3px;
                          text-align: center; border-radius: 6px; font-weight: 600; font-size: 12px;
                          background: var(--surface-2); border: 1px solid var(--border); }}
  tr.dim {{ opacity: 0.25; }}
  tr.pick1 {{ background: rgba(79,157,255,0.20) !important; }}
  tr.pick2 {{ background: rgba(255,138,61,0.20) !important; }}
  .h2hSection table td:first-child, .h2hSection table th:first-child {{ white-space: nowrap; }}
  .standGridWrap {{ overflow-x: auto; }}
  .standGridTable {{ min-width: 560px; }}
  .standGridTable td:first-child, .standGridTable th:first-child {{ text-align: left; white-space: nowrap; }}
  .standGridT1, .standGridT2 {{ border-bottom: 2px solid var(--cat-blue); }}
  .standGridCell {{ font-weight: 700; font-variant-numeric: tabular-nums; border-radius: 6px; }}
  .standGridCell.win {{ background: rgba(34,197,94,0.18); color: #4ade80; }}
  .standGridCell.draw {{ background: rgba(255,255,255,0.07); color: var(--ink-2); }}
  .standGridCell.loss {{ background: rgba(240,96,95,0.18); color: #ff8a89; }}
  .standGridCell.empty {{ color: var(--ink-muted); }}
  .standGridRank {{ color: var(--ink-muted); font-weight: 500; font-variant-numeric: tabular-nums; }}
  .league-picker {{
    margin: 4px 0 22px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 12px 16px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: var(--shadow);
  }}
  .league-picker .note {{ margin: 0; }}
  .league-picker select {{
    background: var(--page-2); color: var(--ink); border: 1px solid var(--border);
    border-radius: 9px; padding: 9px 16px; font-size: 14.5px; font-weight: 650;
    transition: border-color .15s ease;
  }}
  .league-picker select:hover {{ border-color: var(--ink-2); }}
  .league-picker select:focus {{ outline: 2px solid var(--cat-blue); outline-offset: 1px; }}
  .quickNav {{
    position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 7px;
    margin: 0 0 22px; padding: 12px 14px;
    background: linear-gradient(100deg, #1e3a6e 0%, #3a2a7a 55%, #5a2a72 100%);
    border: 1px solid rgba(180,160,255,0.35);
    border-radius: 12px; box-shadow: 0 6px 22px -6px rgba(79,157,255,0.45), var(--shadow);
  }}
  .quickNav button {{
    background: rgba(255,255,255,0.10); color: #f0f0ff; border: 1px solid rgba(255,255,255,0.22);
    border-radius: 999px; padding: 6px 14px; font-size: 12px; font-weight: 700;
    cursor: pointer; white-space: nowrap; transition: all .15s ease;
  }}
  .quickNav button:hover {{
    background: #fff; color: #24246e; border-color: #fff; transform: translateY(-1px);
    box-shadow: 0 3px 10px -2px rgba(0,0,0,0.4);
  }}
  /* Пил-сегменти за W-D-L / 1X2 — вместо суров текст "10-0-3" */
  .wdlPill {{ display: inline-flex; gap: 3px; align-items: center; font-weight: 700;
              font-variant-numeric: tabular-nums; font-size: 12.5px; }}
  .wdlPill b {{ display: inline-block; min-width: 18px; padding: 2px 5px; border-radius: 5px;
                text-align: center; }}
  .wdlPill .w {{ background: rgba(34,197,94,0.18); color: #4ade80; }}
  .wdlPill .d {{ background: rgba(255,255,255,0.07); color: var(--ink-2); }}
  .wdlPill .l {{ background: rgba(240,96,95,0.18); color: #ff8a89; }}
{theme_css}
</style>
</head>
<body>
<div class="masthead" id="top">
  <a class="logo" href="#top" aria-label="StatsVantage">
    <svg viewBox="0 0 32 32" aria-hidden="true"><rect x="1.5" y="1.5" width="29" height="29" rx="8" fill="none" stroke="var(--primary)" stroke-width="2"/><path d="M6 22 L12 15 L17 19 L26 9" fill="none" stroke="var(--primary)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="26" cy="9" r="2.6" fill="var(--primary)"/></svg>
    StatsVantage
  </a>
  <p class="note">Данни от FotMob (до края на септември — Sofascore) · Последно обновено: <b id="lastUpdated">току-що</b></p>
</div>

{hero}

{top_picks}

{track_record}

<div class="league-picker">
  <label for="leagueSel" class="note">Първенство: </label>
  <select id="leagueSel">{league_options}</select>
  <span class="note" id="roundInfo">до кръг {first_round}</span>
</div>

<nav class="quickNav" id="quickNav">
  <button type="button" data-jump="pred">🔮 Прогнози</button>
  <button type="button" data-jump="1">📊 1. Форма</button>
  <button type="button" data-jump="2">🆚 2. Дом/Гост</button>
  <button type="button" data-jump="3">⏱️ 3. Полувреме</button>
  <button type="button" data-jump="4">⚽ 4. Над2.5/BTTS</button>
  <button type="button" data-jump="5">📉 5. Изпуснат лидер</button>
  <button type="button" data-jump="6">🟨 6. Съдии</button>
  <button type="button" data-jump="7">💸 7. Early payout</button>
  <button type="button" data-jump="8">⏳ 8. 85'+</button>
  <button type="button" data-jump="9">⚡ 9. Ранни голове</button>
  <button type="button" data-jump="10">🎯 10. Удари</button>
  <button type="button" id="topNavBtn">🏆 Топ прогнози</button>
  <button type="button" id="arbNavBtn">🧮 Арбитраж</button>
</nav>

{blocks}

{arb_fragment}

<script>
(function() {{
  const h2hData = {h2h_json};
  const h2hSummaryData = {h2h_summary_json};
  const h2hMatchesData = {h2h_matches_json};
  const matchesData = {matches_json};
  const roundsData = {rounds_json};
  const simData = {sim_json};
  const savesData = {saves_json};
  const generatedAt = new Date({generated_at_json});

  // "Последно обновено преди X минути" (2026-08-09) - тиктака на клиента,
  // не само веднъж при зареждане, за да усеща страницата като жива дори
  // ако зрителят я държи отворена дълго.
  function updateLastUpdated() {{
    const el = document.getElementById('lastUpdated');
    if (!el) return;
    const diffSec = Math.max(0, Math.floor((Date.now() - generatedAt.getTime()) / 1000));
    let text;
    if (diffSec < 60) text = 'току-що';
    else if (diffSec < 3600) text = `преди ${{Math.floor(diffSec / 60)}} мин.`;
    else if (diffSec < 86400) text = `преди ${{Math.floor(diffSec / 3600)}} ч.`;
    else text = `преди ${{Math.floor(diffSec / 86400)}} дни`;
    el.textContent = text;
  }}
  updateLastUpdated();
  setInterval(updateLastUpdated, 30000);

  // Countdown до старта на мача (2026-08-09) - ЧИСТО клиентско изчисление от
  // data-kickoff (Unix epoch секунди, UTC) - за разлика от спрения liveBadge,
  // тук няма външна заявка изобщо, значи не може да бъде блокирано. Крие се
  // автоматично след старта (не показва "минус" време).
  function updateCountdowns() {{
    const now = Date.now();
    document.querySelectorAll('.countdown[data-kickoff]').forEach(el => {{
      const diffSec = Math.floor(parseInt(el.dataset.kickoff, 10) * 1000 - now) / 1000;
      if (diffSec <= 0) {{ el.classList.remove('show'); el.textContent = ''; return; }}
      let text;
      if (diffSec < 3600) text = `старт след ${{Math.ceil(diffSec / 60)}} мин.`;
      else if (diffSec < 86400) {{
        const h = Math.floor(diffSec / 3600), m = Math.round((diffSec % 3600) / 60);
        text = `старт след ${{h}}ч ${{m}}мин.`;
      }} else text = `старт след ${{Math.ceil(diffSec / 86400)}} дни`;
      el.classList.add('show');
      el.textContent = text;
    }});
  }}
  updateCountdowns();
  setInterval(updateCountdowns, 30000);

  // "На живо" индикатор — СПРЯН (2026-08-09): директната browser fetch към
  // Sofascore's /sport/football/events/live се оказа ненадеждна - Cloudflare
  // връща 403 в повечето случаи на реалния сайт (потвърдено многократно, не
  // еднократен fluke), не CORS-отворена, както изглеждаше при първоначалния
  // тест. Спряно до решение със сървърен прокси (curl_cffi вместо директна
  // browser заявка) - виж бележката до live_badge = "" в model_comparison_html().

  // ПОПРАВКА (2026-07): renderH2H()/renderSim() вмъкваха скрейпнати имена
  // (отбор/турнир/дата) в innerHTML БЕЗ ескейпване — за разлика от всяка
  // сървърно-рендната таблица (esc() в Python), тук нищо не пазеше от '<'/
  // '&' в такова име да се изпълни като markup вместо да се покаже като
  // текст.
  function escHtml(s) {{
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }}

  // Последни <=10 срещи, ВСИЧКИ турнири/сезони (fetch_h2h.py — Sofascore
  // /event/{{id}}/h2h, само W-D-L бройка, без резултати). Fallback за лиги
  // без h2hMatchesData (Bundesliga/Ligue1 — все още без fetch_sofa.py
  // team_id-та за team-history пътя).
  function h2hSummaryFor(league, t1, t2) {{
    const table = h2hSummaryData[league];
    if (!table) return null;
    const key = [t1, t2].sort().join('|||');
    const row = table[key];
    if (!row) return null;
    const w1 = row.team_a === t1 ? row.wins_a : row.wins_b;
    const w2 = row.team_a === t1 ? row.wins_b : row.wins_a;
    return {{ wins1: w1, wins2: w2, draws: row.draws, total: row.total }};
  }}

  // Реален мач-по-мач H2H (fetch_h2h_matches.py — Sofascore
  // /team/{{id}}/events/last/{{page}}, ВСИЧКИ турнири/сезони: лига, купа,
  // суперкупа...) — дати, резултати, турнир. Първичен източник, когато е
  // наличен (виж h2hSummaryFor за fallback).
  function h2hMatchesFor(league, t1, t2) {{
    const table = h2hMatchesData[league];
    if (!table) return null;
    const key = [t1, t2].sort().join('|||');
    return table[key] || null;
  }}

  // Голев H2H множител — огледално на h2h.py _build_directed_h2h(), но
  // base е own_xg от sim модела (нямаме отделно "среден брой реални голове
  // на сезона" вграден тук). blend = (сума голове в H2H + k*base) / (n+k);
  // mult = blend/base, капнато — същия дух като CLI-то, вече с истински
  // голове, не само W-D-L.
  function h2hGoalMultiplier(matches, teamName, baseXg, k, cap) {{
    k = k === undefined ? 6 : k;
    cap = cap === undefined ? [0.85, 1.15] : cap;
    let sum = 0, n = 0;
    for (const m of matches) {{
      if (m.home === teamName) {{ sum += m.hg; n++; }}
      else if (m.away === teamName) {{ sum += m.ag; n++; }}
    }}
    if (n === 0 || !baseXg || baseXg <= 0) return 1.0;
    const blend = (sum + k * baseXg) / (n + k);
    const mult = blend / baseXg;
    return Math.max(cap[0], Math.min(cap[1], mult));
  }}

  // ── Бърза симулация (xG-Dixon-Coles, огледално на engine/simulate.py
  // score_matrix()/match_markets()). H2H тук е ПО-СЛАБ сигнал от CLI
  // simulate_sofa.py: Sofascore дава само W-D-L бройка (не голове), затова
  // множителят е скромен (капнат ±12%) и се смалява при малка извадка —
  // виж h2hMultiplier() по-долу. ──
  function poissonPmf(lam, k) {{
    let logFact = 0;
    for (let i = 2; i <= k; i++) logFact += Math.log(i);
    return Math.exp(k * Math.log(lam) - lam - logFact);
  }}

  function scoreMatrix(lam, mu, rho, maxGoals) {{
    rho = rho === undefined ? -0.12 : rho;   // калибрирано, виж engine/simulate.py RHO
    maxGoals = maxGoals === undefined ? 10 : maxGoals;
    const ph = [], pa = [];
    for (let k = 0; k <= maxGoals; k++) {{ ph.push(poissonPmf(lam, k)); pa.push(poissonPmf(mu, k)); }}
    const m = [];
    for (let i = 0; i <= maxGoals; i++) {{
      const row = [];
      for (let j = 0; j <= maxGoals; j++) row.push(ph[i] * pa[j]);
      m.push(row);
    }}
    m[0][0] *= 1 - lam * mu * rho;
    m[0][1] *= 1 + lam * rho;
    m[1][0] *= 1 + mu * rho;
    m[1][1] *= 1 - rho;
    let total = 0;
    for (const row of m) for (const p of row) total += p;
    for (let i = 0; i <= maxGoals; i++) for (let j = 0; j <= maxGoals; j++) m[i][j] /= total;
    return m;
  }}

  // Честен (break-even) коефициент = 1/вероятност — огледално на dixon_coles.fair()
  // (Python), вече използвано в CLI изходите на simulate_lineup_v1/v2.py,
  // model_g_scoring.py и simulate_sofa.py ("71% (1.41)" конвенцията).
  function fairOdds(p) {{
    return p > 0.001 ? (1 / p).toFixed(2) : '—';
  }}

  function matchMarkets(lam, mu) {{
    const m = scoreMatrix(lam, mu);
    let pHome = 0, pDraw = 0, pAway = 0, pOver25 = 0, pBtts = 0;
    for (let i = 0; i < m.length; i++) {{
      for (let j = 0; j < m.length; j++) {{
        const p = m[i][j];
        if (i > j) pHome += p; else if (i < j) pAway += p; else pDraw += p;
        if (i + j > 2.5) pOver25 += p;
        if (i > 0 && j > 0) pBtts += p;
      }}
    }}
    return {{ pHome, pDraw, pAway, pOver25, pBtts }};
  }}

  function h2hMultiplier(wins1, wins2, draws, total) {{
    const K = 6;                              // приор — колкото по-малка извадка, толкова по-неутрално
    const STRENGTH = 0.12;                    // таван на корекцията (по-тесен от goal-based h2h.py — само W-D-L)
    const shrink = total / (total + K);
    const winShare1 = (wins1 + 0.5 * draws) / total;   // 0..1, 0.5 = неутрално
    const skew = (winShare1 - 0.5) * 2;                // -1..1
    const mult1 = 1 + skew * shrink * STRENGTH;
    const mult2 = 1 - skew * shrink * STRENGTH;
    return {{ mult1: Math.max(0.85, Math.min(1.15, mult1)),
             mult2: Math.max(0.85, Math.min(1.15, mult2)) }};
  }}

  function simulateMatch(league, home, away) {{
    const model = simData[league];
    if (!model) return null;
    const th = model.teams[home], ta = model.teams[away];
    if (!th || !ta) return null;
    // ПОПРАВКА (2026-07): този JS порт беше останал със СТАРАТА асиметрична
    // формула (home_adv само върху lam, без компенсация в mu) — същият бъг,
    // който вече е оправен в simulate_sofa.py, но не беше пренесен тук.
    // Затова бързата симулация даваше системно завишени Над2.5/ГГ и изместен
    // към домакина 1X2 спрямо DC реда в картите с прогнози.
    // home_mult + away_mult = 2 → сборът lam+mu е закотвен към реалната
    // лигова средна, а съотношението за средна среща остава home_adv.
    const homeMult = 2 * model.home_adv / (1 + model.home_adv);
    const awayMult = 2 / (1 + model.home_adv);
    let lam = th.own_xg * (ta.xg_conceded / model.avg_xg) * homeMult;
    let mu = ta.own_xg * (th.xg_conceded / model.avg_xg) * awayMult;
    lam *= th.att_form / ta.def_form;
    mu *= ta.att_form / th.def_form;
    lam *= th.rating_form;
    mu *= ta.rating_form;

    let h2h = null;
    const h2hM = h2hMatchesFor(league, home, away);
    if (h2hM) {{
      const mult1 = h2hGoalMultiplier(h2hM, home, th.own_xg);
      const mult2 = h2hGoalMultiplier(h2hM, away, ta.own_xg);
      lam *= mult1;
      mu *= mult2;
      h2h = {{ total: h2hM.length, rich: true }};
    }} else {{
      const h2hSum = h2hSummaryFor(league, home, away);
      if (h2hSum) {{
        const {{ mult1, mult2 }} = h2hMultiplier(h2hSum.wins1, h2hSum.wins2, h2hSum.draws, h2hSum.total);
        lam *= mult1;
        mu *= mult2;
        h2h = Object.assign({{ rich: false }}, h2hSum);
      }}
    }}

    return Object.assign({{ lam, mu, h2h }}, matchMarkets(lam, mu));
  }}

  function wireLeague(block) {{
    const league = block.dataset.league;
    const rows = Array.from(block.querySelectorAll('tr.team-row'));
    // event-row (2026-08-16): "Изпуснат лидер"/"85'+" и др. — искаме да
    // маркираме реда, АКО избраният отбор се появи там, но БЕЗ да
    // затъмняваме останалите (за разлика от team-row) - тези таблици
    // изброяват само някои отбори, "затъмни несъвпадащите" почти винаги
    // би изгасил цялата таблица (виж бележката до event-row в Python-а).
    const eventRows = Array.from(block.querySelectorAll('tr.event-row'));
    const sel1 = block.querySelector('.team1');
    const sel2 = block.querySelector('.team2');
    const h2hSection = block.querySelector('.h2hSection');
    const h2hTitle = block.querySelector('.h2hTitle');
    const h2hSummaryEl = block.querySelector('.h2hSummary');
    const h2hBody = block.querySelector('.h2hBody');

    function renderH2H(t1, t2) {{
      if (!t1 || !t2 || t1 === t2) {{ h2hSection.style.display = 'none'; return; }}
      const key = [t1, t2].sort().join('|||');
      const rich = (h2hMatchesData[league] || {{}})[key];
      const matches = rich || (h2hData[league] || {{}})[key] || [];
      const scope = rich ? 'всички турнири/сезони' : 'текущия сезон';
      h2hTitle.textContent = `H2H — ${{t1}} срещу ${{t2}} (${{matches.length}} срещи, ${{scope}})`;

      if (rich) {{
        h2hSummaryEl.textContent = '';
      }} else {{
        const sum = h2hSummaryFor(league, t1, t2);
        h2hSummaryEl.textContent = sum
          ? `Последни ${{sum.total}} срещи (всички турнири/сезони, Sofascore — само W-D-L, ` +
            `няма запазен резултат): ${{t1}} ${{sum.wins1}} победи — ${{sum.draws}} равенства — ` +
            `${{t2}} ${{sum.wins2}} победи`
          : '';
      }}
      h2hBody.innerHTML = matches.map(m => {{
        const winner = m.hg > m.ag ? 'home' : (m.ag > m.hg ? 'away' : 'draw');
        const homeCls = winner === 'home' ? 'tname res-w' : 'tname';
        const awayCls = winner === 'away' ? 'tname res-w' : 'tname';
        return `<tr><td class='tname'>${{escHtml(m.date)}}</td><td class='${{homeCls}}'>${{escHtml(m.home)}}</td>` +
               `<td class='form'>${{m.hg}}:${{m.ag}}</td><td class='${{awayCls}}'>${{escHtml(m.away)}}</td>` +
               `<td class='detail'>${{escHtml(m.tournament || '')}}</td></tr>`;
      }}).join('') || '<tr><td colspan="5" class="detail">Няма изиграни срещи.</td></tr>';
      h2hSection.style.display = '';
    }}

    const simEl = block.querySelector('.simResult');

    function renderSim(t1, t2) {{
      if (!t1 || !t2 || t1 === t2) {{ simEl.innerHTML = ''; return; }}
      const r = simulateMatch(league, t1, t2);
      if (!r) {{
        simEl.innerHTML = '<span class="simNote">Няма данни за бърза симулация.</span>';
        return;
      }}
      const pct = x => Math.round(x * 100) + '%';
      // Коеф. до всеки % (2026-08-24, по искане на потребителя) — честен
      // (break-even) коефициент 1/вероятност, същата "71% (1.41)" конвенция
      // като CLI изходите на v1/v2/Модел Г/simulate_sofa.py (fairOdds() по-горе).
      const pv = x => `${{pct(x)}} <span class="odds">(${{fairOdds(x)}})</span>`;
      const h2hNote = !r.h2h ? '· без H2H (нямат история в базата)'
        : r.h2h.rich ? `· H2H ${{r.h2h.total}} срещи (голове, всички турнири)`
        : `· H2H ${{r.h2h.total}} срещи (W-D-L)`;
      simEl.innerHTML =
        `<span class="pill"><b>1X2</b><span class="val">${{pv(r.pHome)}} / ${{pv(r.pDraw)}} / ${{pv(r.pAway)}}</span></span>` +
        `<span class="pill"><b>Над2.5</b><span class="val">${{pv(r.pOver25)}}</span></span>` +
        `<span class="pill"><b>Г/Г</b><span class="val">${{pv(r.pBtts)}}</span></span>` +
        `<span class="simNote">${{escHtml(t1)}} домакин · xG-модел ${{escHtml(h2hNote)}}</span>`;
    }}

    // Очаквани вратарски спасявания (2026-10-16, по искане на потребителя).
    // Удари в рамката към вратаря = ср. точни удари на ролята на нападателя (домакин/гост)
    // × (сила на нападението на съперника ÷ лигата)^0.85 × (колко удари допуска отборът
    // на вратаря ÷ лигата)^0.85; силите са свити (K=20 мача) към силата на отбора от миналия сезон,
    // а при липса на такава — към средното на лигата. После
    // × процент спасени удари на вратаря, свит към лигата (K=40 удара).
    // Мултипликативна, не средна: при силен срещу слаб отбор ефектите се умножават
    // (бектест 3 362 изхода: при най-големите несъответствия реалните 6.1 удара се
    // предсказват като 6.1, при „средна" формула — като 5.6). Броят спасявания е
    // свръхразпръснат спрямо Поасон (var/mean ≈ 1.21), затова опашките са от
    // отрицателно биномно разпределение. Последните 5 мача на вратаря са само
    // справка — смесени в числото влошаваха бектеста при всяко тегло.
    const savesSection = block.querySelector('.savesSection');
    const savesBody = block.querySelector('.savesBody');
    const savesNote = block.querySelector('.savesNote');
    const SAVES_DISP = 1.21;

    function nbTail(mean, k) {{      // P(X >= k)
      const r = mean / (SAVES_DISP - 1), p = r / (r + mean);
      let pmf = Math.pow(p, r), cdf = 0;
      for (let i = 0; i < k; i++) {{
        cdf += pmf;
        pmf *= (i + r) / (i + 1) * (1 - p);
      }}
      return Math.max(0, 1 - cdf);
    }}

    function renderSaves(t1, t2) {{
      const d = savesData[league];
      if (!t1 || !t2 || t1 === t2 || !d || !d.t || !d.t[t1] || !d.t[t2]) {{
        savesSection.style.display = 'none'; return;
      }}
      const K = 20, KS = 40, G = 0.85, W = 0.7;
      // Цел на свиването: НЕ средното на лигата, а силата на отбора от миналия сезон
      // (pf/pa — точни удари за/срещу спрямо лигата; тежест W). Новак (nw) няма минала
      // лига: мерените новаци дават 0.84× удари за и 1.12× срещу средното.
      const tgtA = x => x.pf !== undefined ? d.mu * ((1 - W) + W * x.pf) : (x.nw ? d.mu * 0.84 : d.mu);
      const tgtD = x => x.pa !== undefined ? d.mu * ((1 - W) + W * x.pa) : (x.nw ? d.mu * 1.12 : d.mu);
      const att = x => (x.sf * x.n + K * tgtA(x)) / (x.n + K);
      const dfn = x => (x.sa * x.n + K * tgtD(x)) / (x.n + K);
      const ps = x => (x.sv * x.n + KS * d.p0) / (x.sa * x.n + KS);
      // Отбор 1 = домакин: неговият вратар посреща удари на ГОСТА (d.ma), и обратно.
      const rowFor = (keeperTeam, oppTeam, base) => {{
        const k = d.t[keeperTeam], o = d.t[oppTeam];
        const sot = base * Math.pow(att(o) / d.mu, G) * Math.pow(dfn(k) / d.mu, G);
        const exp = sot * ps(k);
        const cell = line => {{
          const p = nbTail(exp, line);
          return `${{Math.round(p * 100)}}% <span class="odds">(${{fairOdds(p)}})</span>`;
        }};
        const kp = (d.k || {{}})[keeperTeam];
        const who = kp ? `${{escHtml(kp.gk)}} <span class="odds">${{escHtml(keeperTeam)}}</span>` : escHtml(keeperTeam);
        const last5 = kp ? kp.last5.map((v, i) => `<span class="chip" title="${{escHtml(((kp.info5 || [])[i] || '')).replace(/"/g, '&quot;')}}">${{v}}</span>`).join('') : '—';
        return `<tr><td class="tname">${{who}}</td><td class="chips">${{last5}}</td><td>${{sot.toFixed(1)}}</td>` +
               `<td><b>${{exp.toFixed(2)}}</b></td><td>${{cell(3)}}</td><td>${{cell(4)}}</td><td>${{cell(5)}}</td><td>${{cell(6)}}</td></tr>`;
      }};
      savesBody.innerHTML = rowFor(t1, t2, d.ma || d.mu) + rowFor(t2, t1, d.mh || d.mu);
      const few = Math.min(d.t[t1].n, d.t[t2].n) < 6;
      const hasPrior = Object.values(d.t).some(x => x.pf !== undefined);
      savesNote.textContent = 'Справка, не залог: в бектест върху 3 362 вратарски изхода моделът греши средно ' +
        'с 1.48 спасявания срещу 1.51 за просто лигово средно — предимството е малко, ' +
        'но вероятностите са добре калибрирани. Последните 5 са само информация (смесени в числото влошаваха точността). ' +
        'В скоби е коефициентът без марж. Вратарят е този от последния мач на отбора — провери състава.' +
        (hasPrior ? ' Силата на отборите ползва и миналия сезон (в ранните кръгове грешката падна от 1.56 на 1.51); новаците са с мерения им профил — повече удари срещу, по-малко за.'
                  : ' За тази лига няма данни от миналия сезон — силата на отборите идва само от този сезон и е свита към средното на лигата.') +
        (few ? ' Внимание: някой от отборите има под 6 мача — оценката му разчита основно на приора.' : '');
      savesSection.style.display = '';
    }}

    // "Мачове в първенството" мрежа (2026-08-14, по искане на потребителя,
    // tipster.bg-style, виж standGridSection в HTML-а) - за всеки друг отбор
    // в лигата, как е играл дом/гост срещу t1 и t2 тази сезон. Резултатът е
    // ВИНАГИ "домакин-гост" (m.hg-m.ag буквално), а клетката се оцветява
    // спрямо отбора от КОЛОНАТА (t1/t2), не спрямо реда - потвърдено срещу
    // реалната tipster.bg страница преди имплементацията (виж бележката за
    // "Розенборг-Гост=3-0" примера). Редовете на самите t1/t2 показват
    // директната им среща под колоната на другия отбор, огледално на
    // tipster.bg.
    const standGridSection = block.querySelector('.standGridSection');
    const standGridBody = block.querySelector('.standGridBody');
    const standGridT1Head = block.querySelector('.standGridT1');
    const standGridT2Head = block.querySelector('.standGridT2');

    function renderStandingsGrid(t1, t2) {{
      if (!t1 || !t2 || t1 === t2) {{ standGridSection.style.display = 'none'; return; }}
      const matches = matchesData[league] || [];
      if (!matches.length) {{ standGridSection.style.display = 'none'; return; }}

      function findFixture(refTeam, opponent, venue) {{
        return matches.find(m => venue === 'home'
          ? (m.home === refTeam && m.away === opponent)
          : (m.away === refTeam && m.home === opponent));
      }}
      function cellHtml(refTeam, opponent, venue) {{
        const m = findFixture(refTeam, opponent, venue);
        if (!m) return '<td class="standGridCell empty">-</td>';
        const refScore = venue === 'home' ? m.hg : m.ag;
        const oppScore = venue === 'home' ? m.ag : m.hg;
        const cls = refScore > oppScore ? 'win' : (refScore < oppScore ? 'loss' : 'draw');
        return `<td class="standGridCell ${{cls}}">${{m.hg}}-${{m.ag}}</td>`;
      }}

      const points = {{}};
      matches.forEach(m => {{
        if (!(m.home in points)) points[m.home] = 0;
        if (!(m.away in points)) points[m.away] = 0;
        if (m.hg > m.ag) points[m.home] += 3;
        else if (m.ag > m.hg) points[m.away] += 3;
        else {{ points[m.home] += 1; points[m.away] += 1; }}
      }});
      const teams = Object.keys(points).sort((a, b) => points[b] - points[a]);

      standGridT1Head.textContent = t1;
      standGridT2Head.textContent = t2;
      // Класиране пред името (2026-08-24, по искане на потребителя) — `teams`
      // вече е сортиран по точки (виж по-горе), значи индексът+1 Е мястото в
      // тази изчислена (не официална Sofascore) таблица.
      standGridBody.innerHTML = teams.map((team, i) => {{
        const c1h = team === t1 ? '<td class="standGridCell empty">-</td>' : cellHtml(t1, team, 'home');
        const c1a = team === t1 ? '<td class="standGridCell empty">-</td>' : cellHtml(t1, team, 'away');
        const c2h = team === t2 ? '<td class="standGridCell empty">-</td>' : cellHtml(t2, team, 'home');
        const c2a = team === t2 ? '<td class="standGridCell empty">-</td>' : cellHtml(t2, team, 'away');
        // избраните отбори се маркират в класирането (2026-10-27): синьо = Отбор 1, оранжево = Отбор 2,
        // като в останалите таблици; точките са най-вдясно
        const pickCls = team === t1 ? 'pick1' : (team === t2 ? 'pick2' : '');
        return `<tr class="${{pickCls}}"><td class="tname"><span class="standGridRank">${{i + 1}}.</span> ${{escHtml(team)}}</td>${{c1h}}${{c1a}}${{c2h}}${{c2a}}<td class="standGridPts">${{points[team]}}</td></tr>`;
      }}).join('');
      standGridSection.style.display = '';
    }}

    function apply() {{
      const t1 = sel1.value, t2 = sel2.value;
      const active = Boolean(t1 || t2);
      rows.forEach(r => {{
        r.classList.remove('dim', 'pick1', 'pick2');
        if (!active) return;
        // venue-специфични таблици (Домакин/Гост, вкл. по полувреме): само
        // избраният ДОМАКИН highlight-ва ред в "Домакин" таблицата, само
        // избраният ГОСТ — в "Гост" таблицата. Без data-venue (форма/
        // Над2.5/BTTS и др.) — старото поведение, проверяваме и двата.
        const venue = r.dataset.venue;
        if (venue === 'home') {{
          if (r.dataset.team === t1) r.classList.add('pick1');
          else r.classList.add('dim');
        }} else if (venue === 'away') {{
          if (r.dataset.team === t2) r.classList.add('pick2');
          else r.classList.add('dim');
        }} else if (r.dataset.team === t1) r.classList.add('pick1');
        else if (r.dataset.team === t2) r.classList.add('pick2');
        else r.classList.add('dim');
      }});
      eventRows.forEach(r => {{
        r.classList.remove('pick1', 'pick2');
        if (!active) return;
        if (r.dataset.team === t1) r.classList.add('pick1');
        else if (r.dataset.team === t2) r.classList.add('pick2');
      }});
      renderH2H(t1, t2);
      renderSim(t1, t2);
      renderSaves(t1, t2);
      renderStandingsGrid(t1, t2);
    }}

    // Отваряне на карта с прогноза = зарежда двойката в picker-а долу
    // (2026-08-09, по искане на потребителя: "махни тика, само като кликна
    // на мача автоматично да зарежда отборите" - заменя старите ръчни
    // чекбоксове). Държи се като радио бутон (само една отворена карта
    // "избрана" наведнъж) - затваряне на избраната карта или ръчна промяна
    // на select-ите/"Изчисти" маха избора.
    const pairCards = Array.from(block.querySelectorAll('.predCard[data-home]'));

    function clearPairPicks() {{
      pairCards.forEach(card => card.classList.remove('picked'));
    }}

    pairCards.forEach(card => {{
      card.addEventListener('toggle', () => {{
        if (card.open) {{
          clearPairPicks();
          card.classList.add('picked');
          sel1.value = card.dataset.home;
          sel2.value = card.dataset.away;
        }} else if (card.classList.contains('picked')) {{
          card.classList.remove('picked');
          sel1.value = ''; sel2.value = '';
        }}
        apply();
      }});
    }});

    sel1.addEventListener('change', () => {{ clearPairPicks(); apply(); }});
    sel2.addEventListener('change', () => {{ clearPairPicks(); apply(); }});
    block.querySelector('.clearPick').addEventListener('click', () => {{
      sel1.value = ''; sel2.value = ''; clearPairPicks(); apply();
    }});
  }}

  document.querySelectorAll('.league-block').forEach(wireLeague);

  const leagueSel = document.getElementById('leagueSel');
  const roundInfo = document.getElementById('roundInfo');

  function selectLeague(name) {{
    document.querySelectorAll('.league-block').forEach(b => {{
      b.style.display = (b.dataset.league === name) ? '' : 'none';
    }});
    roundInfo.textContent = `до кръг ${{roundsData[name]}}`;
  }}

  // Помни последно избраната лига (localStorage) — за да не се "рестартира"
  // изгледът при автоматичното презареждане по-долу.
  const savedLeague = localStorage.getItem('statsvantage_league');
  if (savedLeague && [...leagueSel.options].some(o => o.value === savedLeague)) {{
    leagueSel.value = savedLeague;
    selectLeague(savedLeague);
  }}

  leagueSel.addEventListener('change', () => {{
    localStorage.setItem('statsvantage_league', leagueSel.value);
    selectLeague(leagueSel.value);
  }});

  // Бърза навигация — скролва до секцията в ТЕКУЩО избраната лига (не първата
  // намерена в DOM-а, защото всички лиги съществуват едновременно, само скрити).
  // Таблица „Удари и точни удари“ (секция 10): клик върху заглавие на колона
  // подрежда по нея (числата — първо низходящо; „Отбор“ — азбучно), втори клик обръща.
  document.querySelectorAll('table.shotsTable').forEach(tbl => {{
    const ths = Array.from(tbl.querySelectorAll('thead th'));
    const tbody = tbl.querySelector('tbody');
    ths.forEach((th, idx) => {{
      if (idx === 0) return;
      th.classList.add('sortable');
      th.addEventListener('click', () => {{
        const isText = idx === 1;
        const wasDesc = th.classList.contains('sorted-desc');
        const wasAsc = th.classList.contains('sorted-asc');
        const asc = wasDesc ? true : (wasAsc ? false : isText);
        ths.forEach(x => x.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
        const rows = Array.from(tbody.querySelectorAll('tr.team-row'));
        const val = r => isText ? r.children[idx].textContent.trim()
                                : parseFloat(r.children[idx].textContent);
        rows.sort((a, b) => {{
          const va = val(a), vb = val(b);
          const c = isText ? va.localeCompare(vb, 'bg') : (va - vb);
          return asc ? c : -c;
        }});
        rows.forEach((r, i) => {{
          tbody.appendChild(r);
          r.children[0].textContent = i + 1;
          r.classList.remove('rank-1', 'rank-2', 'rank-3');
          if (!asc && !isText && i < 3) r.classList.add('rank-' + (i + 1));
        }});
      }});
    }});
  }});

  const topNav = document.getElementById('topNavBtn');
  if (topNav) topNav.addEventListener('click', () => {{
    const t = document.getElementById('topPicksSection');
    if (t) t.scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }});

  document.querySelectorAll('#quickNav button').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const activeBlock = document.querySelector(`.league-block[data-league="${{leagueSel.value}}"]`);
      const target = activeBlock && activeBlock.querySelector(`[data-section="${{btn.dataset.jump}}"]`);
      if (target) target.scrollIntoView({{behavior: 'smooth', block: 'start'}});
    }});
  }});

}})();
</script>

</body>
</html>"""
    return html


def gather_sofa(db_path: Path, league_name: str) -> dict | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    matches = sofa_matches(con)
    if not matches:
        return None
    latest_round = con.execute("SELECT MAX(round) FROM matches").fetchone()[0]
    form5, form10 = form_table(matches)
    home_rows, away_rows = home_away_table(matches)
    over_sorted, btts_sorted = over_btts_table(matches)
    h2h = sofa_h2h_dataset(con)
    h2h_summary = sofa_h2h_summary(con)
    h2h_matches = sofa_h2h_matches(con)
    referees = [r for r in referee_table(con) if r["matches"] >= 2]
    _ep_data = gather_early_payout(league_name, db=str(db_path))
    early_payout = _ep_data["team_table"]
    comeback = _ep_data["comeback_table"]
    sim_model = sim_model_dataset(con)

    model_comparison = []
    goalkeeper_saves = []
    try:
        team_shots = team_shots_table(con)
    except Exception as e:
        team_shots = []
        print(f"team_shots пропуснат за {league_name}: {e}")
    try:
        keepers, side_sot = team_keepers(con), league_side_sot(con)
    except Exception as e:
        keepers, side_sot = {}, None
        print(f"вратари/домакин-гост пропуснати за {league_name}: {e}")
    try:
        saves_prior = prior_season_shots(league_name, [r["team"] for r in team_shots])
    except Exception as e:
        saves_prior = ({}, False)
        print(f"миналогодишен приор пропуснат за {league_name}: {e}")
    sofa_ids = LEAGUE_SOFA_IDS.get(league_name)
    if sofa_ids:
        try:
            model_comparison = build_model_comparison(league_name, db_path, *sofa_ids)
        except Exception as e:
            print(f"model_comparison пропуснат за {league_name}: {e}")
        # Секция "Вратари — спасявания на мач" е МАХНАТА от сайта (2026-10-16, по
        # решение на потребителя — Sofascore е блокиран и данните бяха от 19.09).
        # goalkeeper_saves_table()/goalkeeper_saves_html() остават в кода; връщане =
        # разкоментиране на този блок и на {goalkeeper_section} в league-block шаблона.
        # try:
        #     goalkeeper_saves = goalkeeper_saves_table(*sofa_ids)
        # except Exception as e:
        #     print(f"goalkeeper_saves пропуснат за {league_name}: {e}")

    # Реален HT резултат (incidents.json през parse_sofa_api.py) при налична
    # база — иначе xG-по-полувреме (стари vault-парснати бази без home_ht).
    if has_real_half(con):
        fh, sh, fh_sorted, sh_sorted = sofa_half_goals(con)
        is_xg_half = False
        blown = sofa_blown_lead(con)
        fh_wdl, sh_wdl = sofa_half_1x2(con)
        home_fh_rows, home_sh_rows, away_fh_rows, away_sh_rows = home_away_half_table(con)
    else:
        fh, sh, fh_sorted, sh_sorted = sofa_half_xg(con)
        is_xg_half = True
        blown = None
        fh_wdl, sh_wdl = None, None
        home_fh_rows = home_sh_rows = away_fh_rows = away_sh_rows = None

    # Чисто информационно класиране (85'+ и ранни голове) — виж
    # sofa_late_goals()/sofa_early_goals() докстринговете: НЕ участват в
    # никой от симулационните модели, само за report-а.
    late_goals = sofa_late_goals(con) if has_goal_events(con) else None
    early_goals = sofa_early_goals(con) if has_goal_events(con) else None

    return {
        "form5": form5, "form10": form10, "home_rows": home_rows, "away_rows": away_rows,
        "home_fh_rows": home_fh_rows, "home_sh_rows": home_sh_rows,
        "away_fh_rows": away_fh_rows, "away_sh_rows": away_sh_rows,
        "fh": fh, "sh": sh, "fh_sorted": fh_sorted, "sh_sorted": sh_sorted,
        "fh_wdl": fh_wdl, "sh_wdl": sh_wdl,
        "over_sorted": over_sorted, "btts_sorted": btts_sorted, "blown": blown,
        "late_goals": late_goals, "early_goals": early_goals,
        "h2h": h2h, "h2h_summary": h2h_summary, "h2h_matches": h2h_matches,
        "is_xg_half": is_xg_half, "latest_round": latest_round,
        "all_teams": sorted({r["team"] for r in form5}), "referees": referees,
        "model_comparison": model_comparison, "goalkeeper_saves": goalkeeper_saves,
        "team_shots": team_shots, "keepers": keepers, "side_sot": side_sot, "saves_prior": saves_prior,
        "early_payout": early_payout, "comeback": comeback, "sim_model": sim_model,
        "matches_raw": [{"date": d, "home": h, "away": a, "hg": hg, "ag": ag}
                        for d, h, a, hg, ag in matches if hg is not None],
    }


# ── Кеш на пресметнатите данни по лига (2026-10-26, по искане на потребителя: "да не се
# генерира на ново, а само промените да стават бързо"). Измерено: сглобяването на HTML
# (render) е ~0 сек., цялото време е в gather_sofa (модели, състави). Затова gather_sofa
# се пази като файл за всяка лига и се пресмята наново само когато:
#   • се е променила базата на лигата / файловете с предстоящите кръгове / Elo, или
#   • е сменена GATHER_VERSION (вдигни я, когато променяш КОД, който влияе на данните —
#     таблици, модели, gather_sofa; промени само във вида (render/HTML/CSS/JS) НЕ я
#     изискват), или
#   • кешът е по-стар от CACHE_MAX_AGE_MIN (съставите/контузените идват отвън, без да
#     променят базата), освен при --cached.
# Флагове:  --cached  = ползвай кеша, колкото и да е стар (бърза промяна по вида)
#           --rebuild = игнорирай кеша, пресметни всичко наново
GATHER_VERSION = "2026-10-26-4"   # -2: Sofascore изключен от lineup_impact (състави само от FotMob/кеш)
CACHE_MAX_AGE_MIN = 45
CACHE_DIR = DATA_DIR / "report_cache"


def _file_sig(path: Path) -> str:
    try:
        st = path.stat()
        return f"{path.name}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return f"{path.name}:-"


def _league_fingerprint(name: str, db_path: Path) -> str:
    import hashlib
    parts = [GATHER_VERSION, _file_sig(db_path)]
    ids = LEAGUE_SOFA_IDS.get(name)
    rounds_dir = DATA_DIR / "raw_sofa" / "rounds"
    if ids and rounds_dir.exists():
        prefix = f"{ids[0]}_{ids[1]}_r"
        parts += sorted(_file_sig(f) for f in rounds_dir.glob(prefix + "*.json"))
    for extra in ("opta_elo_current.csv", "opta_elo_history.csv"):
        parts.append(_file_sig(DATA_DIR / extra))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def gather_cached(name: str, db_path: Path, mode: str) -> dict | None:
    """mode: 'auto' (по подразбиране) | 'cached' | 'rebuild'."""
    import pickle
    import time
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{name}.pkl"
    fp = _league_fingerprint(name, db_path)
    if mode != "rebuild" and cache_file.exists():
        try:
            blob = pickle.loads(cache_file.read_bytes())
            age_min = (time.time() - blob["saved_at"]) / 60
            same = blob["fingerprint"] == fp
            fresh = age_min <= CACHE_MAX_AGE_MIN
            if mode == "cached" and blob["version"] == GATHER_VERSION:
                print(f"  {name}: кеш (възраст {age_min:.0f} мин.{'' if same else ', базата е сменена след това'})")
                return blob["data"]
            if same and fresh:
                print(f"  {name}: кеш (непроменена база, {age_min:.0f} мин.)")
                return blob["data"]
        except Exception as e:                       # развален кеш = просто пресмятаме наново
            print(f"  {name}: кешът не е четим ({e}) — пресмятам")
    data = gather_sofa(db_path, name)
    if data is not None:
        cache_file.write_bytes(pickle.dumps({"fingerprint": fp, "version": GATHER_VERSION,
                                              "saved_at": time.time(), "data": data}))
    return data


def main():
    mode = "cached" if "--cached" in sys.argv else ("rebuild" if "--rebuild" in sys.argv else "auto")
    league_data = {}
    for name, db_path in LEAGUES:
        data = gather_cached(name, db_path, mode)
        if data:
            league_data[name] = data
        else:
            print(f"Пропускам {name} — няма данни в {db_path}")

    names = list(league_data.keys())
    if not names:
        # ПОПРАВКА (2026-07): render() достъпва league_names[0]
        # безусловно — ако всички конфигурирани db пътища липсват/грешни,
        # това преди гърмеше с неясен IndexError вместо ясно съобщение.
        sys.exit("Няма данни за нито една лига — провери LEAGUES/db пътищата в make_team_report.py.")
    html = render(league_data, names)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Записано: {OUT}  (лиги: {', '.join(names)})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
