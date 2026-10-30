"""Генерира публичната track-record страница (track_record.html) от
picks_ledger.db — ВСИЧКИ picks, включително загубите (виж позиционирането:
анти-tipster прозрачност, не подбрани печеливши примери).

Отделен файл от team_report.html - НЕ се публикува автоматично през
update_all.py/publish() засега (това е бъдещата платена продуктова страница,
не сегашния безплатен отчет). Пусни ръчно след resolve_picks.py:

    python engine/make_track_record.py

Полето `source` от ledger-а НЕ се показва тук - вътрешна бележка, не за
публично показване (виж picks_ledger.py докстринга).
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import TRACK_RECORD_PATH
from picks_ledger import DB_PATH, connect, unit_result

OUT_PATH = TRACK_RECORD_PATH

STATUS_LABEL = {"won": "Won", "lost": "Lost", "pending": "Pending", "void": "Void"}


def load_picks() -> list[dict]:
    con = connect()
    rows = con.execute(
        """SELECT published_at, league, home, away, market, selection, odds,
                  stake_units, status, result_score
           FROM picks ORDER BY published_at ASC"""
    ).fetchall()
    con.close()
    picks = []
    for published_at, league, home, away, market, selection, odds, stake, status, result_score in rows:
        units = unit_result(status, odds, stake) if status in ("won", "lost") else None
        picks.append({
            "published_at": published_at, "league": league, "home": home, "away": away,
            "market": market, "selection": selection, "odds": odds, "stake": stake,
            "status": status, "result_score": result_score, "units": units,
        })
    return picks


def sparkline_svg(cumulative: list[float], width: int = 640, height: int = 120) -> str:
    if len(cumulative) < 2:
        return ""
    lo, hi = min(cumulative), max(cumulative)
    span = (hi - lo) or 1.0
    pad = 8
    n = len(cumulative)
    points = []
    for i, v in enumerate(cumulative):
        x = pad + (width - 2 * pad) * i / (n - 1)
        y = height - pad - (height - 2 * pad) * (v - lo) / span
        points.append(f"{x:.1f},{y:.1f}")
    line = " ".join(points)
    zero_y = height - pad - (height - 2 * pad) * (0 - lo) / span
    zero_y = max(pad, min(height - pad, zero_y))
    fill_points = f"{pad:.1f},{height - pad:.1f} " + line + f" {width - pad:.1f},{height - pad:.1f}"
    return f"""
    <svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none">
      <line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}" class="spark-zero" />
      <polygon points="{fill_points}" class="spark-fill" />
      <polyline points="{line}" class="spark-line" />
    </svg>"""


def render(picks: list[dict]) -> str:
    resolved = [p for p in picks if p["units"] is not None]
    wins = sum(1 for p in resolved if p["status"] == "won")
    win_rate = (wins / len(resolved) * 100) if resolved else 0.0
    net_units = sum(p["units"] for p in resolved)

    cumulative = []
    running = 0.0
    for p in resolved:
        running += p["units"]
        cumulative.append(running)
    chart = sparkline_svg(cumulative) if cumulative else ""

    rows_html = []
    for p in reversed(picks):  # newest first for display
        status_class = {"won": "st-won", "lost": "st-lost", "pending": "st-pending", "void": "st-void"}[p["status"]]
        units_txt = f"{p['units']:+.2f}u" if p["units"] is not None else "—"
        score_txt = p["result_score"] or "—"
        odds_txt = f"{p['odds']:.2f}" if p["odds"] else "—"
        rows_html.append(f"""
        <tr>
          <td class="tleft mono">{p['published_at'][:16].replace('T', ' ')}</td>
          <td class="tleft">{p['league']}</td>
          <td class="tleft">{p['home']} – {p['away']}</td>
          <td class="tleft">{p['market']} {p['selection']}</td>
          <td class="mono">{odds_txt}</td>
          <td class="mono">{score_txt}</td>
          <td><span class="pill {status_class}">{STATUS_LABEL[p['status']]}</span></td>
          <td class="mono {'pos' if (p['units'] or 0) > 0 else 'neg' if (p['units'] or 0) < 0 else ''}">{units_txt}</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StatsVantage — Track Record</title>
