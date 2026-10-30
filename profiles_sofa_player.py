"""Играчов att_index/def_index/shots90 върху Sofascore данни (player_match
в sofa.db) — огледално на profiles.py, но за да ПРОВЕРИМ дали lineup-моделът
(simulate.py: lineup-индекс / baseline, клампнато 0.75-1.25) изобщо дава
смислени числа извън Opta клипингите, преди да разширяваме към Ла Лига.

САМО валидация — НЕ е закачено към simulate.py/simulate_sofa.py.

Разлики спрямо profiles.py (Sofascore не проследява същото на ниво играч):
  sca      Opta: асистенции + key passes -> Sofa: assists + key_passes.
           Същата дефиниция (виж коментара в db.py: "sca — shots created
           (асистенции + key passes)"), само различни имена на колони.
  blocked  Opta def_actions = tackles + interceptions + 0.5×blocked, където
           "blocked" е БЛОКОВЕ, НАПРАВЕНИ от играча (защитно действие).
           Sofascore НЯМА такова поле на ниво играч — 'blockedScoringAttempt'
           е обратното (собствен удар на играча, блокиран от съперника —
           атакуващ показател, вече влиза в 'shots'). Заместено с clearances
           (totalClearance), най-близкото налично "последна линия" защитно
           действие; тегло 0.3 вместо 0.5× (изчиствания са по-чести и по-малко
           дискриминиращи от истински блокове — приблизително, некалибрирано).
  group by sofa_player_id, не по име — Sofascore дава стабилен номерен id
           (Opta нямаше такъв, оттам групирането по име в оригинала).

Употреба:  python engine/profiles_sofa_player.py [--top 15] [--db data/sofa.db]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from config import DATA_DIR

SOFA_DB = DATA_DIR / "sofa.db"

M_SHRINK = 600
SCA_XG = 0.10
POS_MAP = {"G": "GK", "D": "DF", "M": "MF", "F": "FW"}
ATT_W = {"GK": 0.0, "DF": 0.5, "MF": 1.0, "FW": 1.5}
DEF_W = {"GK": 1.5, "DF": 1.5, "MF": 1.0, "FW": 0.3}


def _load(con):
    pm = pd.read_sql_query(
        """SELECT match_id, team, sofa_player_id, name, position, minutes,
                  xg, assists, key_passes, tackles, interceptions,
                  clearances, shots, goals
           FROM player_match""", con)
    matches = pd.read_sql_query("SELECT match_id, home, away FROM matches", con)
    xg_team = pd.read_sql_query(
        "SELECT match_id, team, xg FROM stats WHERE period = 'full'", con)

    matches = matches.merge(
        xg_team.rename(columns={"team": "home", "xg": "home_xg"}),
        on=["match_id", "home"], how="left")
    matches = matches.merge(
        xg_team.rename(columns={"team": "away", "xg": "away_xg"}),
        on=["match_id", "away"], how="left")

    pm = pm.merge(matches, on="match_id", how="left")
    return pm


def build_player_profiles(con) -> dict:
    pm = _load(con)
    if pm.empty:
        raise SystemExit("player_match е празна — пусни engine/parse_sofa_api.py")

    pm["pos"] = pm["position"].map(POS_MAP).fillna("MF")
    pm["team_xg_conceded"] = np.where(pm["team"] == pm["home"],
                                      pm["away_xg"], pm["home_xg"])
    pm["sca"] = pm["assists"].fillna(0) + pm["key_passes"].fillna(0)
    pm["att_raw"] = pm["xg"].fillna(0) + SCA_XG * pm["sca"]
    pm["def_actions"] = (pm["tackles"].fillna(0) + pm["interceptions"].fillna(0)
                         + 0.3 * pm["clearances"].fillna(0))
    pm["conceded_share"] = pm["team_xg_conceded"].fillna(0) * pm["minutes"] / 90.0

    g = pm.groupby("sofa_player_id").agg(
        name=("name", "last"), team=("team", "last"),
        minutes=("minutes", "sum"), matches=("match_id", "nunique"),
        att_raw=("att_raw", "sum"), def_actions=("def_actions", "sum"),
        conceded=("conceded_share", "sum"), xg=("xg", "sum"),
        sca=("sca", "sum"), goals=("goals", "sum"), shots=("shots", "sum"))
    g["pos"] = pm.groupby("sofa_player_id")["pos"].agg(lambda s: s.mode().iat[0])

    pos_mean_att = (g.groupby("pos")
                     .apply(lambda x: 90 * x["att_raw"].sum() / x["minutes"].sum(),
                            include_groups=False))
    pos_mean_def = (g.groupby("pos")
                     .apply(lambda x: 90 * x["def_actions"].sum() / x["minutes"].sum(),
                            include_groups=False))
    pos_mean_shots = (g.groupby("pos")
                      .apply(lambda x: 90 * x["shots"].sum() / x["minutes"].sum(),
                             include_groups=False))
    league_conceded90 = 90 * g["conceded"].sum() / g["minutes"].sum()

    def shrink(total, minutes, prior90):
        return 90 * (total + prior90 * M_SHRINK / 90) / (minutes + M_SHRINK)

    g["att90"] = shrink(g["att_raw"], g["minutes"], g["pos"].map(pos_mean_att))
    g["def_act90"] = shrink(g["def_actions"], g["minutes"], g["pos"].map(pos_mean_def))
    g["conceded90"] = shrink(g["conceded"], g["minutes"],
                             pd.Series(league_conceded90, index=g.index))
    g["shots90"] = shrink(g["shots"], g["minutes"], g["pos"].map(pos_mean_shots))

    mean_att = 90 * g["att_raw"].sum() / g["minutes"].sum()
    mean_def = 90 * g["def_actions"].sum() / g["minutes"].sum()
    g["att_index"] = g["att90"] / mean_att
    g["def_index"] = (0.6 * g["def_act90"] / mean_def
                      + 0.4 * league_conceded90 / g["conceded90"])
    g["att_w"] = g["pos"].map(ATT_W).fillna(1.0)
    g["def_w"] = g["pos"].map(DEF_W).fillna(1.0)
    g["shots_w"] = g["att_w"]

    pm2 = pm.merge(
        g[["att_index", "def_index", "att_w", "def_w", "shots90", "shots_w"]],
        left_on="sofa_player_id", right_index=True)

    def lineup_baseline(x, idx, w):
        return np.average(x[idx], weights=x[w] * x["minutes"])

    base = pm2.groupby("team").apply(
        lambda x: pd.Series({
            "baseline_att": lineup_baseline(x, "att_index", "att_w"),
            "baseline_def": lineup_baseline(x, "def_index", "def_w"),
            "baseline_shots": lineup_baseline(x, "shots90", "shots_w")}),
        include_groups=False)

    return {"players": g, "team_baseline": base}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--db", default=str(SOFA_DB))
    ap.add_argument("--min-minutes", type=int, default=450,
                    help="филтър за показване (shrinkage-ът си работи и без него)")
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(args.db)
    prof = build_player_profiles(con)
    p = prof["players"]
    b = prof["team_baseline"]

    shown = p[p["minutes"] >= args.min_minutes]

    print(f"Играчи общо: {len(p)}   (>= {args.min_minutes} мин: {len(shown)})\n")

    print(f"ТОП {args.top} по att_index (shrinkage M={M_SHRINK})")
    view = shown.sort_values("att_index", ascending=False).head(args.top)
    print(view[["name", "team", "pos", "minutes", "goals", "xg", "att_index", "def_index"]]
          .round(3).to_string())

    print(f"\nТОП {args.top} по def_index")
    view = shown.sort_values("def_index", ascending=False).head(args.top)
    print(view[["name", "team", "pos", "minutes", "att_index", "def_index"]]
          .round(3).to_string())

    print(f"\nТОП {args.top} по shots90")
    view = shown.sort_values("shots90", ascending=False).head(args.top)
    print(view[["name", "team", "pos", "minutes", "shots", "shots90"]]
          .round(3).to_string())

    print(f"\nНАЙ-НИСЪК att_index сред нападатели/полузащитници "
         f"(санитарна проверка — очакваме вратари/бекове тук, не звезди)")
    view = shown[shown["pos"].isin(["FW", "MF"])].sort_values("att_index").head(8)
    print(view[["name", "team", "pos", "minutes", "att_index"]].round(3).to_string())

    print("\nБАЗОВИ LINEUP-ИНДЕКСИ ПО ОТБОР (baseline_att — сезонно средно)")
    print(b.sort_values("baseline_att", ascending=False).round(3).to_string())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
