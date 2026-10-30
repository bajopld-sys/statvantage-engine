"""Модел В, вариант 1 — симулация от МАЧОВА статистика (xg+xa, рейтинг) на
КОНКРЕТНИЯ старов състав (11 играча, прогнозен/потвърден), не от сезонния
тим-average (Dixon-Coles) или league-wide ранговете (model_b_heuristic.py).
Вариант 2 (Sofascore attribute-overview радар) е в simulate_lineup_v2.py —
отделни файлове по избор на потребителя ("нека са отделни").

Голово разпределение (сменено 2026-08-02, по избор на потребителя): Negative
Binomial вместо Poisson — виж dixon_coles.NB_ALPHA/match_markets_nbinom().
Dixon-Coles (DC) и v2 остават на Poisson (score_matrix/match_markets,
непроменени) — смяната е само за v1.

Shrinkage при тънка lineup извадка (добавено 2026-08-02): прогнозният състав
понякога дава xg/xa данни само за 2-3 играча — твърде малко за надежден
lineup-специфичен индекс сам по себе си. attack/defense_index вече е смес
между lineup-специфичното число и team_regular_indices() (отборната средна
от обичайните титуляри, не филтрирана по днешния прогнозен състав), тегло
пропорционално на броя lineup играчи с реални данни — виж shrink_index().
Остава lineup-anchored модел (не team-level замяна), просто по-устойчив на
тънки извадки.

ПОПРАВКА (2026-07): attack_index беше СУМА от xg+xa на класифицираните
"attack" играчи — несиметрично спрямо defense_index (СРЕДНО). Броят играчи,
класифицирани в "attack" групата, варира по отбор/формация (открито на
реален случай: Brighton 6 срещу Leeds 4 "attack"-играча) — сума директно
зависи от този брой, не само от качеството, което изкриви резултатите
системно в полза на отбори с повече "attack"-класифицирани играчи. Сега
СРЕДНО, симетрично на defense_index.

Употреба:
    python engine/simulate_lineup_v1.py --league ELITESERIEN --home "Sarpsborg 08" \
        --away HamKam --event-id 15260857
"""
import sqlite3

import pandas as pd

from dixon_coles import match_markets_nbinom, fair, calibrate_lambda_mu
from lineup_impact import fetch_lineup_starters, _load_player_matches, _attack_weight, _blended_rating, _team_match_count
from config import DATA_DIR

LEAGUES = {
    # ПОПРАВКА (2026-09-08): "EPL": "sofa.db" сочеше към несъществуваща база
    # (EPL е в sofa_epl.db от 26/27 сезона), а половината активни лиги
    # липсваха изобщо — backtest_lineup_v1.py отказваше да ги пусне.
    "EPL": "sofa_epl.db", "LALIGA": "sofa_laliga.db", "LIGUE1": "sofa_ligue1.db",
    "ELITESERIEN": "sofa_eliteserien.db", "ALLSVENSKAN": "sofa_allsvenskan.db",
    "BUNDESLIGA": "sofa_bundesliga.db", "BUNDESLIGA2": "sofa_bundesliga2.db",
    "CHAMPIONSHIP": "sofa_championship.db", "BELGIUMPL": "sofa_belgiumpl.db",
    "LALIGA2": "sofa_laliga2.db", "SERIEA": "sofa_seriea.db", "FAROE": "sofa_faroe.db",
}

HOME_ADV = 1.15  # начална стойност, не backtest-калибрирана
SHRINK_K = 3.0   # shrinkage константа за team_regular_indices fallback (виж shrink_index) —
                # начална стойност, не backtest-калибрирана


def _xg_xa_per_match(rows: pd.DataFrame) -> float | None:
    rated = rows.dropna(subset=["xg", "xa"])
    if rated.empty:
        return None
    # ПОПРАВКА (2026-07): виж lineup_impact._blended_rating — season_avg
    # преди включваше и последните 5 мача, надувайки ефективното тегло на
    # "последните 5" над документираните 60/40. Непресичащ се остатък.
    combined = rated["xg"] + rated["xa"]
    recent_avg = combined.tail(5).mean()
    older = combined.iloc[:-5] if len(combined) > 5 else combined.iloc[0:0]
    if older.empty:
        return recent_avg
    return 0.6 * recent_avg + 0.4 * older.mean()