<style>
  :root {{
    --page: #0a0a0b; --surface: #17181a; --surface-2: #1e2023;
    --ink: #f5f5f2; --ink-2: #c3c2b7; --ink-muted: #8b8a85;
    --grid-line: #2a2c2f; --border: rgba(255,255,255,0.08);
    --good: #22c55e; --critical: #f0605f; --pending: #8b8a85;
    --accent: #4f9dff; --accent-2: #7c6bff;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.55);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: radial-gradient(900px 500px at 15% -10%, rgba(79,157,255,0.10), transparent 60%), var(--page);
    color: var(--ink); margin: 0; padding: 32px clamp(16px, 4vw, 40px) 70px;
    max-width: 1100px; margin-inline: auto; line-height: 1.5;
  }}
  h1 {{
    font-size: 26px; font-weight: 750; letter-spacing: -.3px; margin: 0 0 6px;
    background: linear-gradient(100deg, var(--ink) 35%, var(--accent) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  .sub {{ color: var(--ink-2); font-size: 14.5px; max-width: 68ch; margin: 0 0 28px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }}
  .stat .label {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: .5px; color: var(--ink-muted); margin-bottom: 6px; }}
  .stat .value {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px 24px; margin-bottom: 20px; box-shadow: var(--shadow); }}
  .spark {{ width: 100%; height: 120px; display: block; }}
  .spark-line {{ fill: none; stroke: var(--accent); stroke-width: 2; }}
  .spark-fill {{ fill: rgba(79,157,255,0.12); stroke: none; }}
  .spark-zero {{ stroke: var(--grid-line); stroke-width: 1; stroke-dasharray: 3 3; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: center; border-bottom: 1px solid var(--grid-line); font-variant-numeric: tabular-nums; }}
  td.tleft, th.tleft {{ text-align: left; }}
  th {{ color: var(--ink-muted); font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: .5px; }}
  tbody tr:hover {{ background: rgba(79,157,255,0.06); }}
  .mono {{ font-family: ui-monospace, "SF Mono", Consolas, monospace; }}
  .pos {{ color: var(--good); }}
  .neg {{ color: var(--critical); }}
  .pill {{ display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 11px; font-weight: 700; }}
  .st-won {{ background: rgba(34,197,94,0.15); color: var(--good); }}
  .st-lost {{ background: rgba(240,96,95,0.15); color: var(--critical); }}
  .st-pending {{ background: rgba(139,138,133,0.15); color: var(--ink-muted); }}
  .st-void {{ background: rgba(139,138,133,0.15); color: var(--ink-muted); }}
  .disclaimer {{ color: var(--ink-muted); font-size: 12px; margin-top: 24px; line-height: 1.6; }}
  .overflow {{ overflow-x: auto; }}
</style>
</head>
<body>
  <h1>Track Record</h1>
  <p class="sub">Every featured pick we've ever published, timestamped before kickoff — wins and losses both, tracked in flat units, not currency.</p>

  <div class="stats">
    <div class="stat"><div class="label">Picks resolved</div><div class="value">{len(resolved)}</div></div>
    <div class="stat"><div class="label">Win rate</div><div class="value">{win_rate:.1f}%</div></div>
    <div class="stat"><div class="label">Net units</div><div class="value {'pos' if net_units > 0 else 'neg' if net_units < 0 else ''}">{net_units:+.2f}u</div></div>
    <div class="stat"><div class="label">Pending</div><div class="value">{len(picks) - len(resolved)}</div></div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 12px;font-size:11.5px;color:var(--ink-2);text-transform:uppercase;letter-spacing:.5px;">Cumulative units over time</h3>
    {chart or '<p class="sub" style="margin:0;">Not enough resolved picks yet for a chart.</p>'}
  </div>

  <div class="card overflow">
    <table>
      <thead><tr>
        <th class="tleft">Published (UTC)</th><th class="tleft">League</th><th class="tleft">Match</th>
        <th class="tleft">Pick</th><th>Odds</th><th>Score</th><th>Result</th><th>Units</th>
      </tr></thead>
      <tbody>{''.join(rows_html) if rows_html else '<tr><td colspan="8" class="tleft">No picks published yet.</td></tr>'}</tbody>
    </table>
  </div>

  <p class="disclaimer">18+. Statistical analysis only — not financial advice, not a guarantee of any outcome. Betting involves real financial risk. If gambling stops being fun, or you feel you can't control it, please seek help from a responsible-gambling support service in your country.</p>
</body>
</html>"""


def main() -> None:
    picks = load_picks()
    html = render(picks)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Записано: {OUT_PATH}  ({len(picks)} picks, от {DB_PATH})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
