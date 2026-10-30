"""Модел В — симулация директно от Sofascore player attribute-overview
(ATT/CRE/TEC/DEF/TAC радар, виждан на профилната страница на играча) за
СТАРТОВИЯ СЪСТАВ (11 играча, прогнозен или потвърден), не от статистика на
мачове (xg/rating) и не от сезонния тим-average (Dixon-Coles) или
league-wide ранговете (model_b_heuristic.py). Четвърти/трети независим
паралелен модел, за сравнение.

За всеки от 11-те играча се взимат неговите 5 атрибута директно от
Sofascore (не се смятат от нашата статистика). Team attack/defense index =
среден ATT/DEF (с лека добавка от CRE/TAC) на ЦЕЛИЯ състав, не само
нападатели/защитници — всеки играч допринася по малко и за двете посоки,
точно както си му е реалния ATT/DEF профил.

Структурно все пак минава през същата dixon_coles.score_matrix/
match_markets Poisson математика, за консистентност с останалите модели.

Употреба:
    python engine/simulate_lineup.py --league ELITESERIEN --home "Sarpsborg 08" \
        --away HamKam --event-id 15260857
"""
import sqlite3
import time

import pandas as pd

from dixon_coles import score_matrix, match_markets, fair, rho_for_league
from lineup_impact import (fetch_lineup_starters_detailed, BEST_EFFORT_TIMEOUT,
                           player_attributes_json, SOFASCORE_ENABLED)
from config import DATA_DIR


def _polite_pause() -> None:
    """Пауза между заявките към Sofascore. При изключен Sofascore (само кеш) няма заявка,
    а паузата струваше ~110 сек. на лига (220 × 0.5 сек.; открито 2026-10-27)."""
    if SOFASCORE_ENABLED:
        time.sleep(0.5)


LEAGUES = {
    "EPL": "sofa.db", "LALIGA": "sofa_laliga.db", "LIGUE1": "sofa_ligue1.db",
    "ELITESERIEN": "sofa_eliteserien.db",
}

ATT_W, CRE_W = 0.7, 0.3   # attack index = 0.7*attacking + 0.3*creativity
DEF_W, TAC_W = 0.7, 0.3   # defense index = 0.7*defending + 0.3*tactical
HOME_ADV = 1.15  # начална, некалибрирана стойност — виж бележка в main()


def _sofa_attributes(player_id: int) -> dict | None:
    """Всичките 5 атрибута (attacking/technical/tactical/defending/
    creativity) за текущия сезон (yearShift=0), само ПОЛЕВИ играчи. None
    при вратар (различни полета — виж _sofa_gk_attributes) или липса на
    данни."""
    try:
        data = player_attributes_json(player_id)   # cached, see lineup_impact._cached_json()
        if data is None:
            return None
        for ov in data.get("playerAttributeOverviews", []):
            if ov.get("yearShift") == 0 and "attacking" in ov:
                return ov
    except Exception as e:
        print(f"⚠ _sofa_attributes({player_id}) грешка: {e}")
        return None
    return None


GK_SAVES_W, GK_AERIAL_W = 0.7, 0.3  # вратарска "defense_val" = 0.7*saves + 0.3*aerial


def _sofa_gk_attributes(player_id: int) -> dict | None:
    """Вратарски атрибути (saves/aerial/tactical/anticipation/
    ballDistribution — различна схема от полевите играчи). None при
    полеви играч или липса на данни."""
    try:
        data = player_attributes_json(player_id)   # cached, see lineup_impact._cached_json()
        if data is None:
            return None
        for ov in data.get("playerAttributeOverviews", []):
            if ov.get("yearShift") == 0 and "saves" in ov:
                return ov
    except Exception as e:
        print(f"⚠ _sofa_gk_attributes({player_id}) грешка: {e}")
        return None
    return None


