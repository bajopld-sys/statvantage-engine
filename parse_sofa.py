"""Sofascore парсер → изолирана база data/sofa.db (отборно ниво).

Източник: Sofascore "live score" клипинги (VAULT_ROOT/EPL/
RoundNN/*.md за историята; занапред потребителят сваля по 3 файла на мач —
базов (lineups/рейтинги), (1) = 1-во полувреме статистика, (2) = 2-ро).

Формат на статистиките (декодиран 2026-07): числата се пренасят през
редовете по фиксирано правило —
    home_1
    ЕТИКЕТ_1
    away_1 home_2
    ЕТИКЕТ_2
    ...
    away_n
т.е. числото ПРЕДИ етикета е на домакина, числото СЛЕД етикета е на госта
(слято на един ред с домакинската стойност на следващия показател).

Валидации (нищо не се приема тихо):
  1. Аритметика на ударите per блок: SoT + извън + блокирани == общо,
     в наказателното + извън наказателното == общо.
  2. Класификация на периодите: "известните" имена на отбори се откриват от
     самите файлови заглавия (discover_teams); периодите (1-во/2-ро п-ме)
     се приемат по конвенция (файлов ред), без външна xG сверка (старата
     Опта-база отпадна при разчистването на 2026-07-23).

Все още активен само за лиги без fetch_sofa.py API данни (Bundesliga/
Ligue1) — EPL/LaLiga/Eliteserien минават през parse_sofa_api.py.

Употреба:  python engine/parse_sofa.py --league Bundesliga [--vault DIR] [--db ...]
"""
import argparse
import difflib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from config import DATA_DIR, VAULT_ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))

VAULT = VAULT_ROOT / "EPL"
SOFA_DB = DATA_DIR / "sofa.db"


OVERVIEW_LABELS = [
    "Ball possession", "Expected goals (xG)", "Big chances", "Total shots",
    "Goalkeeper saves", "Corner kicks", "Fouls", "Passes", "Tackles",
    "Free kicks", "Yellow cards", "Red cards",
]
SHOTS_LABELS = [
    "Total shots", "Shots on target", "Hit woodwork", "Shots off target",
    "Blocked shots", "Shots inside box", "Shots outside box",
]
ATTACK_LABELS = [
    "Big chances scored", "Big chances missed", "Through balls",
    "Fouled in final third", "Offsides",
]

# каноничните имена = ОПТА имената, за да е тривиална сверката. Кратките
# прякори в lineup секцията ("Man City", "Spurs") невинаги са субстринг на
# пълното Опта име (виж canon_team fallback) — явно изредени тук за
# случаите, при които fuzzy matching e рисково (кратки/неприпокриващи се
# думи като "Spurs"/"Hotspur").
SOFA_TO_OPTA = {
    "Liverpool FC": "Liverpool", "Wolverhampton": "Wolverhampton Wanderers",
    "Man City": "Manchester City", "Man Utd": "Manchester United",
    "Man United": "Manchester United", "Spurs": "Tottenham Hotspur",
    "Palace": "Crystal Palace", "Villa": "Aston Villa",
    "Wolves": "Wolverhampton Wanderers", "Forest": "Nottingham Forest",
    "Brighton": "Brighton & Hove Albion",
}

FIELD_MAP = {
    "Ball possession": "possession", "Expected goals (xG)": "xg",
    "Big chances": "big_chances", "Total shots": "shots",
    "Goalkeeper saves": "saves", "Corner kicks": "corners", "Fouls": "fouls",
    "Passes": "passes", "Tackles": "tackles", "Free kicks": "free_kicks",
    "Yellow cards": "yellows", "Red cards": "reds",
    "Shots on target": "sot", "Hit woodwork": "woodwork",
    "Shots off target": "shots_off", "Blocked shots": "blocked",
    "Shots inside box": "inside_box", "Shots outside box": "outside_box",
    "Big chances scored": "big_scored", "Big chances missed": "big_missed",
    "Through balls": "through_balls", "Fouled in final third": "fouled_final3",
    "Offsides": "offsides",
}