def team_indices(con: sqlite3.Connection, team: str, as_of: pd.Timestamp,
                 starting_names: set[str]) -> dict:
    """(attack_index, defense_index) за КОНКРЕТНИЯ състав starting_names —
    и двете ПРЕТЕГЛЕНО СРЕДНО (не проста сума), за да са сравними между
    отбори с различен брой класифицирани attack/defense играчи.

    ПОПРАВКА (2026-09-04, по случая ван Дайк): преди всеки играч се броеше
    100% в ЕДНАТА посока (бинарно, чрез _player_group — D/G винаги "defense",
    нула принос в атаката) — "защитник не може да вкара гол" беше буквалният
    резултат от кода. Огледално на simulate_lineup_v2.py's team_indices()
    (случаят Sean Longstaff, 2026-07) — сега всеки играч участва И в двете
    посоки, тежи според собствения си _attack_weight()."""
    df = _load_player_matches(con, team, as_of)
    attack_vals, attack_wts, defense_vals, defense_wts = [], [], [], []
    for name, rows in df.groupby("name"):
        if name not in starting_names:
            continue
        position = rows["position"].mode().iat[0] if not rows["position"].mode().empty else None
        w = _attack_weight(position, rows)
        xg_val = _xg_xa_per_match(rows)
        if xg_val is not None and w > 0:
            attack_vals.append(xg_val)
            attack_wts.append(w)
        rating_val = _blended_rating(rows)
        if rating_val is not None and (1 - w) > 0:
            defense_vals.append(rating_val)
            defense_wts.append(1 - w)
    attack_index = (sum(v * w for v, w in zip(attack_vals, attack_wts)) / sum(attack_wts)) if attack_wts else None
    defense_index = (sum(v * w for v, w in zip(defense_vals, defense_wts)) / sum(defense_wts)) if defense_wts else None
    return {"attack_index": attack_index, "defense_index": defense_index,
           "n_attack": len(attack_vals), "n_defense": len(defense_vals)}


def team_regular_indices(con: sqlite3.Connection, team: str, as_of: pd.Timestamp) -> dict:
    """(attack_index, defense_index) от ОБИЧАЙНИТЕ титуляри на отбора
    (>=50% старта до момента) — НЕ филтрирано по конкретния прогнозен състав
    на днешния мач. Извадено от league_averages() (2026-08-02) за да служи и
    като shrinkage fallback в simulate() при тънка lineup извадка — вижте
    там защо: прогнозният състав понякога дава данни само за 2-3 играча,
    твърде малко за надежден lineup-специфичен индекс сам по себе си."""
    df = _load_player_matches(con, team, as_of)
    tm = _team_match_count(con, team, as_of)
    if df.empty or tm == 0:
        return {"attack_index": None, "defense_index": None, "n_attack": 0, "n_defense": 0}
    attack_vals, attack_wts, defense_vals, defense_wts = [], [], [], []
    for name, rows in df.groupby("name"):
        if len(rows) / tm < 0.5:
            continue
        position = rows["position"].mode().iat[0] if not rows["position"].mode().empty else None
        w = _attack_weight(position, rows)
        xg_val = _xg_xa_per_match(rows)
        if xg_val is not None and w > 0:
            attack_vals.append(xg_val)
            attack_wts.append(w)
        rating_val = _blended_rating(rows)
        if rating_val is not None and (1 - w) > 0:
            defense_vals.append(rating_val)
            defense_wts.append(1 - w)
    return {
        "attack_index": (sum(v * w for v, w in zip(attack_vals, attack_wts)) / sum(attack_wts)) if attack_wts else None,
        "defense_index": (sum(v * w for v, w in zip(defense_vals, defense_wts)) / sum(defense_wts)) if defense_wts else None,
        "n_attack": len(attack_vals), "n_defense": len(defense_vals),
    }


