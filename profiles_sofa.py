"""Отборни профили от Sofascore данни (sofa.db) — огледално на profiles.py,
но БЕЗ играчова гранулярност (sofa.db е на ниво отбор, не играч).

Два модела, паралелно на Опта версията:
  own_xg / xg_conceded        — директно от Sofascore xG (period='full')
  threat_for / threat_against — претеглена сума на 3 независими офанзивни
                                 оси (SoT, big chances, корнери), огледално
                                 на THREAT_WEIGHTS в profiles.py; заместваме
                                 SCA→big_chances (по-чист "създаване на
                                 положение" сигнал в Sofascore данните) и
                                 центрирания отпадат (не се парсват надеждно
                                 — виж бележката в parse_sofa.py).

Форма: decay по БРОЙ мачове назад (не дни), същия принцип като profiles.py.
Лигови константи: avg_xg, home_adv, avg_threat, threat_home_adv,
goals_per_threat (реални голове/threat, БЕЗ xG) — всички изчислени само от
sofa.db, независимо от Опта базата.

Употреба:  python engine/profiles_sofa.py [--db data/sofa.db]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlite3

XI = 0.0019
FORM_HALFLIFE = 5
FORM_CAP = (0.80, 1.25)

# тегла на офанзивните оси (пренесени от profiles.THREAT_WEIGHTS; sca→big_chances)
THREAT_WEIGHTS_SOFA = {"sot": 0.80, "big_chances": 0.60, "corners": 0.15}

SOFA_DB = DATA_DIR / "sofa.db"


def _connect(db_path: Path = SOFA_DB) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


RATING_MIN_MATCHES = 3   # под това — недостатъчно за смислено "скорошно vs сезонно"
RATING_FORM_CAP = (0.85, 1.15)   # по-тесен от голевата форма — рейтингът е по-стабилен


def _load(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    matches = pd.read_sql_query("SELECT * FROM matches", con, parse_dates=["date"])
    stats = pd.read_sql_query("SELECT * FROM stats WHERE period='full'", con)
    return matches, stats


def _read_ratings_table(con) -> pd.DataFrame:
    """Играчови рейтинги — предпочита player_match (нов API пайплайн,
    parse_sofa_api.py), пада на player_ratings (стар vault пайплайн,
    parse_sofa.py) само ако player_match липсва/е празна.

    ПОПРАВКА (2026-08-09): _rating_form() преди четеше ИЗКЛЮЧИТЕЛНО от
    player_ratings — за лигите на новия API пайплайн (Eliteserien/
    Allsvenskan/Bundesliga2) тази таблица изобщо не съществува (потвърдено:
    0 реда/липсваща във всичките 3 активни бази), затова rating_form тихо
    падаше на неутрално 1.0 навсякъде активно, макар player_match.rating да
    е добре населена (3843/3927 за Eliteserien и т.н.). Играчовите редове са
    и на двата отбора в мача — груповането по `team` по-долу ги разделя,
    без значение от кой пайплайн идват."""
    try:
        df = pd.read_sql_query(
            "SELECT match_id, team, rating FROM player_match WHERE rating IS NOT NULL", con)
        if not df.empty:
            return df
    except Exception:
        pass
    try:
        return pd.read_sql_query("SELECT match_id, team, rating FROM player_ratings", con)
    except Exception:
        return pd.DataFrame(columns=["match_id", "team", "rating"])


def _rating_form(con, matches: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    """Форма от играчовите рейтинги — среден рейтинг на титулярите за
    последните 5 (реално оценени) мача спрямо сезонния среден на отбора.
    Допълнителен, независим сигнал от xG/threat формата (субективна оценка
    на представянето, не само резултат от статистиката). Неутрален (1.0)
    при <RATING_MIN_MATCHES оценени мача — все още градим историята."""
    pr = _read_ratings_table(con)
    if pr.empty:
        return pd.Series(dtype=float)
    tm = pr.groupby(["match_id", "team"])["rating"].mean().reset_index()
    tm = tm.merge(matches[["match_id", "date"]], on="match_id")
    tm = tm[tm["date"] < as_of].sort_values("date")

    lo, hi = RATING_FORM_CAP
    out = {}
    for team, g in tm.groupby("team"):
        if len(g) < RATING_MIN_MATCHES:
            continue
        season_avg = g["rating"].mean()
        recent_avg = g["rating"].tail(5).mean()
        out[team] = float(np.clip(recent_avg / season_avg, lo, hi)) if season_avg > 0 else 1.0
    return pd.Series(out, name="rating_form")


def build_profiles(con, as_of: pd.Timestamp | None = None) -> dict:
    matches, stats = _load(con)
    if matches.empty:
        raise SystemExit("sofa.db е празна — пусни първо engine/parse_sofa.py")
    if as_of is None:
        as_of = matches["date"].max() + pd.Timedelta(days=1)

    # ПОПРАВКА (2026-09-04): мачовете НЕ се филтрираха по as_of — само
    # _rating_form() го правеше. `.clip(lower=0)` при теглата долу дава на
    # всеки мач СЛЕД as_of пълно тегло exp(0)=1.0, вместо да го изключи: при
    # build_profiles(con, as_of=минала_дата) върху пълна база профилът тихо
    # "виждаше бъдещето" (лигови константи, форма, всичко). Бектестът се
    # спасяваше само защото подава предварително филтрирано in-memory копие
    # (backtest_sofa._filtered_con) — капанът стоеше за всяко друго извикване.
    # Филтрира се ВЕДНЪЖ тук, за да няма частично филтрирано състояние
    # (long/home_s/away_s/лигови суми надолу ползват вече изчистените данни).
    matches = matches[matches["date"] < as_of]
    if matches.empty:
        raise SystemExit(f"Няма мачове преди {as_of:%Y-%m-%d} — нищо за профилиране.")
    stats = stats[stats["match_id"].isin(set(matches["match_id"]))]

    s = stats.copy()
    W = THREAT_WEIGHTS_SOFA
    s["threat"] = sum(W[c] * s[c].fillna(0) for c in W)

    home_s = s.merge(matches[["match_id", "home", "away", "date"]], on="match_id")
    home_s = home_s[home_s["team"] == home_s["home"]]
    away_s = s.merge(matches[["match_id", "home", "away", "date"]], on="match_id")
    away_s = away_s[away_s["team"] == away_s["away"]]

    long = pd.concat([
        home_s.rename(columns={"team": "team", "xg": "xg_for", "threat": "threat_for"})
              .assign(venue="home"),
        away_s.rename(columns={"team": "team", "xg": "xg_for", "threat": "threat_for"})
              .assign(venue="away"),
    ], ignore_index=True)
    # добавяме "срещу" стойностите (на другия отбор в същия мач)
    opp = s.set_index(["match_id", "team"])
    def opp_val(row, col):
        opp_team = row["away"] if row["venue"] == "home" else row["home"]
        try:
            return opp.loc[(row["match_id"], opp_team), col]
        except KeyError:
            return np.nan
    long["xg_against"] = long.apply(lambda r: opp_val(r, "xg"), axis=1)
    long["threat_against"] = long.apply(lambda r: opp_val(r, "threat"), axis=1)
    long["shots_for"] = long["shots"]
    long["shots_against"] = long.apply(lambda r: opp_val(r, "shots"), axis=1)

    long = long.dropna(subset=["xg_for", "xg_against", "threat_for", "threat_against"])
    long["w"] = np.exp(-XI * (as_of - long["date"]).dt.days.clip(lower=0))

    def wavg(g, col):
        return np.average(g[col], weights=g["w"])

    teams = long.groupby("team").apply(
        lambda g: pd.Series({
            "matches": len(g),
            "own_xg": wavg(g, "xg_for"), "xg_conceded": wavg(g, "xg_against"),
            "threat_for": wavg(g, "threat_for"), "threat_against": wavg(g, "threat_against"),
            "shots_for": wavg(g, "shots_for"), "shots_against": wavg(g, "shots_against"),
        }), include_groups=False)

    # форма — decay по брой мачове назад
    lf = long.sort_values(["team", "date"], ascending=[True, False]).copy()
    lf["rank"] = lf.groupby("team").cumcount()
    lf["w_form"] = 0.5 ** (lf["rank"] / FORM_HALFLIFE)
    recent = lf.groupby("team").apply(
        lambda g: pd.Series({
            "recent_xg_for": np.average(g["xg_for"], weights=g["w_form"]),
            "recent_xg_against": np.average(g["xg_against"], weights=g["w_form"]),
            "recent_threat_for": np.average(g["threat_for"], weights=g["w_form"]),
            "recent_threat_against": np.average(g["threat_against"], weights=g["w_form"]),
        }), include_groups=False)
    teams = teams.join(recent)
    eps = 0.05
    lo, hi = FORM_CAP
    teams["att_form"] = (teams["recent_xg_for"].clip(lower=eps)
                         / teams["own_xg"].clip(lower=eps)).clip(lo, hi)
    teams["def_form"] = (teams["xg_conceded"].clip(lower=eps)
                         / teams["recent_xg_against"].clip(lower=eps)).clip(lo, hi)
    teams["threat_att_form"] = (teams["recent_threat_for"].clip(lower=eps)
                                / teams["threat_for"].clip(lower=eps)).clip(lo, hi)
    teams["threat_def_form"] = (teams["threat_against"].clip(lower=eps)
                                / teams["recent_threat_against"].clip(lower=eps)).clip(lo, hi)

    rf = _rating_form(con, matches, as_of)
    teams["rating_form"] = rf.reindex(teams.index).fillna(1.0)

    league = {
        "avg_xg": float(np.average(long["xg_for"], weights=long["w"])),
        "avg_threat": float(np.average(long["threat_for"], weights=long["w"])),
        "as_of": as_of,
    }
    k = 20
    hxg = home_s.merge(matches[["match_id"]], on="match_id")["xg"].sum()
    axg = away_s.merge(matches[["match_id"]], on="match_id")["xg"].sum()
    league["home_adv"] = float(np.clip(
        (hxg + k * 1.12 * league["avg_xg"]) / (axg + k * 1.00 * league["avg_xg"]),
        1.0, 1.30))

    ht = home_s["threat"].sum()
    at = away_s["threat"].sum()
    league["threat_home_adv"] = float(np.clip(
        (ht + k * 1.08 * league["avg_threat"]) / (at + k * 1.00 * league["avg_threat"]),
        1.0, 1.25))

    tot_goals = float(matches[["hg", "ag"]].to_numpy().sum())
    tot_threat = float(home_s["threat"].sum() + away_s["threat"].sum())
    league["goals_per_threat"] = tot_goals / tot_threat if tot_threat > 0 else 0.0

    return {"teams": teams, "league": league}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(SOFA_DB))
    args = ap.parse_args()
    con = _connect(Path(args.db))
    prof = build_profiles(con)
    lg = prof["league"]
    print(f"Лига (Sofascore): средно xG {lg['avg_xg']:.2f}, threat {lg['avg_threat']:.2f}, "
          f"home_adv ×{lg['home_adv']:.3f}, threat_home_adv ×{lg['threat_home_adv']:.3f}, "
          f"голове/threat {lg['goals_per_threat']:.3f}\n")
    t = prof["teams"].round(3)
    cols = ["matches", "own_xg", "xg_conceded", "threat_for", "threat_against",
            "att_form", "def_form"]
    print(t[cols].sort_values("own_xg", ascending=False).to_string())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