STAT_FIELDS = list(dict.fromkeys(FIELD_MAP.values()))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def canon_team(raw: str, known: list[str]) -> str | None:
    raw = SOFA_TO_OPTA.get(raw.strip(), raw.strip())
    n = _norm(raw)
    for k in known:
        nk = _norm(k)
        if n == nk or n in nk or nk in n:
            return k
    # fallback за къси Sofascore прякори в lineup секцията ("Man City",
    # "Spurs", "Palace" и т.н.), които не са субстринг на пълното име —
    # fuzzy matching по същия принцип като simulate.py's resolve().
    close = difflib.get_close_matches(n, [_norm(k) for k in known], n=1, cutoff=0.4)
    if close:
        for k in known:
            if _norm(k) == close[0]:
                return k
    return None


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


def _nums(line: str) -> list[float] | None:
    """Числата от ред с данни; None ако редът не е чисто числов."""
    cleaned = line.replace("%", " ")
    if not re.fullmatch(r"[\d.\s\-]+", cleaned):
        return None
    found = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
    return [float(x) for x in found] if found else None


def parse_shifted_block(lines: list[str], labels: list[str]) -> dict | None:
    """{етикет: (home, away)} по правилото 'преди/след етикета'."""
    seq = []          # елементи: ("L", етикет) или ("N", [числа])
    for l in lines:
        if l in labels:
            seq.append(("L", l))
        else:
            n = _nums(l)
            if n is not None:
                seq.append(("N", n))
    # очакван вид: N L N L N ... L N (числата: 1, 2, 2, ..., 2, 1)
    found_labels = [x[1] for x in seq if x[0] == "L"]
    if not found_labels:
        return None
    values: list[float] = []
    for kind, v in seq:
        if kind == "N":
            values.extend(v)
    if len(values) != 2 * len(found_labels):
        return None      # разместен блок — не гадаем
    out = {}
    for i, lab in enumerate(found_labels):
        out[lab] = (values[2 * i], values[2 * i + 1])   # (home, away)
    return out


def _slice(lines: list[str], start: str, end: str, from_idx: int = 0):
    try:
        i = lines.index(start, from_idx)
    except ValueError:
        return None, from_idx
    try:
        j = lines.index(end, i + 1)
    except ValueError:
        j = len(lines)
    return lines[i + 1:j], j


def parse_stats_file(text: str) -> dict | None:
    """Един клипинг → {поле: (home, away)} за периода, който показва."""
    lines = _lines(text)
    ov, idx = _slice(lines, "Match overview", "Shotmap")
    if ov is None:
        return None
    out = parse_shifted_block(ov, OVERVIEW_LABELS) or {}

    sh, idx2 = _slice(lines, "Attack map", "Attack", idx)
    if sh is not None:
        blk = parse_shifted_block(sh, SHOTS_LABELS)
        if blk:
            out.update({k: v for k, v in blk.items() if k not in out or k != "Total shots"})
    at, _ = _slice(lines, "Attack", "Pass map", idx2)
    if at is not None:
        blk = parse_shifted_block(at, ATTACK_LABELS)
        if blk:
            out.update(blk)
    if not out:
        return None
    return {FIELD_MAP[k]: v for k, v in out.items() if k in FIELD_MAP}


def shots_arithmetic_ok(s: dict) -> bool | str:
    """Валидация/поправка на ударите чрез двете независими разбивки:
      сбор1 = SoT + извън целта + блокирани;  сбор2 = в наказ. + извън наказ.
    - двете съвпадат с тотала → чисто
    - двете съвпадат ПОМЕЖДУ си, но не с тотала → тоталът е грешен в самите
      Sofascore данни; ПОПРАВЯМЕ тотала към сбора (връща "repaired")
    - разминаване ≤1 само в едната → шум в източника, приема се
    - иначе → False (блокът не се доверява)"""
    need = ("shots", "sot", "shots_off", "blocked", "inside_box", "outside_box")
    if not all(k in s for k in need):
        return True      # непълен блок — няма как да проверим, не блокираме
    new_tot = list(s["shots"])
    repaired = False
    for side in (0, 1):
        tot = s["shots"][side]
        sum1 = s["sot"][side] + s["shots_off"][side] + s["blocked"][side]
        sum2 = s["inside_box"][side] + s["outside_box"][side]
        if sum1 == tot and sum2 == tot:
            continue                                       # чисто
        if sum1 == sum2 and sum1 != tot:
            new_tot[side] = sum1                           # тоталът е сгрешен
            repaired = True
        elif (sum1 == tot and abs(sum2 - tot) <= 1) or \
             (sum2 == tot and abs(sum1 - tot) <= 1):
            continue                       # едната потвърждава, другата ±1 шум
        else:
            return False
    if repaired:
        s["shots"] = tuple(new_tot)
        return "repaired"
    return True


