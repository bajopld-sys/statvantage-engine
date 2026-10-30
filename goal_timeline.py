"""Минути на головете — два независими пътя:

  build_goal_timelines_from_events — за лиги с fetch_sofa.py API данни
    (EPL/LaLiga/Eliteserien): чете направо goal_events (incidents.json),
    точен резултат, 100% покритие. Предпочитан път, виж early_payout.py.

  build_goal_timelines_sofa — за лиги БЕЗ API данни (Bundesliga/Ligue1,
    все още vault-парснати MD клипинги). Sofascore "live score" страниците
    (vault/<Лига>/RoundNN/*.md) имат обобщен ред точно след "Advertisement":

      "Matt O'Riley 55' (Pen.) Rodrigo Muniz 90' +7"
      "Erling Haaland 90' +3, 90' Maxime Estève 65' (OG), 12' (OG) ..."

    Отборната принадлежност на всеки скорер НЕ е дадена текстово (изчезва
    при сплескване на HTML колоните) — определя се чрез съпоставка на
    фамилията му със състава на двата отбора (от player_ratings). Автоголове
    ("(OG)"/"(o.g.)") се приписват на ПРОТИВНИКА. Всеки възстановен мач се
    ВАЛИДИРА срещу истинския резултат от matches — при несъответствие мачът
    се пропуска, не се тихо приема грешен.

    Известни празнини в източника (документирани, не скрити):
      - ~14% от мачовете с голове нямат обобщения ред в клипинга (изглежда
        Sofascore понякога го пропуска — не сме открили защо).
      - "MM' +N goals" (свит запис при 3+ гола на играч) не дава минути на
        допълнителните голове — такива мачове се пропускат изцяло.

Употреба: вика се от early_payout.py — не се пуска самостоятелно.
"""
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SUMMARY_RE = re.compile(
    r"Advertisement\n\n(.*?)\n\n(?:\d{2}/\d{2}/\d{4}\d{2}:\d{2}|Full-time)", re.S)
UNIT = r"\d{1,3}'(?:\s*\+\d+)?(?:\s*\([^)]*\))?"
ENTRY_RE = re.compile(
    rf"(?P<name>[A-ZÀ-Ý][\w'À-ſ.\- ]*?)\s+(?P<mins>{UNIT}(?:,\s*{UNIT})*)")
UNIT_RE = re.compile(r"(\d{1,3})'(?:\s*\+(\d+))?(?:\s*\(([^)]*)\))?")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def _parse_summary_line(line: str) -> list[tuple[str, int, str, bool]]:
    """-> [(име_вкарал, минута, полувреме 'A'/'B', автогол?), ...]
    Полувремето се определя от БАЗОВАТА минута (45'+2 → база 45 → 1-во
    полувреме), не от сборната — иначе 45'+N изглежда като 2-ро полувреме.
    Етикетите 'A'/'B' (не '1'/'2') — ПОПРАВКА (2026-07): двата "взаимозаменяеми"
    пътя в този файл преди използваха различен речник за едно и също нещо
    (build_goal_timelines_from_events дава 'A'/'B') — приведено към общ."""
    events = []
    for m in ENTRY_RE.finditer(line):
        name = m.group("name").strip()
        for um in UNIT_RE.finditer(m.group("mins")):
            base, extra = int(um.group(1)), int(um.group(2) or 0)
            note = (um.group(3) or "").lower()
            is_og = bool(re.search(r"o\.?g\.?", note))
            half = "A" if base <= 45 else "B"
            events.append((name, base + extra, half, is_og))
    return events


def _team_rosters_from_ratings(sofa_con) -> dict[str, set[str]]:
    """Съставите за лиги БЕЗ паралелна Опта база (Ligue1/Bundesliga/...) —
    от player_ratings (същия lineup-файл, вижда се от 2026-07-19), не от
    Опта player_match. Покритието зависи от кои кръгове имат lineup файл
    (историята кр. 1-20 няма) — best-effort, неразпознати имена си остават
    бележка, не тиха грешка (виж build_goal_timelines_generic)."""
    rosters: dict[str, set[str]] = {}
    for name, team in sofa_con.execute("SELECT DISTINCT name, team FROM player_ratings"):
        rosters.setdefault(team, set()).add(_norm(name.split()[-1]))
    return rosters


def _attribute_team(scorer: str, home: str, away: str, rosters: dict) -> str | None:
    surname = _norm(scorer.split()[-1])
    in_home = surname in rosters.get(home, set())
    in_away = surname in rosters.get(away, set())
    if in_home and not in_away:
        return home
    if in_away and not in_home:
        return away
    return None


def _find_opta_team(name_part: str, opta_names: dict) -> str | None:
    n = _norm(name_part)
    for on, orig in opta_names.items():
        if on == n or on in n or n in on:
            return orig
    return None