def team_indices(starters: dict[str, tuple[int, str]]) -> dict:
    """starters: {име: (sofa_player_id, позиция)} — директно от Sofascore-овия
    lineup отговор за ТОЗИ мач (fetch_lineup_starters_detailed), НЕ от
    нашата локална player_match история (2026-08-28, по искане на
    потребителя: "атрибутите няма да се променят драстично за 1 мач...
    нека няма значение дали играч има изигран мач"). Преди играч без НИТО
    един записан мач при нас (напр. чисто нов трансфер) отпадаше мълчаливо,
    защото не му знаехме Sofascore ID-то локално — вече не отпада, ID-то
    идва директно от състава на самия мач.

    ПОПРАВКА (2026-07, все още в сила): преди всеки играч се брояше 100% в
    ЕДНАТА посока (attack ИЛИ defense, бинарно) — конкретен случай Sean
    Longstaff (attacking=57, defending=53, реален централен халф) показа,
    че така неговите реални защитни действия изобщо не влизаха в отборната
    защита. Сега всеки играч участва И в двете, с тегло според собствения
    му attacking/defending дял (57/53 → ~52% принос в атаката, ~48% в
    защитата — не бинарно 100%/0%)."""
    attack_vals, attack_wts = [], []
    defense_vals, defense_wts = [], []
    found = 0
    for name, (pid, position) in starters.items():
        if position == "G":
            # ПОПРАВКА (2026-07): вратарите преди изобщо не участваха (нямат
            # 'attacking' поле, _sofa_attributes връщаше None за тях) —
            # смяна на вратар е един от най-силните единични ефекти във
            # футбола. Вратарска defense_val от saves/aerial, изцяло в
            # защитната посока (0% принос в атаката).
            gk = _sofa_gk_attributes(int(pid))
            _polite_pause()
            if gk is None:
                continue
            found += 1
            defense_vals.append(GK_SAVES_W * gk["saves"] + GK_AERIAL_W * gk["aerial"])
            defense_wts.append(1.0)
            continue

        attrs = _sofa_attributes(int(pid))
        _polite_pause()
        if attrs is None:
            continue
        found += 1
        total = attrs["attacking"] + attrs["defending"]
        att_w = attrs["attacking"] / total if total else 0.5
        def_w = 1 - att_w

        attack_vals.append(ATT_W * attrs["attacking"] + CRE_W * attrs["creativity"])
        attack_wts.append(att_w)
        defense_vals.append(DEF_W * attrs["defending"] + TAC_W * attrs["tactical"])
        defense_wts.append(def_w)

    if not attack_vals or sum(attack_wts) == 0 or sum(defense_wts) == 0:
        return {"attack_index": None, "defense_index": None, "n_found": 0}
    return {
        "attack_index": sum(v * w for v, w in zip(attack_vals, attack_wts)) / sum(attack_wts),
        "defense_index": sum(v * w for v, w in zip(defense_vals, defense_wts)) / sum(defense_wts),
        "n_found": found,
    }


def league_averages(con: sqlite3.Connection, as_of: pd.Timestamp) -> dict:
    goals = con.execute("SELECT AVG(hg + ag) FROM matches WHERE date < ?",
                        (as_of.strftime("%Y-%m-%d"),)).fetchone()[0]
    # среден attack/defense атрибут в лигата ~50 по дизайн на Sofascore скалата
    # (0-99, центрирана около средата за league-average играч) — ползваме 50
    # като нормализираща база вместо да теглим целия състав на всеки отбор.
    return {"avg_attack": 50.0, "avg_defense": 50.0, "avg_goals_per_team": (goals or 2.6) / 2}


def simulate(con: sqlite3.Connection, home: str, away: str, as_of: pd.Timestamp,
            home_starters: dict[str, tuple[int, str]], away_starters: dict[str, tuple[int, str]],
            league: str | None = None) -> dict:
    # home/away (имената) вече не се ползват за lookup тук (2026-08-28) — само
    # за симетрия с останалите модели/CLI извежданията. home_starters/
    # away_starters вече са {име: (sofa_id, позиция)}, не голи имена.
    lg = league_averages(con, as_of)
    h = team_indices(home_starters)
    a = team_indices(away_starters)

    if h["attack_index"] is None or a["attack_index"] is None:
        return {"error": "недостатъчно данни за някой от съставите", "home_idx": h, "away_idx": a}

    # ПОПРАВКА (2026-07): HOME_ADV прилаган само върху lam изкривяваше ОБЩАТА
    # сума lam+mu над реалната лигова средна за ВСЕКИ мач (за средна среща:
    # 1.15+1.0=2.15 вместо вярното 2.0 → систематично завишени Над2.5/ГГ
    # навсякъде, открито от потребителя при проверка на реални report данни).
    # Сега HOME_ADV разпределя предимството симетрично (запазва same
    # съотношение lam/mu=HOME_ADV за средна среща), но сборът остава закотвен
    # към истинската лигова средна: HOME_MULT+AWAY_MULT = 2.
    home_mult = 2 * HOME_ADV / (1 + HOME_ADV)
    away_mult = 2 / (1 + HOME_ADV)

    lam = (lg["avg_goals_per_team"] * home_mult * (h["attack_index"] / lg["avg_attack"])
          / (a["defense_index"] / lg["avg_defense"]))
    mu = (lg["avg_goals_per_team"] * away_mult * (a["attack_index"] / lg["avg_attack"])
         / (h["defense_index"] / lg["avg_defense"]))

    return {"lam": lam, "mu": mu, "home_idx": h, "away_idx": a, "league": lg,
           **match_markets(lam, mu, rho=rho_for_league(league))}


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

    home_starters, away_starters, confirmed = fetch_lineup_starters_detailed(args.event_id)
    if not home_starters or not away_starters:
        raise SystemExit("Sofascore още няма състав (нито прогнозен) за този мач.")

    as_of = pd.Timestamp.now()

    res = simulate(con, args.home, args.away, as_of, home_starters, away_starters)
    if "error" in res:
        print(res["error"], res.get("home_idx"), res.get("away_idx"))
        return

    print(f"Състав: {'потвърден' if confirmed else 'прогнозен'}")
    print(f"{args.home}: attack_index={res['home_idx']['attack_index']:.1f}  "
         f"defense_index={res['home_idx']['defense_index']:.1f}  "
         f"(намерени {res['home_idx']['n_found']}/11 играча)")
    print(f"{args.away}: attack_index={res['away_idx']['attack_index']:.1f}  "
         f"defense_index={res['away_idx']['defense_index']:.1f}  "
         f"(намерени {res['away_idx']['n_found']}/11 играча)")
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