FORMATION_RE = re.compile(r"^\d(-\d){2,4}$")
JERSEY_NAME_RE = re.compile(r"^(\d+)(\(c\))?\s*(.+)$")


def parse_lineup_ratings(text: str, known: list[str]) -> tuple[dict, list[str]]:
    """{отбор: [(номер, капитан?, име, рейтинг), ...]} от базовия
    (lineups+ratings) клипинг.

    Наблюдавана структура (НЕ интуитивна — засича се с точно 2 формационни
    реда, "4-1-3-2" стил): [домакин header/рейтинг/формация] [домакин 11
    играча] [гост 11 играча] [гост header/рейтинг/формация] — рейтингът и
    формацията на госта идват СЛЕД неговите играчи, не преди. Затова
    играчите между двете формационни позиции се събират в общ списък и се
    разполовяват (11/11), не се делят по header маркер."""
    raw = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [l for l in raw if not l.startswith("![")]
    idxs = [i for i, l in enumerate(lines) if FORMATION_RE.match(l)]
    notes: list[str] = []
    if len(idxs) != 2 or idxs[0] < 2 or idxs[1] < 2:
        return {}, ["lineup файл: неочакван брой формационни редове"]
    i1, i2 = idxs
    team1 = canon_team(lines[i1 - 2], known)
    team2 = canon_team(lines[i2 - 2], known)
    try:
        avg1, avg2 = float(lines[i1 - 1]), float(lines[i2 - 1])
    except ValueError:
        avg1 = avg2 = None

    pairs = []
    j = i1 + 1
    end = i2 - 2
    while j < end - 1:
        m_rating = re.fullmatch(r"\d+\.\d+", lines[j])
        if m_rating:
            m_name = JERSEY_NAME_RE.match(lines[j + 1])
            if m_name:
                num, capt, name = m_name.group(1), bool(m_name.group(2)), m_name.group(3)
                pairs.append((int(num), capt, name.strip(), float(lines[j])))
                j += 2
                continue
        j += 1

    half = len(pairs) // 2
    p1, p2 = pairs[:half], pairs[half:]
    out = {}
    if team1 and p1:
        if avg1 is not None and abs(sum(x[3] for x in p1) / len(p1) - avg1) > 0.5:
            notes.append(f"{team1}: изчисленият среден рейтинг не пасва на "
                         f"отборния ({avg1}) — блокът е пропуснат")
        else:
            out[team1] = p1
    if team2 and p2:
        if avg2 is not None and abs(sum(x[3] for x in p2) / len(p2) - avg2) > 0.5:
            notes.append(f"{team2}: изчисленият среден рейтинг не пасва на "
                         f"отборния ({avg2}) — блокът е пропуснат")
        else:
            out[team2] = p2
    return out, notes


def is_lineup_file(text: str) -> bool:
    return "Ratings for this competition" in text and "Match overview" not in text


