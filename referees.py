"""Статистика на съдиите (жълти/червени картони) от Sofascore базите.

Съдията идва от блока "Referee" в clipping-а (виж parse_sofa.py); картоните
са реалните отборни суми от stats(period='full') за същия мач — не средната
стойност, която Sofascore показва за кариерата на съдията.

Употреба:  python engine/referees.py [--league EPL|Bundesliga|Ligue1] [--db ...]
           [--min-matches 3]
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from config import DATA_DIR

DB_DIR = DATA_DIR


def _db_for(league: str) -> Path:
    return DB_DIR / "sofa.db" if league.upper() == "EPL" else DB_DIR / f"sofa_{league.lower()}.db"


def referee_table(con) -> list[dict]:
    rows = con.execute("""
        SELECT m.referee, m.match_id,
               COALESCE(SUM(s.yellows), 0) AS y,
               COALESCE(SUM(s.reds), 0) AS r
        FROM matches m JOIN stats s
          ON s.match_id = m.match_id AND s.period = 'full'
        WHERE m.referee IS NOT NULL
        GROUP BY m.match_id
    """).fetchall()

    agg: dict[str, dict] = {}
    for referee, match_id, y, r in rows:
        a = agg.setdefault(referee, {"matches": 0, "yellows": 0.0, "reds": 0.0})
        a["matches"] += 1
        a["yellows"] += y
        a["reds"] += r

    out = []
    for referee, a in agg.items():
        n = a["matches"]
        out.append({
            "referee": referee, "matches": n,
            "yellows": a["yellows"], "reds": a["reds"],
            "cards_per_match": (a["yellows"] + a["reds"]) / n,
            "yellows_per_match": a["yellows"] / n,
            "reds_per_match": a["reds"] / n,
        })
    return sorted(out, key=lambda r: -r["cards_per_match"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL")
    ap.add_argument("--db")
    ap.add_argument("--min-matches", type=int, default=3)
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else _db_for(args.league)
    con = sqlite3.connect(db_path)
    table = [r for r in referee_table(con) if r["matches"] >= args.min_matches]

    print(f"Съдии ({args.league}, ≥{args.min_matches} мача) — по картони/мач:\n")
    print(f"{'Съдия':<24}{'мачове':>7}{'ж.к./мач':>10}{'ч.к./мач':>10}{'общо/мач':>10}")
    for r in table:
        print(f"{r['referee']:<24}{r['matches']:>7}"
              f"{r['yellows_per_match']:>10.2f}{r['reds_per_match']:>10.2f}"
              f"{r['cards_per_match']:>10.2f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
