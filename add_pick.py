"""Добавя нов "featured pick" в публичния track record (picks_ledger.db).

Употреба:
    python engine/add_pick.py --league Eliteserien --home "SK Brann" --away "Rosenborg BK" \\
        --market 1X2 --selection Home --odds 1.85 --source "Model 1/2/3 съгласие"

    python engine/add_pick.py --league Allsvenskan --home "Malmo FF" --away "AIK" \\
        --market OU --selection Over2.5 --odds 1.95

timestamp-ът (published_at) се слага автоматично при insert-а — това е
доказателството, че прогнозата е публикувана преди старта на мача (заедно с
git commit-а, ако файлът/отчетът се публикува веднага след това).
"""
import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from picks_ledger import MARKETS, connect, normalize_selection


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", required=True)
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--market", required=True, choices=MARKETS)
    ap.add_argument("--selection", required=True, help="напр. Home/Draw/Away, Over2.5/Under2.5, Yes/No")
    ap.add_argument("--odds", type=float, default=None)
    ap.add_argument("--stake", type=float, default=1.0, help="единици залог, по подразбиране 1.0 (плоско)")
    ap.add_argument("--date", default=None, help="дата на мача YYYY-MM-DD (по избор, помага при resolve)")
    ap.add_argument("--source", default=None, help="напр. 'Model 1/2/3 съгласие', 'analyst'")
    ap.add_argument("--note", default=None)
    ap.add_argument("--public", action="store_true",
                     help="маркира pick-а видим без абонамент (trial teaser); иначе е subscriber-only")
    args = ap.parse_args()

    visibility = "public" if args.public else "subscriber"

    # хвърля ValueError с ясно съобщение, ако селекцията не е разпозната —
    # по-добре да гръмне тук, отколкото да влезе грешен ред в ledger-а
    norm_selection = normalize_selection(args.market, args.selection)

    published_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    con = connect()
    con.execute(
        """INSERT INTO picks (published_at, league, home, away, match_date, market,
                               selection, odds, stake_units, visibility, source, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (published_at, args.league, args.home, args.away, args.date, args.market,
         norm_selection, args.odds, args.stake, visibility, args.source, args.note),
    )
    con.commit()
    pick_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.close()

    print(f"Добавен pick #{pick_id}: {args.home} - {args.away} ({args.league}) "
          f"| {args.market} {norm_selection} @ {args.odds or '—'} | {args.stake}u | {visibility}")
    print(f"published_at (UTC): {published_at}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
