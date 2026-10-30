"""H2H (head-to-head) сигнал — matchup ефект с контрол за сила, отделен от
общата сила на отборите (която моделът вече отчита през own_xg/xg_conceded).

Три отделни ефекта, всичките с credibility смесване според ефективния брой
срещи (H2H стойността се тегли към базова линия, докато няма достатъчно
история — 0 срещи → без ефект):

1. Голове и удари (насочено, по (отбор, съперник)) → множител на λ/μ:
     ratio = (стат. на отбора срещу ТОЗИ съперник, времево претеглени)
           / (стат. на отбора срещу ВСИЧКИ, същия период)
     blend = (Σ w·стат_H2H + K·база) / (Σ w + K)
     mult  = clamp( blend / база , CAP )

2. BTTS и Над/Под 2.5 (ненасочено, по двойка отбори — резултатът на мача не
   зависи от кой е "домакин" в историческия смисъл) → директно смесване на
   моделната вероятност с историческата честота:
     p_blended = (n_eff·честота_H2H + K_MARKET·p_модел) / (n_eff + K_MARKET)

Източник: football-data CSV-тата на ValueEngine (EPL 2021-22 → 2025-26).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from config import FOOTBALL_DATA_RAW_DIR

# Суровите football-data CSV-та се четат директно (не през ValueEngine's
# load_matches — тя филтрира по фиксиран whitelist от колони, който не
# включва HC/AC; по-чисто е да не пипаме друг проект заради 2 колони).
FD_RAW_DIR = FOOTBALL_DATA_RAW_DIR

XI = 0.0019          # затихване/ден (както DC модела)
K = 6.0              # псевдо-срещи за голове: при n_eff = K ефектът е наполовина
CAP = (0.85, 1.15)   # таван на голевия множител
K_MARKET = 5.0        # псевдо-срещи за BTTS/Над-Под: при n_eff = K_MARKET → наполовина
K_SHOTS = 6.0        # псевдо-срещи за H2H по удари (същото К като головете)
SHOTS_CAP = (0.85, 1.20)  # таван на H2H множителя за удари

# football-data име → OPTA име
FD_TO_OPTA = {
    "Brighton": "Brighton & Hove Albion", "Leeds": "Leeds United",
    "Man City": "Manchester City", "Man United": "Manchester United",
    "Newcastle": "Newcastle United", "Nott'm Forest": "Nottingham Forest",
    "Tottenham": "Tottenham Hotspur", "West Ham": "West Ham United",
    "Wolves": "Wolverhampton Wanderers",
}


def _load_fd(as_of: pd.Timestamp | None) -> tuple[pd.DataFrame, pd.Timestamp]:
    """EPL мачове (голове + удари) от суровите football-data CSV-та,
    5 сезона (2021-22 → 2025-26)."""
    cols = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "HS", "AS"]
    frames = []
    for f in sorted(FD_RAW_DIR.glob("E0_*.csv")):
        df = pd.read_csv(f, encoding="utf-8-sig")
        frames.append(df[[c for c in cols if c in df.columns]].copy())
    fd = pd.concat(frames, ignore_index=True)
    fd["Date"] = pd.to_datetime(fd["Date"], dayfirst=True)
    fd = fd.dropna(subset=["FTHG", "FTAG"]).sort_values("Date").reset_index(drop=True)
    fd["HomeTeam"] = fd["HomeTeam"].map(lambda t: FD_TO_OPTA.get(t, t))
    fd["AwayTeam"] = fd["AwayTeam"].map(lambda t: FD_TO_OPTA.get(t, t))
    if as_of is None:
        as_of = pd.Timestamp.today()
    fd = fd[fd["Date"] < as_of]
    fd["w"] = np.exp(-XI * (as_of - fd["Date"]).dt.days.clip(lower=0))
    return fd, as_of


def _build_directed_h2h(fd: pd.DataFrame, home_col: str, away_col: str,
                        k: float, cap: tuple[float, float]) -> dict:
    """{(team, opp): {"mult", "n", "n_eff"}} — насочен credibility-blend
    множител по произволна статистика (голове или удари)."""
    long = pd.concat([
        fd.rename(columns={"HomeTeam": "team", "AwayTeam": "opp", home_col: "v"})
          [["Date", "team", "opp", "v", "w"]],
        fd.rename(columns={"AwayTeam": "team", "HomeTeam": "opp", away_col: "v"})
          [["Date", "team", "opp", "v", "w"]],
    ])
    base = long.groupby("team").apply(
        lambda g: np.average(g["v"], weights=g["w"]), include_groups=False)

    lo, hi = cap
    out = {}
    for (team, opp), g in long.groupby(["team", "opp"]):
        n_eff = g["w"].sum()
        b = base.get(team, 0)
        if n_eff <= 0 or b <= 0:
            continue
        blend = ((g["v"] * g["w"]).sum() + k * b) / (n_eff + k)
        mult = float(np.clip(blend / b, lo, hi))
        out[(team, opp)] = {"mult": mult, "n": len(g), "n_eff": round(n_eff, 2)}
    return out


def build_h2h(as_of: pd.Timestamp | None = None) -> dict:
    """{(home, away): {"mult_home", "mult_away", "n"}} — голове, за всички
    OPTA двойки."""
    fd, as_of = _load_fd(as_of)
    return _build_directed_h2h(fd, "FTHG", "FTAG", K, CAP)


def build_h2h_shots(as_of: pd.Timestamp | None = None) -> dict:
    """{(home, away): {"mult_home", "mult_away", "n"}} — удари (HS/AS от
    football-data), огледално на build_h2h, но за удари вместо голове."""
    fd, as_of = _load_fd(as_of)
    return _build_directed_h2h(fd, "HS", "AS", K_SHOTS, SHOTS_CAP)


def h2h_for_match(h2h: dict, home: str, away: str) -> dict:
    """Множители за конкретен мач; 1.0 при липса на история."""
    h = h2h.get((home, away))
    a = h2h.get((away, home))
    return {"mult_home": h["mult"] if h else 1.0,
            "mult_away": a["mult"] if a else 1.0,
            "n": h["n"] if h else 0}


def build_h2h_markets(as_of: pd.Timestamp | None = None) -> dict:
    """{frozenset({A,B}): {"btts_rate", "over25_rate", "n", "n_eff"}} —
    ненасочено (не зависи кой е домакин в историята), за BTTS/Над-Под смесване."""
    fd, as_of = _load_fd(as_of)
    fd["btts"] = (fd["FTHG"] > 0) & (fd["FTAG"] > 0)
    fd["over25"] = (fd["FTHG"] + fd["FTAG"]) > 2.5
    fd["pair"] = list(zip(fd["HomeTeam"], fd["AwayTeam"]))
    fd["pair"] = fd["pair"].map(lambda p: tuple(sorted(p)))

    out = {}
    for pair, g in fd.groupby("pair"):
        n_eff = g["w"].sum()
        if n_eff <= 0:
            continue
        out[pair] = {
            "btts_rate": float(np.average(g["btts"], weights=g["w"])),
            "over25_rate": float(np.average(g["over25"], weights=g["w"])),
            "n": len(g), "n_eff": round(float(n_eff), 2)}
    return out


def h2h_market_for_match(markets: dict, home: str, away: str) -> dict | None:
    return markets.get(tuple(sorted((home, away))))


def blend_market(model_p: float, mkt: dict | None, key: str,
                 k: float = K_MARKET) -> float:
    """Смесва моделната вероятност с историческата H2H честота (без история
    → връща моделната стойност непроменена)."""
    if not mkt:
        return model_p
    n_eff = mkt["n_eff"]
    return (n_eff * mkt[key] + k * model_p) / (n_eff + k)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    h2h = build_h2h()
    h2h_s = build_h2h_shots()
    markets = build_h2h_markets()
    print(f"Двойки с голова история: {len(h2h)}, с удари: {len(h2h_s)}, "
          f"с BTTS/O-U история: {len(markets)}")
    for pair in [("Newcastle United", "Arsenal"), ("Chelsea", "Manchester United"),
                 ("Leeds United", "Bournemouth"), ("Sunderland", "Aston Villa"),
                 ("Manchester City", "Liverpool")]:
        m = h2h_for_match(h2h, *pair)
        ms = h2h_for_match(h2h_s, *pair)
        mk = h2h_market_for_match(markets, *pair)
        mk_txt = (f"BTTS {mk['btts_rate']:.0%}, Над2.5 {mk['over25_rate']:.0%}"
                  if mk else "няма")
        print(f"  {pair[0]} vs {pair[1]}: голове ×{m['mult_home']:.3f}/"
              f"×{m['mult_away']:.3f}, удари ×{ms['mult_home']:.3f}/"
              f"×{ms['mult_away']:.3f} ({m['n']} срещи) — {mk_txt}")
