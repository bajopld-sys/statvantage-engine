"""Симулация от Sofascore профили (profiles_sofa.py) — реизползва
score_matrix/match_markets/RHO от dixon_coles.py (споделена математика,
без Opta зависимости — виж dixon_coles.py докстринга). Два паралелни
модела, без lineup корекции (sofa.db е на ниво отбор):

  simulate_xg      — λ от own_xg/xg_conceded
  simulate_threat  — λ от threat_for/against (SoT+big_chances+корнери)

H2H реизползва h2h.py директно (football-data източник, независим от Опта/
Sofascore базите — вече универсален сигнал).

Употреба:  python engine/simulate_sofa.py --home "Arsenal" --away "Chelsea"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dixon_coles import (score_matrix, match_markets, RHO, fair, rho_for_league,
                         calibrate_lambda_mu)
from h2h import (build_h2h, h2h_for_match, build_h2h_markets,
                 h2h_market_for_match, blend_market, build_h2h_shots)
from profiles_sofa import build_profiles, _connect
from lineup_impact import lineup_factors, lineup_factors_from_starters, fetch_missing_players, fetch_lineup_starters


def _apply_lineup_factors(lam, mu, con, home, away, as_of, event_id):
    """lam/mu умножени по ефекта от състава (виж lineup_impact.py).
    Предпочита lineup_factors_from_starters (прогнозен/потвърден 11-членен
    състав — по-прецизен, хваща и качеството на заместника), пада обратно
    на lineup_factors (само списък отсъстващи) ако Sofascore още няма
    никакъв състав за мача. No-op (1.0 навсякъде), ако event_id е None или
    и двата опита не върнат данни."""
    if event_id is None:
        return lam, mu, None

    home_starters, away_starters, confirmed = fetch_lineup_starters(event_id)
    if home_starters and away_starters:
        fh = lineup_factors_from_starters(con, home, as_of, home_starters)
        fa = lineup_factors_from_starters(con, away, as_of, away_starters)
        lam *= fh["attack"] / fa["defense"]
        mu *= fa["attack"] / fh["defense"]
        return lam, mu, {"home": fh, "away": fa, "method": "starters", "confirmed": confirmed}

    miss_home, miss_away = fetch_missing_players(event_id)
    if not miss_home and not miss_away:
        return lam, mu, None
    fh = lineup_factors(con, home, as_of, miss_home) if miss_home else {"attack": 1.0, "defense": 1.0}
    fa = lineup_factors(con, away, as_of, miss_away) if miss_away else {"attack": 1.0, "defense": 1.0}
    lam *= fh["attack"] / fa["defense"]
    mu *= fa["attack"] / fh["defense"]
    return lam, mu, {"home": fh, "away": fa, "method": "missing",
                     "missing_home": miss_home, "missing_away": miss_away}

# 2026-07: пробвано team-level Над2.5/BTTS смесване (собствената сезонна
# честота на всеки отбор, огледално на h2h.blend_market) — мотивирано от
# конкретен разминаващ се случай (Real Oviedo–Rayo Vallecano: модел 57/59%,
# реални честоти 25-33%). ОТХВЪРЛЕНО след 320-мачов walk-forward бектест:
# влошава Над2.5/BTTS точността (-0.6 до -4.4pp), подобрява само визуално
# крайни случаи. Извод: opponent-adjusted xG сигналът е по-информативен от
# плоската собствена честота на повечето мачове — смесването разрежда
# по-добрия сигнал с по-слаб. Виж отчета (team_report.html) секция 4 за
# същите числа като контекст/сверка — там не пипат прогнозата.


def simulate_xg(prof: dict, home: str, away: str,
                 h2h: dict | None = None, h2h_markets: dict | None = None,
                 use_form: bool = True, con=None, as_of=None, event_id: int | None = None,
                 league: str | None = None) -> dict:
    """league (2026-09-08) избира калибрираното за тази лига rho — виж
    dixon_coles.rho_for_league(). None => pooled/константа, както преди."""
    t, lg = prof["teams"], prof["league"]
    th, ta = t.loc[home], t.loc[away]

    # ПОПРАВКА (2026-07): home_adv преди се прилагаше само върху lam, без
    # компенсация в mu — за "средна" среща (own_xg=xg_conceded=avg_xg за
    # двата отбора) сборът lam+mu излизаше avg_xg*(home_adv+1) вместо
    # истинската лигова средна avg_xg*2, систематично завишавайки Над2.5/ГГ
    # с ~(home_adv-1)/2 за ВСЕКИ мач. Виж simulate_lineup_v1/v2.py за същия
    # фикс, приложен там първо. Съотношението lam/mu=home_adv за средна среща
    # се запазва, само сборът вече е закотвен към реалната средна.
    home_mult = 2 * lg["home_adv"] / (1 + lg["home_adv"])
    away_mult = 2 / (1 + lg["home_adv"])

    lam = th["own_xg"] * (ta["xg_conceded"] / lg["avg_xg"]) * home_mult
    mu = ta["own_xg"] * (th["xg_conceded"] / lg["avg_xg"]) * away_mult

    hh = h2h_for_match(h2h, home, away) if h2h else None
    if hh:
        lam *= hh["mult_home"]
        mu *= hh["mult_away"]

    res_form = None
    if use_form:
        res_form = {"att_home": th["att_form"], "att_away": ta["att_form"],
                    "def_home": th["def_form"], "def_away": ta["def_form"]}
        lam *= res_form["att_home"] / res_form["def_away"]
        mu *= res_form["att_away"] / res_form["def_home"]
        # рейтингова форма — независим сигнал (субективно представяне,
        # не изчислено от xG), последните 5 оценени мача vs сезонния среден
        lam *= th["rating_form"]
        mu *= ta["rating_form"]

    lineup = None
    if con is not None and as_of is not None:
        lam, mu, lineup = _apply_lineup_factors(lam, mu, con, home, away, as_of, event_id)

    # Рекалибрация на λ/μ — ПОСЛЕДНА стъпка, след H2H/форма/lineup корекциите
    # (те всички раздуват дисперсията; калибрацията я свива обратно към
    # реалната). Виж dixon_coles.calibrate_lambda_mu().
    lam_raw, mu_raw = lam, mu
    lam, mu = calibrate_lambda_mu(lam, mu, league)
    res = {"lam": lam, "mu": mu, "lam_raw": lam_raw, "mu_raw": mu_raw,
          "h2h": hh, "form": res_form, "lineup": lineup,
          "rho": rho_for_league(league), **match_markets(lam, mu, rho=rho_for_league(league))}

    mkt = h2h_market_for_match(h2h_markets, home, away) if h2h_markets else None
    if mkt:
        res["p_btts"] = blend_market(res["p_btts"], mkt, "btts_rate")
        res["p_over25"] = blend_market(res["p_over25"], mkt, "over25_rate")
        res["p_under25"] = 1 - res["p_over25"]
    return res


def simulate_threat(prof: dict, home: str, away: str,
                     h2h_shots: dict | None = None, use_form: bool = True,
                     con=None, as_of=None, event_id: int | None = None,
                     league: str | None = None) -> dict:
    t, lg = prof["teams"], prof["league"]
    th, ta = t.loc[home], t.loc[away]
    conv = lg["goals_per_threat"]

    # ПОПРАВКА (2026-07): виж бележката в simulate_xg() — threat_home_adv само
    # върху lam изкривяваше сбора над истинската лигова средна за всеки мач.
    home_mult = 2 * lg["threat_home_adv"] / (1 + lg["threat_home_adv"])
    away_mult = 2 / (1 + lg["threat_home_adv"])

    lam = th["threat_for"] * (ta["threat_against"] / lg["avg_threat"]) * home_mult * conv
    mu = ta["threat_for"] * (th["threat_against"] / lg["avg_threat"]) * away_mult * conv

    hhs = h2h_for_match(h2h_shots, home, away) if h2h_shots else None
    if hhs:
        lam *= hhs["mult_home"]
        mu *= hhs["mult_away"]

    res_form = None
    if use_form:
        res_form = {"att_home": th["threat_att_form"], "att_away": ta["threat_att_form"],
                    "def_home": th["threat_def_form"], "def_away": ta["threat_def_form"]}
        lam *= res_form["att_home"] / res_form["def_away"]
        mu *= res_form["att_away"] / res_form["def_home"]
        lam *= th["rating_form"]
        mu *= ta["rating_form"]

    lineup = None
    if con is not None and as_of is not None:
        lam, mu, lineup = _apply_lineup_factors(lam, mu, con, home, away, as_of, event_id)

    return {"lam": lam, "mu": mu, "h2h_shots": hhs, "form": res_form, "lineup": lineup,
            "rho": rho_for_league(league), **match_markets(lam, mu, rho=rho_for_league(league))}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--no-h2h", action="store_true")
    ap.add_argument("--no-form", action="store_true")
    ap.add_argument("--db")
    args = ap.parse_args()

    con = _connect(Path(args.db)) if args.db else _connect()
    prof = build_profiles(con)
    use_form = not args.no_form

    h2h = h2h_markets = h2h_shots = None
    if not args.no_h2h:
        as_of = prof["league"]["as_of"]
        h2h = build_h2h(as_of)
        h2h_markets = build_h2h_markets(as_of)
        h2h_shots = build_h2h_shots(as_of)

    sx = simulate_xg(prof, args.home, args.away, h2h, h2h_markets, use_form)
    st = simulate_threat(prof, args.home, args.away, h2h_shots, use_form)

    print(f"{args.home} vs {args.away}  (Sofascore)\n")
    print(f"xG-модел:      λ {sx['lam']:.2f}-{sx['mu']:.2f}   "
          f"1X2 {sx['p_home']*100:.0f}/{sx['p_draw']*100:.0f}/{sx['p_away']*100:.0f}%   "
          f"Над2.5 {sx['p_over25']*100:.0f}%   BTTS {sx['p_btts']*100:.0f}%")
    print(f"threat-модел:  λ {st['lam']:.2f}-{st['mu']:.2f}   "
          f"1X2 {st['p_home']*100:.0f}/{st['p_draw']*100:.0f}/{st['p_away']*100:.0f}%   "
          f"Над2.5 {st['p_over25']*100:.0f}%   BTTS {st['p_btts']*100:.0f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
