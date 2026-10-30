"""Telegram съобщение с топ5 мача от току-що записания кръг - по искане на
потребителя (2026-08-26): "след всяко обновяване на лигите... топ5 мача с
най-голяма вероятност да излезат... топ5 за над... топ5 за г/г".

Класации:
  - 1X2: по DC-то max(p_home, p_draw, p_away) - "най-голяма вероятност да
    излезе" буквално.
  - Над2.5 / Г/Г: Модел Б НЕ пази числени вероятности в model_predictions.db
    (само категоричен pick - виж record_predictions.py._extract). ПРОВЕРЕНО
    (2026-08-26): нито DC, нито Модел Г, нито Opta-Elo пазят p_over25/p_btts
    в тази таблица - единствено v1/v2 ("Модел В") имат числа за тези пазари.
    Затова филтрираме по Модел Б-то pick (OVER/YES - неговите "реални"
    пикове за тези пазари), но подреждаме по v2-то ("Модел В v2 - атрибути")
    числена вероятност - избран пред v1 заради по-широко текущо покритие
    (виж SELECT model, COUNT(p_over25) ... GROUP BY model, ако искаш да
    провериш пак по-нататък в сезона; може да си струва v1/v2 да се усреднят
    вместо само v2, ако покритието на v1 се изравни).

"От този кръг" = само редове с recorded_at >= --since (подадено от
update_all.py като timestamp-а точно преди record_predictions.py да се
пусне) - INSERT OR IGNORE в record_predictions.py значи вече записани мачове
пазят стария си recorded_at, само НОВИТЕ от този пуск получават пресен.

Ползва СЪЩИЯ Telegram бот/чат като pl-injury-bot. Токенът/chat id се четат
от env vars, по избор през .env в root на проекта (виж .env.example).

Употреба:
    python engine/top_picks_alert.py --since 2026-08-26T12:00:00+00:00
"""
from __future__ import annotations

import argparse
import html
import os
import sqlite3
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_team_report import LEAGUES as REPORT_LEAGUES, agreed_1x2_rows, agreed_combo_rows, CACHE_DIR

from config import DATA_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
DB_PATH = DATA_DIR / "model_predictions.db"
TOP_N = 5
MIN_ROUNDS = 5  # по искане на потребителя (2026-08-26): "топ5 само на лиги
# който вече има 5 минали кръга" — новостартирали лиги (EPL/Ligue1/SerieA
# на 1 кръг към момента на добавяне) нямат достатъчно история моделите да
# им се доверява, затова се пропускат в Telegram-а, докато настигнат.


def esc(text) -> str:
    return html.escape(str(text))