def shrink_index(lineup_val: float | None, lineup_n: int,
                 team_val: float | None, team_n: int, k: float = SHRINK_K) -> float | None:
    """Частично изтегляне на lineup-специфичния индекс към отборната средна
    (team_regular_indices) — тегло на lineup частта = lineup_n/(lineup_n+k).
    Малко играчи с данни в прогнозния състав → тегли повече към отборната
    средна; много играчи → пази се близо до чисто lineup-based числото.
    k=3 е начална, НЕ backtest-калибрирана стойност (същия статус като
    HOME_ADV/NB_ALPHA преди калибрация)."""
    if lineup_val is None:
        return team_val
    if team_val is None:
        return lineup_val
    weight = lineup_n / (lineup_n + k)
    return weight * lineup_val + (1 - weight) * team_val


def league_averages(con: sqlite3.Connection, teams: list[str], as_of: pd.Timestamp) -> dict:
    """Лигова средна, базирана на ОБИЧАЙНИТЕ титуляри (>=50% старта) на всеки
    отбор — стабилна нормализираща база, независима от днешните избори.
    И двата индекса СРЕДНО, симетрично на team_indices()."""
    attacks, defenses = [], []
    for team in teams:
        ti = team_regular_indices(con, team, as_of)
        if ti["attack_index"] is not None:
            attacks.append(ti["attack_index"])
        if ti["defense_index"] is not None:
            defenses.append(ti["defense_index"])

    goals = con.execute("SELECT AVG(hg + ag) FROM matches WHERE date < ?",
                        (as_of.strftime("%Y-%m-%d"),)).fetchone()[0]
    # ПОПРАВКА (2026-07, открито при добавяне на Allsvenskan): ако ЦЯЛАТА лига
    # няма xG данни от Sofascore (виж parse_sofa_api.py бележката за
    # PLAYER_NO_ZERO_DEFAULT), attacks може да остане с реални (не fallback)
    # стойности, всичките равни на 0.0 — тогава avg_attack излиза точно 0.0,
    # не "празен списък", и `if attacks else 0.2` не го хваща. Изрична >0
    # проверка вместо разчитане само на празния списък.
    avg_attack = sum(attacks) / len(attacks) if attacks else 0.2
    avg_defense = sum(defenses) / len(defenses) if defenses else 6.5
    return {
        "avg_attack": avg_attack if avg_attack > 0 else 0.2,
        "avg_defense": avg_defense if avg_defense > 0 else 6.5,
        "avg_goals_per_team": (goals or 2.6) / 2,
    }