def build_goal_timelines_generic(vault_dir: Path, matches: dict, rosters: dict) -> tuple[dict, list[str]]:
    """Общото ядро — matches: (home,away)->(match_id,hg,ag), rosters:
    team->{нормализирани фамилии}. Позволява преизползване за лиги без
    паралелна Опта база (виж _team_rosters_from_ratings)."""
    opta_names = {_norm(h): h for h, a in matches} | {_norm(a): a for h, a in matches}

    by_match: dict[tuple, list[Path]] = {}
    if vault_dir.exists():
        for rd in vault_dir.glob("Round*"):
            for f in rd.glob("*.md"):
                m = re.match(r"(.+?) vs (.+?) live score", f.name)
                if m:
                    by_match.setdefault((rd.name, m.group(1), m.group(2)), []).append(f)

    timelines: dict[str, list[tuple[int, str]]] = {}
    notes: list[str] = []
    for (rd, h_raw, a_raw), files in by_match.items():
        h, a = _find_opta_team(h_raw, opta_names), _find_opta_team(a_raw, opta_names)
        if not h or not a or (h, a) not in matches:
            continue
        match_id, hg, ag = matches[(h, a)]
        if hg + ag == 0:
            continue

        line = None
        for f in files:
            mm = SUMMARY_RE.search(f.read_text(encoding="utf-8"))
            if mm and re.search(r"\d{1,3}'", mm.group(1)):
                line = mm.group(1)
                break
        if line is None:
            notes.append(f"{rd}: {h} {hg}:{ag} {a} — липсва scorer-ред в клипинга")
            continue
        if re.search(r"\+\d+\s*goals?\b", line):
            notes.append(f"{rd}: {h} {hg}:{ag} {a} — свит запис '+N goals', пропуснат")
            continue

        events = _parse_summary_line(line)
        hs = as_ = 0
        timeline = []
        unresolved = []          # (name, minute, half) — неразпознати чрез състава
        for name, minute, half, is_og in events:
            team = _attribute_team(name, h, a, rosters)
            if team is None:
                unresolved.append((name, minute, half))
                continue
            if is_og:
                team = a if team == h else h
            (hs, as_) = (hs + 1, as_) if team == h else (hs, as_ + 1)
            timeline.append((minute, team, half))

        # Елиминация по краен резултат: ако остане ТОЧНО 1 неразпознат
        # голмайстор (напр. резерва, липсва от player_ratings), но вече
        # знаем разпределението на всички останали — единствената страна,
        # на която ѝ липсва точно 1 гол до реалния резултат, е неговата.
        if len(unresolved) == 1 and (hg - hs, ag - as_) in ((1, 0), (0, 1)):
            name, minute, half = unresolved[0]
            team = h if (hg - hs, ag - as_) == (1, 0) else a
            (hs, as_) = (hs + 1, as_) if team == h else (hs, as_ + 1)
            timeline.append((minute, team, half))
            unresolved = []

        if unresolved:
            names = [n for n, *_ in unresolved]
            notes.append(f"{rd}: {h} vs {a} — неразпознати вкарали: {names}")
            continue
        if (hs, as_) != (hg, ag):
            notes.append(f"{rd}: {h} {hg}:{ag} {a} — възстановено {hs}:{as_} ≠ реалния резултат")
            continue

        timeline.sort()
        timelines[match_id] = timeline

    return timelines, notes


def build_goal_timelines_sofa(sofa_con, league_vault_dir: Path) -> tuple[dict, list[str]]:
    """{match_id: [(minute, team), ...]} за лиги БЕЗ fetch_sofa.py API данни
    (Bundesliga/Ligue1 — все още vault-парснати) — състави от player_ratings.
    За лиги С API данни (EPL/LaLiga/Eliteserien) виж build_goal_timelines_from_events
    — по-пълен и по-точен, не разчита на съвпадение фамилия-срещу-състав."""
    matches = {}
    for mid, h, a, hg, ag in sofa_con.execute(
            "SELECT match_id, home, away, hg, ag FROM matches"):
        matches[(h, a)] = (mid, hg, ag)
    rosters = _team_rosters_from_ratings(sofa_con)
    return build_goal_timelines_generic(league_vault_dir, matches, rosters)


def has_goal_events(con) -> bool:
    """goal_events съществува само за бази, минали през parse_sofa_api.py
    (incidents.json → точен гол-по-гол резултат от Sofascore)."""
    return bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='goal_events'"
    ).fetchone())


def build_goal_timelines_from_events(con) -> tuple[dict, list[str]]:
    """{match_id: [(minute, team, half), ...]} директно от goal_events —
    вече точен резултат след всеки гол, isHome коректно приписан от
    Sofascore (автоголове включени правилно, виж
    parse_sofa_api.parse_incidents). За разлика от build_goal_timelines[_sofa]
    не изисква съвпадение фамилия-срещу-състав — 100% покритие на мачовете
    в базата (включително 0:0 — празен, но валиден timeline), не ~86%."""
    teams = {mid: (h, a) for mid, h, a in
            con.execute("SELECT match_id, home, away FROM matches")}
    by_match: dict[str, list] = {}
    for mid, minute, added, is_home in con.execute(
            "SELECT match_id, minute, added_time, is_home FROM goal_events "
            "ORDER BY match_id, minute, added_time"):
        by_match.setdefault(mid, []).append((minute, added or 0, is_home))

    timelines: dict[str, list[tuple[int, str, str]]] = {}
    for mid, (home, away) in teams.items():
        # ПОПРАВКА (2026-07): added_time се четеше и дори се ползваше за
        # ORDER BY, но никога не се добавяше към минутата — гол на "90+3"
        # (истинска минута 93) се пазеше/докладваше като 90. Полувремето
        # все пак се определя от БАЗОВАТА минута (както в generic пътя
        # по-долу), не от сборната — 45'+2 остава 1-во полувреме.
        timelines[mid] = [
            (minute + added, home if is_home else away, "A" if minute <= 45 else "B")
            for minute, added, is_home in by_match.get(mid, [])]
    return timelines, []