def send_telegram(text: str) -> bool:
    token = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("ГРЕШКА: липсват TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (.env или env var).", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Telegram грешка: {resp.status_code} {resp.text}", file=sys.stderr)
            return False
        return True
    except requests.RequestException as exc:
        print(f"Telegram изключение: {exc}", file=sys.stderr)
        return False


def leagues_with_min_rounds(min_rounds: int = MIN_ROUNDS) -> set[str]:
    """Имена на лиги (както се пазят в model_predictions.league) с поне
    `min_rounds` РЕАЛНО изиграни кръга в локалната matches таблица - виж
    MIN_ROUNDS докстринга по-горе.

    ПОПРАВКА (2026-09-04): броеше се MAX(round), т.е. най-високият НОМЕР на
    кръг, не броят изиграни. Лигите пренареждат кръгове (наваксване на
    отложени - реален случай: Allsvenskan играе кръг 20 на 07.09, докато
    кръг 10 е на 03.09), затова един-единствен мач с висок номер вдигаше
    MAX(round) над прага. Конкретно: LaLiga имаше MAX(round)=6 при само 4
    реално изиграни кръга (31 мача) и влизаше в Telegram топ5, без да е
    натрупала историята, заради която прагът изобщо съществува."""
    qualifying: set[str] = set()
    for league, db_path in REPORT_LEAGUES:
        if not db_path.exists():
            continue
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT COUNT(DISTINCT round) FROM matches WHERE round IS NOT NULL").fetchone()
        finally:
            con.close()
        if row and row[0] is not None and row[0] >= min_rounds:
            qualifying.add(league)
    return qualifying


def top_1x2(con: sqlite3.Connection, since: str, leagues: set[str]) -> list[tuple]:
    if not leagues:
        return []
    placeholders = ",".join("?" * len(leagues))
    return con.execute(
        f"""SELECT league, home, away, match_date, p_home, p_draw, p_away
           FROM model_predictions
           WHERE model = 'dc' AND recorded_at >= ?
             AND p_home IS NOT NULL AND p_draw IS NOT NULL AND p_away IS NOT NULL
             AND league IN ({placeholders})
           ORDER BY MAX(p_home, p_draw, p_away) DESC
           LIMIT ?""",
        (since, *leagues, TOP_N),
    ).fetchall()


# 2026-10-26: сменено от "v2" на "v1". v2 (атрибути на играчи от Sofascore) не се записва от 18.09
# (Sofascore е блокиран) и е скрит от сайта -> с него класациите за Над2.5/Г-Г излизаха празни.
# v1 (xg/xa от състава) има p_over25/p_btts и работи през FotMob състави; връщай "v2" само ако
# атрибутите се върнат (виж SHOW_V2 в make_team_report.py).
PROB_SOURCE_MODEL = "v1"


def top_market(con: sqlite3.Connection, since: str, prob_col: str, pick_col: str, pick_value: str,
               leagues: set[str]) -> list[tuple]:
    if not leagues:
        return []
    # prob_col/pick_col идват от фиксиран вътрешен избор (не user input) - безопасно за f-string в SQL.
    placeholders = ",".join("?" * len(leagues))
    query = f"""
        SELECT pm.league, pm.home, pm.away, pm.match_date, pm.{prob_col}
        FROM model_predictions pm
        JOIN model_predictions mb
          ON mb.event_id = pm.event_id AND mb.model = 'model_b'
        WHERE pm.model = ? AND pm.recorded_at >= ?
          AND pm.{prob_col} IS NOT NULL AND mb.{pick_col} = ?
          AND pm.league IN ({placeholders})
        ORDER BY pm.{prob_col} DESC
        LIMIT ?
    """
    return con.execute(query, (PROB_SOURCE_MODEL, since, pick_value, *leagues, TOP_N)).fetchall()


def _fmt_1x2_row(i: int, row: tuple) -> str:
    league, home, away, date, p_home, p_draw, p_away = row
    probs = {"1": p_home, "X": p_draw, "2": p_away}
    sign = max(probs, key=probs.get)
    return f"{i}. {esc(home)} - {esc(away)} ({esc(league)}) — <b>{sign}</b> {probs[sign] * 100:.0f}%"


def _fmt_agreed_row(i: int, row: tuple) -> str:
    league, home, away, date, sign, p_dc, p_el, p_avg = row
    return (f"{i}. {esc(home)} - {esc(away)} ({esc(league)}) — <b>{sign}</b> {p_avg * 100:.0f}% "
            f"(DC {p_dc * 100:.0f}% · Elo {p_el * 100:.0f}%)")


def _fmt_market_row(i: int, row: tuple) -> str:
    league, home, away, date, p = row
    return f"{i}. {esc(home)} - {esc(away)} ({esc(league)}) — {p * 100:.0f}%"


def build_message(con: sqlite3.Connection, since: str) -> str | None:
    leagues = leagues_with_min_rounds()
    # 1X2: само мачове, при които Dixon-Coles И Opta-Elo са съгласни за знака (2026-10-26)
    rows_1x2 = agreed_1x2_rows(con, leagues, "d.recorded_at >= ?", (since,))
    # Знак и Над2.5 / знак и Г-Г (2026-10-26): от пресметнатите данни на отчета (report_cache/*.pkl,
    # пишат се от make_team_report преди това съобщение в update_fotmob.py), не от model_predictions.db
    rows_so, rows_sb = [], []
    try:
        import pickle
        league_data = {}
        for f in CACHE_DIR.glob("*.pkl"):
            league_data[f.stem] = pickle.loads(f.read_bytes())["data"]
        rows_so = agreed_combo_rows(league_data, leagues, "over25")
        rows_sb = agreed_combo_rows(league_data, leagues, "btts")
    except Exception as exc:
        print(f"⚠ комбинациите знак+Над2.5/Г-Г са пропуснати: {exc}", file=sys.stderr)
    rows_over = top_market(con, since, "p_over25", "pick_over25", "OVER", leagues)
    rows_btts = top_market(con, since, "p_btts", "pick_btts", "YES", leagues)

    if not rows_1x2 and not rows_over and not rows_btts and not rows_so and not rows_sb:
        return None

    parts = ["📊 <b>Топ прогнози за новия кръг</b>"]

    if rows_1x2:
        parts.append(f"\n🎯 <b>Топ{len(rows_1x2)} 1X2 (DC + Elo съгласни)</b>")
        parts += [_fmt_agreed_row(i, r) for i, r in enumerate(rows_1x2, 1)]
    else:
        parts.append("\n🎯 <b>1X2</b> — няма мачове, на които DC и Elo са съгласни този кръг")

    if rows_over:
        parts.append(f"\n⚽ <b>Топ{len(rows_over)} Над 2.5</b> (Модел Б съгласен)")
        parts += [_fmt_market_row(i, r) for i, r in enumerate(rows_over, 1)]
    else:
        parts.append("\n⚽ <b>Над 2.5</b> — няма мачове с убеден Модел Б този кръг")

    if rows_btts:
        parts.append(f"\n🤝 <b>Топ{len(rows_btts)} Г/Г</b> (Модел Б съгласен)")
        parts += [_fmt_market_row(i, r) for i, r in enumerate(rows_btts, 1)]
    else:
        parts.append("\n🤝 <b>Г/Г</b> — няма мачове с убеден Модел Б този кръг")

    for title, rows in (("🎯⚽ <b>Знак и Над 2.5</b> (DC + Elo съгласни за знака)", rows_so),
                        ("🎯🤝 <b>Знак и Г/Г</b> (DC + Elo съгласни за знака)", rows_sb)):
        if rows:
            parts.append("\n" + title)
            parts += [f"{i}. {esc(h)} - {esc(a)} ({esc(lg)}) — <b>{sg}</b> {p * 100:.0f}% ({src})"
                      for i, (lg, h, a, d, sg, p, pd_, pe_, src) in enumerate(rows, 1)]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True,
                        help="ISO timestamp (UTC) - само записи с recorded_at >= това")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Няма {DB_PATH} - пропускам.", file=sys.stderr)
        return 0

    con = sqlite3.connect(DB_PATH)
    try:
        message = build_message(con, args.since)
    finally:
        con.close()

    if message is None:
        print("Няма нови записи от последното обновяване - пропускам Telegram съобщението.")
        return 0

    ok = send_telegram(message)
    print("Изпратено успешно." if ok else "Неуспешно изпращане.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
