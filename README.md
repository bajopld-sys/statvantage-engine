# StatsVantage engine

A multi-league football match-probability engine and static report generator.
It pulls fixtures, results and match statistics, fits several independent
goal models, compares them side by side, and publishes the result as a single
self-contained HTML page.

**Live report:** https://bajopld-sys.github.io/StatVantage/

> Statistical analysis only. Nothing here is betting advice or a guarantee of any outcome.
> The project is not affiliated with Opta, FotMob or Sofascore.
> Code comments and docstrings are mostly in Bulgarian.

## What it does

- Keeps a SQLite database per league (EPL, La Liga, Serie A, Ligue 1, Bundesliga, 2. Bundesliga,
  Championship, Belgian Pro League) with match results, team statistics, goal timelines and referees.
- Produces 1X2, Over/Under 2.5 and Both-Teams-To-Score probabilities for upcoming fixtures
  from several independent models, and shows where they agree.
- Snapshots every model's pre-match call into a prediction log, settles it after the match and
  reports the track record, so the models are judged on forward predictions rather than hindsight.
- Generates one HTML report with a client-side (JavaScript) simulator, head-to-head tables,
  form, half-time and referee statistics.
- Publishes the report to GitHub Pages and sends the day's top picks to Telegram.

## Architecture

```
 data sources           parsers / loaders            models                    outputs
 ────────────           ─────────────────            ──────                    ───────
 FotMob API   ───────►  fotmob_adapter.py  ───►  data/<league>.db  ───►  dixon_coles.py (Poisson + DC rho)
 (curl_cffi)            (incremental merge)                              model_b_heuristic.py (rank-based)
                                                                         model_g_scoring.py (100-point score)
 football-data CSVs ──► h2h.py                                           simulate_lineup_v1/v2.py (line-up based)
 (optional)                                                              build_opta_elo.py (Elo, xG component)
                                                                                    │
                                        model_predictions.db  ◄── record_predictions.py ◄─┘
                                                │  resolve_predictions.py (settle after the match)
                                                ▼
                                       make_team_report.py ──► team_report.html ──► publish/ (git push) ──► GitHub Pages
                                                │
                                       top_picks_alert.py ──► Telegram
```

| Stage | Files |
|---|---|
| Data acquisition | `fotmob_adapter.py`, `fotmob_lineups.py`, `next_fixtures.py` (legacy Sofascore path: `fetch_sofa.py`, `parse_sofa_api.py`, `lineup_impact.py`) |
| Models | `dixon_coles.py`, `simulate_sofa.py`, `model_b_heuristic.py`, `model_g_scoring.py`, `simulate_lineup_v1.py`, `simulate_lineup_v2.py`, `build_opta_elo.py` |
| Calibration / backtests | `calibrate_rho.py`, `calibrate_lambda.py`, `calibrate_alpha_v1.py`, `calibrate_model_g.py`, `backtest_sofa.py`, `backtest_lineup_v1.py` |
| Tracking | `model_predictions_log.py`, `record_predictions.py`, `resolve_predictions.py`, `picks_ledger.py`, `resolve_picks.py`, `make_track_record.py` |
| Report and delivery | `make_team_report.py`, `site_theme.css`, `top_picks_alert.py`, `update_fotmob.py` |

The core goal model is a Dixon-Coles-corrected Poisson score matrix; the low-score correlation
parameter (rho = -0.12) was fitted by maximum likelihood (`calibrate_rho.py`).

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/bajopld-sys/statvantage-engine.git
cd statvantage-engine
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit the paths / Telegram settings you need
```

All locations and secrets come from `.env` (see `config.py`); with no `.env` everything stays
inside the repository folder:

| Variable | Meaning | Default |
|---|---|---|
| `DATA_DIR` | SQLite databases, raw API caches, calibration CSVs | `./data` |
| `OUTPUT_DIR` | where `team_report.html` is written | `./output` |
| `PUBLISH_DIR` | separate git checkout served by GitHub Pages | `./publish` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional top-picks alert | unset |
| `VAULT_ROOT`, `FOOTBALL_DATA_RAW_DIR` | optional legacy inputs (Markdown clippings, football-data CSVs) | `./vault`, `./data/football_data_raw` |

## Run

```bash
python update_fotmob.py --no-publish          # fetch new results, rebuild the report locally
python update_fotmob.py                       # ... and publish to GitHub Pages
python update_fotmob.py --record              # also log the models' pre-match picks and send the Telegram top-5
python update_fotmob.py --rebuild             # ignore the per-league cache and recompute everything
```

`update_fotmob.py` runs, in order: FotMob merge → settle pending picks → settle logged predictions →
Elo rebuild → report → (optional) record picks and Telegram → publish.
The first run needs historical data in `DATA_DIR`; the databases are not included in this repository.

## Google Sheets arbitrage helper

`arb_calculator.html` is a small three-way arbitrage calculator embedded in the report.
Its optional "save to Google Sheets" button posts to a Google Apps Script web app
(`apps_script/arb_sheet.gs`). The script URL and a shared secret are never stored in the code:
they are entered once in the browser and kept in `localStorage`.

## Notes

- Sofascore access is disabled (blocked by Cloudflare); FotMob's unofficial endpoints are the data source.
  These endpoints are undocumented and can change without notice.
- `model_predictions.db` is append-only by design: a prediction is stored once, before kick-off.
