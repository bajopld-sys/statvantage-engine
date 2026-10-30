"""Модел-сравнение за РЕАЛНИТЕ предстоящи фикстури (не свободен избор на
отбори както "quick simulation" джаджата) — за всеки следващ мач: Dixon-
Coles (xG), Модел В v1 (xg/xa състав), Модел В v2 (Sofascore атрибути).

Решение на потребителя (2026-07): за 1X2 — и трите модела; за Над2.5/ГГ —
само v1+v2. ОТМЕНЕНО 2026-09-08: това беше измерено с некалибрирани
λ/μ; след рекалибрацията DC е равен/по-добър от v1 и на двата голови
пазара (992 общи мача) — виж бележката при row["dc"] по-долу.

Взима следващия НЕИЗИГРАН кръг направо от суровия Sofascore кеш
(data/raw_sofa/rounds/) — локалната `matches` таблица пази само завършени
мачове, затова предстоящите фикстури не са там (виж бележката в
fetch_sofa.py за подробности).
"""
import json
import sqlite3
from pathlib import Path

import pandas as pd

from dixon_coles import fair, joint_markets, joint_markets_nbinom
from lineup_impact import fetch_lineup_starters_detailed, fetch_missing_players_detailed, missing_regular_starters
from profiles_sofa import build_profiles
from simulate_sofa import simulate_xg
from simulate_lineup_v1 import simulate as simulate_v1
from simulate_lineup_v2 import simulate as simulate_v2
from model_b_heuristic import (analyze as analyze_model_b, load_matches as load_matches_b,
                               load_goal_events as load_goal_events_b, load_h2h_all as load_h2h_all_b)
from model_g_scoring import analyze as analyze_model_g
from build_opta_elo import predict_1x2 as predict_opta_elo
from parse_sofa_api import canon

from config import DATA_DIR
RAW_ROUNDS = DATA_DIR / "raw_sofa" / "rounds"


def _next_round_fixtures(tournament_id: int, season_id: int, max_round: int = 40) -> list[dict]:
    """Първият кръг с поне 1 неизигран мач в НАЙ-БЛИЗКОТО бъдеще — сканира
    кешираните кръгове по ред, връща фикстурите му (само "Not started")."""
    import datetime
    now = datetime.datetime.now(datetime.UTC).timestamp()
    best_round, best_events = None, []

    for rd in range(1, max_round + 1):
        path = RAW_ROUNDS / f"{tournament_id}_{season_id}_r{rd}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events", [])
        # ПОПРАВКА (2026-07): "!= finished" пропускаше и включваше "postponed"
        # мачове (реален случай — Viking кръг 11/12 по-рано в проекта) със
        # стар/неясен startTimestamp. Изрично "notstarted" вместо blacklist.
        upcoming = [e for e in events if e.get("status", {}).get("type") == "notstarted"
                   and e.get("startTimestamp", 0) >= now]
        if not upcoming:
            continue
        soonest = min(e["startTimestamp"] for e in upcoming)
        if best_round is None or soonest < best_round[0]:
            best_round = (soonest, rd)
            best_events = upcoming
    return best_events


