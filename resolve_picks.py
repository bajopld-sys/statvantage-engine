"""Разрешава чакащите picks в picks_ledger.db срещу реалните резултати.

Пуска се след всяко fetch_sofa.py + parse_sofa_api.py обновяване (може да се
добави като стъпка в update_all.py) — за всеки чакащ pick проверява
съответната лигова база (`data/sofa_<лига>.db`, само завършени мачове) и
маркира won/lost автоматично. Мачове, които още не са завършени, остават
`pending` — не е грешка, просто изчакват следващото обновяване.

Употреба:
    python engine/resolve_picks.py
"""
import sqlite3
import sys
import datetime
from pathlib import Path
from config import DATA_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from picks_ledger import connect, resolve_outcome, unit_result


def league_db_path(league: str) -> Path:
    return DATA_DIR / f"sofa_{league.lower()}.db"


def find_result(league: str, home: str, away: str, match_date: str | None) -> tuple[int, int] | None:
    """Резултатът на конкретния мач, или None ако още не е изигран/не е
    еднозначно определим.

    ПОПРАВКА (2026-09-04): при pick БЕЗ дата се вземаше `fetchone()` от
    "WHERE home=? AND away=?" без ORDER BY — при повече от един такъв мач
    (реванш в същия сезон, или запазена история от предишен сезон) SQLite
    връщаше произволния първи ред и pick-ът се разплащаше по ГРЕШЕН мач.
    Сега при нееднозначност НЕ се гадае: pick-ът остава pending и се
    отпечатва предупреждение да му се добави дата."""
    db_path = league_db_path(league)
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    try:
        if match_date:
            rows = con.execute(
                "SELECT hg, ag FROM matches WHERE home=? AND away=? AND date=?",
                (home, away, match_date),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT hg, ag, date FROM matches WHERE home=? AND away=? ORDER BY date",
                (home, away),
            ).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    if len(rows) > 1:
        dates = ", ".join(str(r[2]) for r in rows) if not match_date else ""
        print(f"⚠ {home} – {away} ({league}): {len(rows)} съвпадащи мача"
              f"{f' ({dates})' if dates else ''} — pick-ът остава pending, "
              f"добави точна дата (match_date), за да се разплати еднозначно.")
        return None
    row = rows[0]
    if row[0] is None or row[1] is None:
        return None
    return int(row[0]), int(row[1])


def main() -> None:
    con = connect()
    pending = con.execute(
        "SELECT id, league, home, away, match_date, market, selection, odds, stake_units "
        "FROM picks WHERE status='pending'"
    ).fetchall()

    if not pending:
        print("Няма чакащи picks.")
        return

    resolved_count = 0
    total_units = 0.0
    for (pick_id, league, home, away, match_date, market, selection,
         odds, stake) in pending:
        result = find_result(league, home, away, match_date)
        if result is None:
            continue
        hg, ag = result
        # ПОПРАВКА (2026-09-04): една счупена селекция вече не сваля целия
        # run — commit()-ът е СЛЕД цикъла, така че при изключение тук вече
        # разплатените picks се губеха.
        try:
            status = resolve_outcome(market, selection, hg, ag)
        except ValueError as exc:
            print(f"⚠ #{pick_id} {home}–{away}: {exc} — прескочен, остава pending.")
            continue
        units = unit_result(status, odds, stake)
        resolved_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        con.execute(
            "UPDATE picks SET status=?, result_score=?, resolved_at=? WHERE id=?",
            (status, f"{hg}-{ag}", resolved_at, pick_id),
        )
        resolved_count += 1
        total_units += units
        marker = "+" if units >= 0 else ""
        print(f"#{pick_id} {home} {hg}-{ag} {away} | {market} {selection} @ {odds or '—'} "
              f"→ {status.upper()} ({marker}{units:.2f}u)")

    con.commit()

    # обща сметка по всички някога разрешени picks (не само тези от този run)
    row = con.execute(
        "SELECT status, odds, stake_units FROM picks WHERE status IN ('won','lost','push')"
    ).fetchall()
    con.close()

    if resolved_count == 0:
        print("Няма мачове с вече наличен резултат — всички чакащи picks още не са завършили.")
        return

    grand_total = sum(unit_result(s, o, st) for s, o, st in row)
    wins = sum(1 for s, _, _ in row if s == "won")
    pushes = sum(1 for s, _, _ in row if s == "push")
    decided = len(row) - pushes  # push не е нито хит, нито мис — извън %
    rate = f"{wins / decided * 100:.1f}%" if decided else "—"
    push_note = f", {pushes} върнати (push)" if pushes else ""
    print(f"\nРазрешени сега: {resolved_count}")
    print(f"Общо в ledger-а: {len(row)} picks, {wins} печеливши "
          f"({rate}){push_note}, {grand_total:+.2f}u нето")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
