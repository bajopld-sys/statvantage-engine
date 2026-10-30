"""Refreshes every league's data end-to-end, regenerates the report, and
publishes it to GitHub Pages - one command instead of running fetch_sofa.py ->
parse_sofa_api.py per league, then make_team_report.py, then copy+git push
by hand each time.

Also runs resolve_picks.py after parsing (2026-08-02) - auto-resolves any
pending picks in picks_ledger.db against the freshly-parsed match results,
before the report is regenerated.

Also runs resolve_predictions.py + record_predictions.py (2026-08-06) - a
SEPARATE, personal-analysis track record (model_predictions.db, distinct from
picks_ledger.db) of what EVERY model (DC, v1, v2, Модел Б, Модел Г) predicted
for EVERY fixture, not just curated "featured picks". resolve_predictions.py
runs BEFORE the report regenerates (settles last round against the
just-parsed results), record_predictions.py runs AFTER (snapshots the new
upcoming round's predictions) - see model_predictions_log.py docstring.

Also runs top_picks_alert.py right after record_predictions.py (2026-08-26) -
sends a Telegram message with top5 1X2 (by DC probability), top5 Над2.5 and
top5 Г/Г (both filtered to matches Model B agrees on, ranked by DC
probability - Model B itself stores no numeric probabilities) from the round
just recorded. Needs TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in a root .env (see
.env.example) or already-set env vars; silently skips (non-fatal, check=False)
if missing or if nothing new was recorded this run.

Per-league rho калибрация (2026-09-08) НЕ се пуска оттук — бектестът, който я
храни, е бавен (пълно walk-forward преизграждане на профилите за всяка дата).
Пуска се ръчно, периодично (напр. на 2-3 месеца, когато лигите са натрупали
достатъчно нови мачове):

    python engine/backtest_sofa.py --league <име>   # за всяка лига с история
    python engine/calibrate_rho.py                  # пише data/rho_calibration.csv
    python engine/calibrate_lambda.py               # пише data/lambda_calibration.csv
    python engine/calibrate_lambda.py --validate    # проверка извън извадката

Ако калибрационните CSV-та липсват, dixon_coles пада на
константата RHO — точно поведението отпреди въвеждането на per-league.

Usage:
    python update_all.py

Add a league by putting it in LEAGUES below - each entry needs the exact
Sofascore league name (matches --league elsewhere) and the season year fed
to fetch_sofa.py (see fetch_sofa.py --list-seasons if unsure).

Publishing (2026-08-01): copies team_report.html -> publish/index.html and
pushes it to https://github.com/bajopld-sys/StatVantage (public, GitHub
Pages, main branch/root) - the live report is at
https://bajopld-sys.github.io/StatVantage/. ONLY the report goes into
publish/ - never the raw scraped data/databases, those stay local. If there's
nothing new to publish (report unchanged since last push), the commit step is
a no-op, not an error.
"""
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LEAGUES = [
    # Eliteserien, Allsvenskan и LaLiga2 махнати от сайта по решение на
    # потребителя (2026-10-03) — данните остават локално, само не се теглят/
    # показват. Връщат се с разкоментиране (и в make_team_report.py LEAGUES).
    # ("Eliteserien", "2026"),
    # ("Allsvenskan", "2026"),
    ("Bundesliga", "26/27"),  # Германия 1во ниво — активирана 2026-10-16 (през FotMob)
    ("Bundesliga2", "26/27"),  # 2. Bundesliga (Германия) — добавена 2026-08-08, сезонът стартира
    ("Championship", "26/27"),  # Англия, 2ро ниво — добавена 2026-08-14, сезонът стартира днес
    ("BelgiumPL", "26/27"),  # Белгия, 1во ниво (Pro League) — добавена 2026-08-15
    ("LaLiga", "26/27"),  # Испания, 1во ниво — реактивирана 2026-08-16, чист файл за новия сезон
    # ("LaLiga2", "26/27"),  # Испания, 2ро ниво — добавена 2026-08-16, махната 2026-10-03
    ("EPL", "26/27"),  # Англия, 1во ниво — активирана 2026-08-24 (26/27 старт 21.08)
    ("Ligue1", "26/27"),  # Франция, 1во ниво — активирана 2026-08-24 (26/27 старт 21.08)
    ("SerieA", "26/27"),  # Италия, 1во ниво — добавена и активирана 2026-08-24
    # Bundesliga — конфигурирана в make_team_report.py (LEAGUE_COUNTRY/
    # LEAGUE_SOFA_IDS вече попълнени), НО умишлено оставена закоментирана и тук
    # (2026-08-24) — 26/27 сезонът стартира 28.08, все още не е започнал
    # (auto_upper_round връща 0 кръга, проверено чрез Sofascore). Разкоментирай
    # реда долу (и съответния в make_team_report.py LEAGUES) щом действително
    # тръгне — старият 25/26 файл вече е архивиран в data/archive/
    # (sofa_bundesliga_old-vault.db), чисто е за нов fetch.
    # ("Bundesliga", "26/27"),
    # Faroe премахната 2026-08-08 — виж бележката в make_team_report.py LEAGUES.
]

