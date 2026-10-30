"""Сверява записаните прогнози (model_predictions.db) срещу реалните резултати,
след като fetch_sofa.py + parse_sofa_api.py са опреснили лиговите бази.

Резолва по sofa_event_id (matches.sofa_event_id) - по-точно от name+date
matching (виж resolve_picks.py), защото next_fixtures.py вече ни дава точния
Sofascore event id при записването (record_predictions.py).

Употреба:
    python engine/resolve_predictions.py
"""
import sqlite3
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_predictions_log import connect, MODEL_LABELS

from config import DATA_DIR


def league_db_path(league: str) -> Path:
    return DATA_DIR / f"sofa_{league.lower()}.db"


def find_result(league: str, event_id: int, home: str | None = None,
                away: str | None = None) -> tuple[int, int] | None:
    """Резултат по sofa_event_id; ако го няма (мачовете, слети от FotMob след
    блока на Sofascore, имат sofa_event_id=NULL) - по отбори. В един сезонен файл
    двойката домакин-гост е уникална, така че (home, away) е еднозначно и покрива
    и отложени мачове, изиграни седмици по-късно (2026-10-16)."""
    db_path = league_db_path(league)
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT hg, ag FROM matches WHERE sofa_event_id=?", (event_id,)
        ).fetchone()
        if (row is None or row[0] is None) and home and away:
            rows = con.execute(
                "SELECT hg, ag FROM matches WHERE home=? AND away=? AND hg IS NOT NULL",
                (home, away)).fetchall()
            row = rows[0] if len(rows) == 1 else None
    finally:
        con.close()
    if row is None or row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


def _hit_1x2(pick: str, hg: int, ag: int) -> int:
    actual = "1" if hg > ag else ("2" if ag > hg else "X")
    return int(pick == actual)


def main() -> None:
    con = connect()
    pending = con.execute(
        "SELECT id, league, event_id, home, away, model, pick_1x2, pick_over25, pick_btts "
        "FROM model_predictions WHERE status='pending'"
    ).fetchall()

    if not pending:
        print("Няма чакащи прогнози.")
        return

    resolved = 0
    for pid, league, event_id, home, away, model, pick_1x2, pick_over25, pick_btts in pending:
        result = find_result(league, event_id, home, away)
        if result is None:
            continue
        hg, ag = result
        total_goals = hg + ag
        hit_1x2 = _hit_1x2(pick_1x2, hg, ag) if pick_1x2 else None
        hit_over25 = (int((total_goals > 2.5) == (pick_over25 == "OVER"))
                     if pick_over25 else None)
        hit_btts = (int(((hg > 0 and ag > 0)) == (pick_btts == "YES"))
                   if pick_btts else None)
        resolved_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        con.execute(
            """UPDATE model_predictions SET status='resolved', actual_hg=?, actual_ag=?,
               hit_1x2=?, hit_over25=?, hit_btts=?, resolved_at=? WHERE id=?""",
            (hg, ag, hit_1x2, hit_over25, hit_btts, resolved_at, pid),
        )
        resolved += 1
        mark = "✅" if hit_1x2 else ("—" if hit_1x2 is None else "❌")
        print(f"{league} | {home} {hg}-{ag} {away} | {MODEL_LABELS.get(model, model):<26} "
             f"1X2={pick_1x2 or '—'} {mark}")

    con.commit()

    print(f"\nРазрешени сега: {resolved}")
    if resolved == 0:
        print("Всички чакащи прогнози са за мачове, които още не са завършили.")
        return

    print("\n── Точност по модел (всички някога разрешени прогнози) ──")
    # ПОПРАВКА (2026-09-04): 1X2 знаменателят беше COUNT(*) — ВСИЧКИ разрешени
    # редове на модела, включително тези без 1X2 pick (hit_1x2 IS NULL). Тогава
    # модел, който понякога няма 1X2 прогноза, излизаше по-слаб отколкото е.
    # Сега е симетрично на Над2.5/ГГ редовете, които открай време броят само
    # редовете с реален pick.
    rows = con.execute(
        "SELECT model, "
        "SUM(CASE WHEN pick_1x2 IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(hit_1x2), SUM(hit_over25), SUM(hit_btts), "
        "SUM(CASE WHEN pick_over25 IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN pick_btts IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM model_predictions WHERE status='resolved' GROUP BY model"
    ).fetchall()
    con.close()
    for model, n, hit1x2, hit_ou, hit_bt, n_ou, n_bt in rows:
        label = MODEL_LABELS.get(model, model)
        line = (f"  {label:<26} 1X2: {hit1x2 or 0}/{n} ({(hit1x2 or 0)/n:.1%})"
                if n else f"  {label:<26} 1X2: — (няма 1X2 прогнози)")
        if n_ou:
            line += f"   Над2.5: {hit_ou or 0}/{n_ou} ({(hit_ou or 0)/n_ou:.1%})"
        if n_bt:
            line += f"   ГГ: {hit_bt or 0}/{n_bt} ({(hit_bt or 0)/n_bt:.1%})"
        print(line)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