def parse_meta(texts: list[str]) -> dict:
    """Дата, кръг, FT резултат, предматчови 1X2 коефициенти — от които и да е файл."""
    meta = {}
    for text in texts:
        lines = _lines(text)
        joined = "\n".join(lines)
        if "date" not in meta:
            m = re.search(r"Date and time(\d{2}/\d{2}/\d{4})", joined)
            if m:
                d, mo, y = m.group(1).split("/")
                meta["date"] = f"{y}-{mo}-{d}"
        if "round" not in meta:
            m = re.search(r"Football, [^,]+, [^,]+, Round (\d+)", joined)
            if m:
                meta["round"] = int(m.group(1))
        if "hg" not in meta:
            m = re.search(r"FT (\d+) - (\d+)", joined)
            if m:
                meta["hg"], meta["ag"] = int(m.group(1)), int(m.group(2))
        if "odds_h" not in meta:
            for i, l in enumerate(lines[:-6]):
                if l == "Full-time" and lines[i + 1] == "1" and lines[i + 3] == "X":
                    try:
                        meta["odds_h"] = float(lines[i + 2])
                        meta["odds_d"] = float(lines[i + 4])
                        meta["odds_a"] = float(lines[i + 6])
                    except (ValueError, IndexError):
                        pass
                    break
        if "referee" not in meta:
            for i, l in enumerate(lines[:-1]):
                if l == "Referee":
                    meta["referee"] = lines[i + 1]
                    break
    return meta


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS matches(
        match_id TEXT PRIMARY KEY, date TEXT, round INTEGER,
        home TEXT, away TEXT, hg INTEGER, ag INTEGER,
        odds_h REAL, odds_d REAL, odds_a REAL, referee TEXT)""")
    cols_existing = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    if "referee" not in cols_existing:
        con.execute("ALTER TABLE matches ADD COLUMN referee TEXT")
    cols = ", ".join(f"{f} REAL" for f in STAT_FIELDS)
    con.execute(f"""CREATE TABLE IF NOT EXISTS stats(
        match_id TEXT, team TEXT, period TEXT, {cols},
        PRIMARY KEY(match_id, team, period))""")
    con.execute("""CREATE TABLE IF NOT EXISTS player_ratings(
        match_id TEXT, team TEXT, num INTEGER, captain INTEGER,
        name TEXT, rating REAL, PRIMARY KEY(match_id, team, name))""")


def upsert_stats(con, match_id, home, away, period, s: dict) -> None:
    for side, team in ((0, home), (1, away)):
        vals = {f: (s[f][side] if f in s else None) for f in STAT_FIELDS}
        cols = ", ".join(vals)
        ph = ", ".join("?" for _ in vals)
        con.execute(
            f"INSERT OR REPLACE INTO stats(match_id, team, period, {cols}) "
            f"VALUES(?,?,?,{ph})", (match_id, team, period, *vals.values()))


def sum_sets(a: dict, b: dict) -> dict:
    out = {}
    for k in set(a) & set(b):
        if k == "possession":
            out[k] = ((a[k][0] + b[k][0]) / 2, (a[k][1] + b[k][1]) / 2)
        else:
            out[k] = (a[k][0] + b[k][0], a[k][1] + b[k][1])
    return out


def discover_teams(league_dir: Path) -> list[str]:
    """Автоматично открива имената на отборите от заглавията на файловете
    в цялата лига (за лиги без паралелна Опта база — LaLiga/Bundesliga/
    Ligue1/SerieA нямат наш Опта източник за 'известни' имена)."""
    teams = set()
    for rd in league_dir.glob("Round*"):
        for f in rd.glob("*.md"):
            m = re.match(r"(.+?) vs (.+?) live score", f.name)
            if m:
                teams.add(m.group(1).strip())
                teams.add(m.group(2).strip())
    return sorted(teams)


def group_files(folder: Path, known: list[str], label: str,
                notes: list[str]) -> dict[tuple, list[Path]]:
    groups: dict[tuple, list[Path]] = {}
    for f in folder.glob("*.md"):
        m = re.match(r"(.+?) vs (.+?) live score", f.name)
        if not m:
            continue
        h = canon_team(m.group(1), known)
        a = canon_team(m.group(2), known)
        if not h or not a:
            notes.append(f"{label}: непознат отбор в '{f.name}'")
            continue
        groups.setdefault((h, a), []).append(f)
    return groups


def process_group(con, label: str, h: str, a: str, files: list[Path],
                  known: list[str], notes: list[str], counts: dict) -> None:
    texts = [f.read_text(encoding="utf-8") for f in sorted(files)]
    meta = parse_meta(texts)
    if "hg" not in meta or "date" not in meta:
        notes.append(f"{label}: {h} vs {a} — липсва FT резултат/дата")
        return
    match_id = f"{meta['date']}|{h}|{a}"
    con.execute(
        "INSERT OR REPLACE INTO matches VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, meta["date"], meta.get("round"), h, a,
         meta["hg"], meta["ag"], meta.get("odds_h"),
         meta.get("odds_d"), meta.get("odds_a"), meta.get("referee")))
    counts["matches"] += 1

    for f, text in zip(sorted(files), texts):
        if is_lineup_file(text):
            lr, lr_notes = parse_lineup_ratings(text, known)
            for note in lr_notes:
                notes.append(f"{label}: {h} vs {a} — {note}")
            for team, players in lr.items():
                if team not in (h, a):
                    continue
                for num, capt, name, rating in players:
                    con.execute(
                        "INSERT OR REPLACE INTO player_ratings VALUES(?,?,?,?,?,?)",
                        (match_id, team, num, int(capt), name, rating))

    sets = []
    for f, text in zip(sorted(files), texts):
        if is_lineup_file(text):
            continue
        s = parse_stats_file(text)
        if s:
            ok = shots_arithmetic_ok(s)
            if ok is False:
                notes.append(f"{label}: {h} vs {a} — счупена аритметика "
                             f"на ударите в '{f.name}', блокът е пропуснат")
                continue
            if ok == "repaired":
                notes.append(f"{label}: {h} vs {a} — тотал удари поправен "
                             f"от разбивките в '{f.name}'")
            sets.append(s)

    if len(sets) == 2:
        # приемаме конвенцията (1)=1-во, (2)=2-ро полувреме и вътрешната
        # аритметика — няма външна xG база за сверка (виж докстринга).
        upsert_stats(con, match_id, h, a, "H1", sets[0])
        upsert_stats(con, match_id, h, a, "H2", sets[1])
        upsert_stats(con, match_id, h, a, "full", sum_sets(*sets))
        counts["halves"] += 1
    elif sets:
        notes.append(f"{label}: {h} vs {a} — {len(sets)} блока (очаквани 2), "
                     f"пропуснат")
    else:
        notes.append(f"{label}: {h} vs {a} — без нито един четим блок")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL",
                    help="подпапка във VAULT_ROOT (EPL/Bundesliga/LaLiga/"
                         "Ligue1/SerieA) — определя vault пътя и db файла")
    ap.add_argument("--vault", help="директно override на vault пътя")
    ap.add_argument("--dir", help="плоска папка с нови клипинги (не Round-организирана)")
    ap.add_argument("--db", help="override на изходната база")
    args = ap.parse_args()

    vault_dir = Path(args.vault) if args.vault else VAULT.parent / args.league
    db_path = Path(args.db) if args.db else (
        SOFA_DB.parent / f"sofa_{args.league.lower()}.db")

    # 'известните' имена се откриват от самите файлови заглавия; периодите
    # (1-во/2-ро п-ме) се приемат по конвенция (файлов ред), без xG сверка.
    known = discover_teams(vault_dir)

    con = sqlite3.connect(db_path)
    ensure_schema(con)

    notes: list[str] = []
    counts = {"matches": 0, "full": 0, "halves": 0}

    for rd in sorted(vault_dir.glob("Round*")):
        groups = group_files(rd, known, rd.name, notes)
        for (h, a), files in sorted(groups.items()):
            process_group(con, rd.name, h, a, files, known, notes, counts)

    if args.dir:
        d = Path(args.dir)
        groups = group_files(d, known, d.name, notes)
        for (h, a), files in sorted(groups.items()):
            process_group(con, d.name, h, a, files, known, notes, counts)

    con.commit()
    con.close()
    print(f"Мачове: {counts['matches']}  (halves-класифицирани: {counts['halves']}, "
          f"full-блок: {counts['full']})")
    print(f"Проблемни/бележки: {len(notes)}")
    for n in notes:
        print("  !", n)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