from config import DATA_DIR, PUBLISH_DIR, REPORT_PATH


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if check and result.returncode != 0:
        sys.exit(f"Command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def publish() -> None:
    print("\n===== Публикуване (GitHub Pages) =====")
    shutil.copyfile(REPORT_PATH, PUBLISH_DIR / "index.html")
    run(["git", "add", "index.html"], cwd=PUBLISH_DIR)
    commit = run(["git", "commit", "-m", "Auto-update report"], cwd=PUBLISH_DIR, check=False)
    if commit.returncode != 0:
        print("Няма промени за публикуване (отчетът е същият като последния път).")
        return
    run(["git", "push"], cwd=PUBLISH_DIR)
    print("Публикувано на https://bajopld-sys.github.io/StatVantage/")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", help="Обнови само тази лига (точното Sofascore име, напр. EPL) "
                                     "вместо всички от LEAGUES по-горе.")
    args = ap.parse_args()

    # Sofascore е блокиран и вече не се ползва (2026-10-26, решение на потребителя). Този
    # конвейер (fetch_sofa/parse_sofa_api) е изключен — обновяването е `engine/update_fotmob.py`.
    sys.exit("update_all.py е изключен: Sofascore не работи. Ползвай: python engine/update_fotmob.py [--record]")

    leagues = LEAGUES
    if args.league:
        leagues = [(lg, yr) for lg, yr in LEAGUES if lg.lower() == args.league.lower()]
        if not leagues:
            sys.exit(f"Непозната лига '{args.league}'. Известни: {', '.join(lg for lg, _ in LEAGUES)}")

    for league, year in leagues:
        print(f"\n===== {league} =====")
        run([sys.executable, "fetch_sofa.py", "--league", league, "--year", year,
             "--rounds", "auto", "--refresh"])
        run([sys.executable, "parse_sofa_api.py", "--league", league])
        # H2H (2026-08-16): забравяно при всяко добавяне на нова лига (4 поредни
        # пъти - Allsvenskan/Bundesliga2/Championship/BelgiumPL останаха без H2H
        # данни, защото тези два скрипта никога не бяха викани извън Eliteserien,
        # нито бяха свързани тук). Без --refresh нарочно - H2H историята рядко се
        # променя мач за мач, кешът прави повторните пускания бързи (почти изцяло
        # кеш-удари), само нови двойки/мачове реално дърпат нещо.
        db_path = str(DATA_DIR / f"sofa_{league.lower()}.db")
        run([sys.executable, "fetch_h2h.py", "--league", league, "--year", year,
             "--db", db_path])
        run([sys.executable, "fetch_h2h_matches.py", "--league", league, "--year", year,
             "--db", db_path])

    print("\n===== Разрешаване на чакащи picks =====")
    run([sys.executable, "resolve_picks.py"])

    print("\n===== Сверяване на записани прогнози на моделите =====")
    run([sys.executable, "resolve_predictions.py"])

    print("\n===== Опресняване на Opta-Elo рейтингите =====")
    run([sys.executable, "build_opta_elo.py"])

    print("\n===== Регенериране на team_report.html =====")
    run([sys.executable, "make_team_report.py"])

    print("\n===== Записване на прогнозите на моделите за новия кръг =====")
    since_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run([sys.executable, "record_predictions.py"])

    print("\n===== Топ5 Telegram съобщение =====")
    run([sys.executable, "top_picks_alert.py", "--since", since_ts], check=False)

    publish()
    print("\nГотово — всички лиги опреснени, отчетът е регенериран и публикуван.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