# max_matches (2026-10-09): таванът беше 8 и тихо криеше мачове — кръговете са 9-14 мача
# (EPL 10, LaLiga 11, Championship до 14), потребителят видя непълен кръг. 16 покрива
# най-големия кръг; цената е само повече модел-изчисления/FotMob заявки (всичко се кешира).
def build_comparison(league_name: str, db_path: Path, tournament_id: int, season_id: int,
                     max_matches: int = 16) -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        prof = build_profiles(con)
    except (SystemExit, ValueError, KeyError):
        prof = None

    all_teams = [r[0] for r in con.execute("SELECT DISTINCT home FROM matches").fetchall()]
    events = _next_round_fixtures(tournament_id, season_id)[:max_matches]

    # Модел Б (потребителският heuristic) — заредено ВЕДНЪЖ за целия кръг
    # (не се мени между отделните фикстури), после analyze() се вика за
    # всеки мач поотделно. verbose=False, за да не спами конзолата при
    # регенерация на отчета за целия кръг.
    #
    # ПОПРАВКА (2026-08-06): преди трите load_* бяха в ЕДИН try/except - ако
    # h2h_matches таблицата липсва (напр. Allsvenskan - H2H fetch никога не е
    # пускан там, виж model_g_scoring._load_h2h_safe за същия проблем),
    # matches_b ставаше None ТВЪРДЕ, макар самото зареждане на мачовете да е
    # успешно - това тихо изключваше Модел Б И Модел Г за ЦЕЛИ лиги, не само
    # H2H частта им (h2h_points() вече дефолтва на неутрално при 0 срещи).
    try:
        matches_b = load_matches_b(db_path)
        goal_events_b = load_goal_events_b(db_path)
    except Exception:
        matches_b = goal_events_b = None
    try:
        h2h_all_b = load_h2h_all_b(db_path)
    except Exception:
        h2h_all_b = []

    # Модел Г ползва СЪЩИТЕ matches_b/h2h_all_b (идентичен load_matches/load_h2h_all
    # формат, model_g_scoring го внася директно от model_b_heuristic) - за живи
    # фикстури before_date е последната позната дата в базата (огледално на
    # model_g_scoring.main()'s --home/--away клон), не датата на самата фикстура,
    # защото matches_b вече съдържа само завършени мачове (всички са "минали").
    latest_date_g = max((m["date"] for m in matches_b), default="9999-12-31") if matches_b else "9999-12-31"

    # ПОПРАВКА (2026-09-04, по случая Ipswich Town – Liverpool FC): Sofascore
    # връща РАЗЛИЧНО пълно име на един и същ отбор в различните си endpoint-и —
    # кръговият fixture endpoint тук дава "Liverpool FC", докато
    # parse_sofa_api.py (при разбор на ИЗИГРАНИ мачове) е записал локално само
    # "Liverpool" (или каквото Sofascore е върнало ТОГАВА). Разминаването чупи
    # ВСЯКО точно текстово съвпадение надолу по веригата (prof["teams"].index,
    # opta_elo CSV-то, matches_b) — DC/v1/Opta-Elo мълчаливо се предаваха с
    # "няма данни"/"отбор още няма изигран мач" (грешно съобщение — Liverpool
    # имаше купища мачове, просто под друго изписване). Модел Б/Г оцеляваха
    # само защото degrade-ват на неутрално при непознат отбор, вместо да
    # откажат напълно. Фиксът: ако пълното име липсва в локалната база, пробвай
    # shortName (Sofascore-ова кратка форма, обикновено съвпада с локалното
    # изписване) и го ползвай навсякъде — и за lookup, и за display.
    def _resolve_team_name(team_obj: dict, known: list[str]) -> str:
        # canon() (fetch_sofa.ALIASES) е СЪЩАТА нормализация, която
        # parse_sofa_api.py прилага при запис на изиграните мачове — ползва се
        # първа, за да има ЕДИН източник на истината за псевдонимите (там вече
        # стоеше "Liverpool FC" -> "Liverpool", просто фикстурната страна
        # никога не го викаше). shortName е вторичен fallback за отбори, които
        # още не са в ALIASES.
        for candidate in (canon(team_obj["name"]), team_obj["name"], team_obj.get("shortName")):
            if candidate and candidate in known:
                return candidate
        return canon(team_obj["name"])  # нищо не съвпадна — моделите ще
                     # отчетат "няма данни" (честно, не подвеждащо) за него.

    results = []
    for ev in events:
        home = _resolve_team_name(ev["homeTeam"], all_teams)
        away = _resolve_team_name(ev["awayTeam"], all_teams)
        eid = ev["id"]
        as_of = pd.Timestamp.now()

        # ПОПРАВКА (2026-07): startTimestamp е Unix epoch (UTC) — без tz-конверсия
        # pd.Timestamp(..., unit="s").strftime() отпечатва суровите UTC часове
        # като локални, показвайки грешен (по-ранен) начален час спрямо
        # българско време (Европа/София, UTC+2/+3 според DST).
        kickoff_local = pd.Timestamp(ev["startTimestamp"], unit="s", tz="UTC").tz_convert("Europe/Sofia")
        row = {"home": home, "away": away, "event_id": eid,
              "date": kickoff_local.strftime("%Y-%m-%d %H:%M"),
              "kickoff_ts": int(ev["startTimestamp"])}

        if prof is not None and home in prof["teams"].index and away in prof["teams"].index:
            try:
                # ПОПРАВКА (2026-07): преди DC се смяташе БЕЗ lineup-adjustment
                # (con/as_of/event_id не се подаваха) — докато v1/v2 отдолу
                # ГО имат, правейки 3-моделното сравнение непоследователно
                # (DC игнорираше липсващи титуляри, v1/v2 не).
                dc = simulate_xg(prof, home, away, con=con, as_of=as_of, event_id=eid,
                                 league=league_name)
                # Над2.5/Г-Г ВЪРНАТИ (2026-09-08): решението от юли да се
                # показват само v1/v2 там ("DC не добавяше нищо съществено")
                # е взето с НЕКАЛИБРИРАНИ λ/μ. След рекалибрацията сравнението
                # глава-срещу-глава върху 992 ОБЩИ мача обръща картината:
                #   Над2.5 — DC 56.3% срещу v1 55.8%
                #   Г/Г    — DC 56.7% срещу v1 54.3%
                # т.е. DC е равен или по-добър и на двата пазара.
                row["dc"] = {"p_home": dc["p_home"], "p_draw": dc["p_draw"], "p_away": dc["p_away"],
                             "p_over25": dc["p_over25"], "p_btts": dc["p_btts"],
                             # съвместни "знак И Над2.5/Г-Г" (2026-10-26) — за топ 5 комбинации
                             "joint": joint_markets(dc["lam"], dc["mu"], dc["rho"])}
            except Exception:
                row["dc"] = None
        else:
            row["dc"] = None

        # Opta-style Elo (2026-08-24, бета) — вижте build_opta_elo.py докстринга.
        # ЧИСТО отделен модел от gather_sofa()'s `prof` профили — чете директно
        # готовите data/opta_elo_current.csv/opta_elo_calibration.csv (записани
        # от build_opta_elo.py, изпълнен предварително в update_all.py), не
        # преизчислява Elo историята при всеки report render. None ако някой
        # отбор още няма изигран мач тази сезон (predict_1x2() докстринга).
        # ПОПРАВКА (2026-09-04): всяка причина за отказ (несъвпадащо име, липсващ
        # CSV, изключение) се показваше в отчета като "отбор още няма изигран мач
        # тази сезон" — подвеждащо, точно това забави диагностиката на
        # "Liverpool FC" случая. Сега причината се записва явно.
        try:
            row["opta_elo"] = predict_opta_elo(league_name, home, away)
            if row["opta_elo"] is None:
                row["opta_elo_reason"] = "отбор още няма Elo рейтинг (без изигран мач тази сезон)"
        except Exception as exc:
            row["opta_elo"] = None
            row["opta_elo_reason"] = f"Opta-Elo грешка: {type(exc).__name__}"

        # ПОПРАВКА (2026-08-28): едно извикване на fetch_lineup_starters_detailed()
        # вместо fetch_lineup_starters() — дава {име: (sofa_id, позиция)} директно
        # от състава на МАЧА, не само голи имена. home_st/away_st (голи имена,
        # за v1/Модел Б/Г — не им трябва ID/позиция) се извеждат от него, а
        # home_detail/away_detail (с ID/позиция) отиват само към v2 — виж бележката
        # в simulate_lineup_v2.team_indices() защо вече не минава през нашата база.
        home_detail, away_detail, confirmed = fetch_lineup_starters_detailed(eid)
        home_st, away_st = set(home_detail), set(away_detail)
        row["confirmed"] = confirmed
        # ПОПРАВКА (2026-07): само `confirmed` не различава "има прогнозен
        # състав" от "състав изобщо още няма" — и двете даваха False, а
        # картата пишеше "прогнозен състав" дори когато Sofascore не връща
        # нито един играч (типично за мач след няколко дни). Оттам и
        # подвеждащото "няма данни" при v1/v2 без обяснение защо.
        row["has_lineup"] = bool(home_st and away_st)
        if home_st and away_st:
            # ПОПРАВКА (2026-07): преди нямаше try/except тук (за разлика от
            # DC клона по-горе) — грешка в 1 мач би прекъснала model_comparison
            # за ЦЕЛИЯ кръг (gather_sofa() хваща само на ниво "цялата функция").
            try:
                r1 = simulate_v1(con, home, away, as_of, home_st, away_st, all_teams,
                                 league=league_name)
                row["v1"] = None if "error" in r1 else {
                    "p_home": r1["p_home"], "p_draw": r1["p_draw"], "p_away": r1["p_away"],
                    "p_over25": r1["p_over25"], "p_btts": r1["p_btts"],
                    # Брой играчи, от които е изчислен attack_index — по искане на
                    # потребителя (2026-08-01): при прогнозен (непотвърден) състав
                    # малка извадка (напр. 2 играча) прави числото ненадеждно,
                    # затова се показва в скоби в HTML отчета.
                    "home_n_attack": r1["home_idx"]["n_attack"], "away_n_attack": r1["away_idx"]["n_attack"],
                    # съвместни "знак И Над2.5/Г-Г" от v1 (2026-10-26) — за топ 5 комбинации
                    "joint": joint_markets_nbinom(r1["lam"], r1["mu"])}
            except Exception:
                row["v1"] = None
            try:
                r2 = simulate_v2(con, home, away, as_of, home_detail, away_detail, league=league_name)
                row["v2"] = None if "error" in r2 else {
                    "p_home": r2["p_home"], "p_draw": r2["p_draw"], "p_away": r2["p_away"],
                    "p_over25": r2["p_over25"], "p_btts": r2["p_btts"],
                    "home_n_found": r2["home_idx"]["n_found"], "away_n_found": r2["away_idx"]["n_found"]}
            except Exception:
                row["v2"] = None
        else:
            row["v1"] = row["v2"] = None

        # Контузени/наказани титуляри (2026-08-09, по искане на потребителя:
        # "искам missingPlayers да се взима предвид, така ще знаем ако добър
        # играч е контузен", после "да тежи по рейтинга на самите отсъстващи")
        # — теглено ВИНАГИ, независимо дали вече има прогнозен състав (за
        # разлика от simulate_sofa._apply_lineup_factors, където missingPlayers
        # е само fallback когато НЯМА lineup). missing_regular_starters()
        # филтрира до ОБИЧАЙНИТЕ титуляри и прикача рейтинг на всеки, с ЕДНО
        # зареждане на отбора (виж lineup_impact.py докстринга там за
        # консолидацията от 2 отделни функции/заявки на играч).
        try:
            miss_home_all, miss_away_all = fetch_missing_players_detailed(eid)
            row["missing_home"] = missing_regular_starters(con, home, as_of, miss_home_all)
            row["missing_away"] = missing_regular_starters(con, away, as_of, miss_away_all)
        except Exception:
            row["missing_home"] = row["missing_away"] = []

        if matches_b is not None:
            try:
                row["model_b"] = analyze_model_b(matches_b, goal_events_b, h2h_all_b, home, away, verbose=False)
            except Exception:
                row["model_b"] = None
            try:
                # Състав/контузии (2026-08-09, обновено да тежи по рейтинг) -
                # подава ЦЕЛИЯ списък отсъстващи ОБИЧАЙНИ титуляри (вече
                # филтрирани по-горе, с прикачен player_rating), Модел Г ги
                # превръща в точки автоматично, тежени по качеството на
                # отсъстващия (виж model_g_scoring.squad_points()).
                row["model_g"] = analyze_model_g(matches_b, h2h_all_b, home, away, before_date=latest_date_g,
                                                 missing_home=row["missing_home"],
                                                 missing_away=row["missing_away"])
            except Exception:
                row["model_g"] = None
        else:
            row["model_b"] = None
            row["model_g"] = None

        results.append(row)
    con.close()
    return results