def simulate(con: sqlite3.Connection, home: str, away: str, as_of: pd.Timestamp,
            home_starters: set[str], away_starters: set[str], all_teams: list[str],
            league: str | None = None) -> dict:
    lg = league_averages(con, all_teams, as_of)
    h_lineup = team_indices(con, home, as_of, home_starters)
    a_lineup = team_indices(con, away, as_of, away_starters)
    h_team = team_regular_indices(con, home, as_of)
    a_team = team_regular_indices(con, away, as_of)

    # Shrinkage (добавено 2026-08-02, по искане на потребителя): прогнозният
    # състав понякога дава xg/xa само за 2-3 играча — твърде тънка извадка да
    # се ползва сурово. attack/defense_index вече е претеглена смес между
    # lineup-специфичното число и отборната средна (team_regular_indices),
    # тегло пропорционално на броя lineup играчи с данни (виж shrink_index).
    # n_attack/n_defense в отчета остават LINEUP бройката (не смесената) —
    # показва точно колко играчи от ДНЕШНИЯ състав реално са допринесли.
    h = {
        "attack_index": shrink_index(h_lineup["attack_index"], h_lineup["n_attack"],
                                     h_team["attack_index"], h_team["n_attack"]),
        "defense_index": shrink_index(h_lineup["defense_index"], h_lineup["n_defense"],
                                      h_team["defense_index"], h_team["n_defense"]),
        "n_attack": h_lineup["n_attack"], "n_defense": h_lineup["n_defense"],
    }
    a = {
        "attack_index": shrink_index(a_lineup["attack_index"], a_lineup["n_attack"],
                                     a_team["attack_index"], a_team["n_attack"]),
        "defense_index": shrink_index(a_lineup["defense_index"], a_lineup["n_defense"],
                                      a_team["defense_index"], a_team["n_defense"]),
        "n_attack": a_lineup["n_attack"], "n_defense": a_lineup["n_defense"],
    }

    # Вече проваля само ако И lineup, И отборната средна нямат данни (преди
    # един липсващ lineup индекс беше достатъчен — shrinkage с fallback на
    # team_regular_indices прави чисто лишен провал по-рядък случай).
    if h["attack_index"] is None or a["attack_index"] is None \
            or h["defense_index"] is None or a["defense_index"] is None:
        return {"error": "недостатъчно данни за някой от отборите (нито lineup, нито отборна средна)",
               "home_idx": h, "away_idx": a}

    # ПОПРАВКА (2026-07): виж simulate_lineup_v2.py — HOME_ADV само върху lam
    # изкривяваше общата сума над реалната лигова средна за всеки мач.
    home_mult = 2 * HOME_ADV / (1 + HOME_ADV)
    away_mult = 2 / (1 + HOME_ADV)

    lam = (lg["avg_goals_per_team"] * home_mult * (h["attack_index"] / lg["avg_attack"])
          / (a["defense_index"] / lg["avg_defense"]))
    mu = (lg["avg_goals_per_team"] * away_mult * (a["attack_index"] / lg["avg_attack"])
         / (h["defense_index"] / lg["avg_defense"]))

    # Рекалибрация (2026-09-08) — собствени коефициенти за v1, различни
    # от DC-то (друг източник на λ/μ — играчов xg/xa срещу отборен xG).
    # lam_raw/mu_raw се връщат изрично — backtest_lineup_v1.py ЗАДЪЛЖИТЕЛНО записва
    # тях, иначе следващата калибрация би се учила от вече коригирани
    # стойности и би приложила свиването два пъти.
    lam_raw, mu_raw = lam, mu
    lam, mu = calibrate_lambda_mu(lam, mu, league, model="v1")
    return {"lam": lam, "mu": mu, "lam_raw": lam_raw, "mu_raw": mu_raw,
           "home_idx": h, "away_idx": a, "league": lg,
           **match_markets_nbinom(lam, mu)}


def main() -> None:
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--event-id", type=int, required=True)
    args = ap.parse_args()

    db_name = LEAGUES.get(args.league.upper())
    if not db_name:
        raise SystemExit(f"Непозната лига. Известни: {', '.join(LEAGUES)}")
    db_path = DATA_DIR / db_name
    con = sqlite3.connect(db_path)

    home_starters, away_starters, confirmed = fetch_lineup_starters(args.event_id)
    if not home_starters or not away_starters:
        raise SystemExit("Sofascore още няма състав (нито прогнозен) за този мач.")

    all_teams = [r[0] for r in con.execute("SELECT DISTINCT home FROM matches").fetchall()]
    as_of = pd.Timestamp.now()

    res = simulate(con, args.home, args.away, as_of, home_starters, away_starters, all_teams)
    if "error" in res:
        print(res["error"], res.get("home_idx"), res.get("away_idx"))
        return

    print(f"Състав: {'потвърден' if confirmed else 'прогнозен'}")
    print(f"{args.home}: attack_index={res['home_idx']['attack_index']:.3f} "
         f"(от {res['home_idx']['n_attack']} играча), "
         f"defense_index={res['home_idx']['defense_index']:.2f} "
         f"(от {res['home_idx']['n_defense']} играча)")
    print(f"{args.away}: attack_index={res['away_idx']['attack_index']:.3f} "
         f"(от {res['away_idx']['n_attack']} играча), "
         f"defense_index={res['away_idx']['defense_index']:.2f} "
         f"(от {res['away_idx']['n_defense']} играча)")
    print(f"\nlam={res['lam']:.2f}  mu={res['mu']:.2f}")
    print(f"1={res['p_home']:.1%} ({fair(res['p_home'])})  "
         f"X={res['p_draw']:.1%} ({fair(res['p_draw'])})  "
         f"2={res['p_away']:.1%} ({fair(res['p_away'])})")
    print(f"Над2.5={res['p_over25']:.1%} ({fair(res['p_over25'])})  "
         f"ГГ={res['p_btts']:.1%} ({fair(res['p_btts'])})")
    print(f"Топ резултати: {res['top_scores']}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
