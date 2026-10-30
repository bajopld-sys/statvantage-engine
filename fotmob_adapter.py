"""FotMob адаптер (2026-10-03) — резервен източник на данни, докато Sofascore
API-то е блокирано (403 от ~29.09). ПРОТОТИП: пише в ОТДЕЛНА тестова база
(data/fotmob_test_<лига>.db), със СЪЩАТА схема като sofa_<лига>.db, и НЕ пипа
живите бази / пайплайна. Сравнение със Sofascore-данните: `--validate`.

Какво взима (всичко от неофициалния fotmob.com/api/data/*, curl_cffi chrome):
  /leagues?id=<id>        -> списък мачове на сезона (id, отбори, кръг, резултат)
  /matchDetails?matchId=  -> екипна статистика по период, голове/HT, съдия,
                             състави, играчова статистика (вкл. xG/xA)

Дисциплина за заявките (виж api-cost-discipline): ВСЕКИ отговор се кешира на диск
(data/raw_fotmob/); завършен мач не се тегли повторно; пауза между заявките;
при първия не-200 отговор спираме веднага (без блъскане); лимит на нови заявки
за пускане (--max-new).

Употреба:
    python engine/fotmob_adapter.py --league Allsvenskan --max-new 40
    python engine/fotmob_adapter.py --league Allsvenskan --validate
"""
import argparse
import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from config import DATA_DIR
RAW = DATA_DIR / "raw_fotmob"
DELAY = 2.0
LEAGUE_LIST_TTL = 3600

# вътрешно име на лига -> (FotMob id, sofa db файл)
LEAGUES = {
    "Allsvenskan": (67, "sofa_allsvenskan.db"),
    "Eliteserien": (59, "sofa_eliteserien.db"),
    "EPL": (47, "sofa_epl.db"),
    "LaLiga": (87, "sofa_laliga.db"),
    "LaLiga2": (140, "sofa_laliga2.db"),
    "SerieA": (55, "sofa_seriea.db"),
    "Ligue1": (53, "sofa_ligue1.db"),
    "Bundesliga": (54, "sofa_bundesliga.db"),
    "Bundesliga2": (146, "sofa_bundesliga2.db"),
    "Championship": (48, "sofa_championship.db"),
    "BelgiumPL": (40, "sofa_belgiumpl.db"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches(
    match_id TEXT PRIMARY KEY, date TEXT, round INTEGER,
    home TEXT, away TEXT, hg INTEGER, ag INTEGER,
    odds_h REAL, odds_d REAL, odds_a REAL, referee TEXT, sofa_event_id INTEGER,
    home_ht INTEGER, away_ht INTEGER, fotmob_id INTEGER);
CREATE TABLE IF NOT EXISTS stats(
    match_id TEXT, team TEXT, period TEXT, possession REAL, xg REAL, big_chances REAL,
    shots REAL, saves REAL, corners REAL, fouls REAL, passes REAL, tackles REAL,
    free_kicks REAL, yellows REAL, reds REAL, sot REAL, woodwork REAL, shots_off REAL,
    blocked REAL, inside_box REAL, outside_box REAL, big_scored REAL, big_missed REAL,
    through_balls REAL, fouled_final3 REAL, offsides REAL,
    PRIMARY KEY(match_id, team, period));
CREATE TABLE IF NOT EXISTS goal_events(
    match_id TEXT, minute INTEGER, added_time INTEGER, is_home INTEGER,
    scorer TEXT, kind TEXT, home_score INTEGER, away_score INTEGER,
    PRIMARY KEY(match_id, minute, added_time, is_home, scorer));
CREATE TABLE IF NOT EXISTS player_match(
    match_id TEXT, team TEXT, sofa_player_id INTEGER, name TEXT, position TEXT,
    started INTEGER, captain INTEGER, accurate_crosses REAL, accurate_passes REAL,
    aerials_lost REAL, aerials_won REAL, assists REAL, big_chances_created REAL,
    big_chances_missed REAL, clearances REAL, crosses REAL, dribbles REAL,
    dribbles_won REAL, duels_lost REAL, duels_won REAL, fouls REAL, goals REAL,
    goals_prevented REAL, interceptions REAL, key_passes REAL, minutes REAL,
    offsides REAL, passes REAL, possession_lost REAL, rating REAL, recoveries REAL,
    saves REAL, shots REAL, sot REAL, tackles REAL, tackles_won REAL, touches REAL,
    was_fouled REAL, xa REAL, xg REAL,
    PRIMARY KEY(match_id, team, sofa_player_id));
"""

# FotMob ключ на екипна статистика -> колона в stats
TEAM_STAT_KEYS = {
    "BallPossesion": "possession", "expected_goals": "xg", "big_chance": "big_chances",
    "total_shots": "shots", "keeper_saves": "saves", "corners": "corners",
    "fouls": "fouls", "passes": "passes", "matchstats.headers.tackles": "tackles",
    "yellow_cards": "yellows", "red_cards": "reds", "ShotsOnTarget": "sot",
    "shots_woodwork": "woodwork", "ShotsOffTarget": "shots_off", "blocked_shots": "blocked",
    "shots_inside_box": "inside_box", "shots_outside_box": "outside_box",
    "big_chance_missed_title": "big_missed", "Offsides": "offsides",
}
PERIODS = {"All": "full", "FirstHalf": "A", "SecondHalf": "B"}

# етикет на играчова статистика (FotMob) -> колона в player_match
PLAYER_LABELS = {
    "FotMob rating": "rating", "Minutes played": "minutes", "Goals": "goals",
    "Assists": "assists", "Expected goals (xG)": "xg", "Expected assists (xA)": "xa",
    "Total shots": "shots", "Shots on target": "sot", "Saves": "saves",
    "Tackles": "tackles", "Interceptions": "interceptions", "Clearances": "clearances",
    "Chances created": "key_passes", "Big chances created": "big_chances_created",
    "Big chances missed": "big_chances_missed", "Accurate passes": "accurate_passes",
    "Accurate crosses": "accurate_crosses", "Aerial duels won": "aerials_won",
    "Duels won": "duels_won", "Duels lost": "duels_lost", "Touches": "touches",
    "Recoveries": "recoveries", "Was fouled": "was_fouled", "Fouls committed": "fouls",
    "Offsides": "offsides", "Goals prevented": "goals_prevented",
    "Successful dribbles": "dribbles_won", "Dispossessed": "possession_lost",
}


class Blocked(Exception):
    pass


def _get(url: str, cache: Path, ttl: float | None) -> dict:
    """ttl=None -> вечен кеш (завършен мач). Иначе кешът важи ttl секунди."""
    if cache.exists():
        fresh = ttl is None or (time.time() - cache.stat().st_mtime) < ttl
        if fresh:
            return json.loads(cache.read_text(encoding="utf-8"))
    from curl_cffi import requests as rq
    time.sleep(DELAY)
    r = rq.get(url, impersonate="chrome", timeout=30)
    if r.status_code != 200:
        if cache.exists():  # stale-fallback, както в lineup_impact._cached_json
            print(f"⚠ {r.status_code} за {url} — ползвам остарял кеш")
            return json.loads(cache.read_text(encoding="utf-8"))
        raise Blocked(f"{r.status_code} за {url}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(r.text, encoding="utf-8")
    return r.json()


# ------------------------------------------------------------------ имена
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(if|aif|bk|fk|ik|fc|ff|sk|sc|fotball|fotbollsforening|ifk|bois|club)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ръчни съпоставки, където автоматичното не успява (FotMob име -> наше име)
OVERRIDES = {
    "OH Leuven": "Oud-Heverlee Leuven", "St.Truiden": "Sint-Truidense VV",
    "Racing Santander": "Real Racing Club", "Rennes": "Stade Rennais",
}


def build_name_map(fm_names: list[str], our_names: list[str]) -> dict[str, str]:
    """FotMob име -> нашето (Sofascore) име. Нормализиран най-близък съвпад;
    неуспешните се връщат с None (виж отпечатката — ръчно ги допълвай в OVERRIDES)."""
    ours = {_norm(n): n for n in our_names}
    out = {}
    for fm in fm_names:
        if OVERRIDES.get(fm) in our_names:
            out[fm] = OVERRIDES[fm]
            continue
        k = _norm(fm)
        if k in ours:
            out[fm] = ours[k]
            continue
        # едното съдържа другото (Mjällby vs Mjällby AIF -> и двете "mjallby")
        cand = [o for nk, o in ours.items() if k and (k in nk or nk in k)]
        if len(cand) == 1:
            out[fm] = cand[0]
            continue
        close = difflib.get_close_matches(k, list(ours), n=1, cutoff=0.75)
        out[fm] = ours[close[0]] if close else None
    return out


# ----------------------------------------------------------------- парсване
def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_match(md: dict, name_of) -> dict | None:
    gen, hdr, c = md["general"], md["header"], md["content"]
    home = name_of(gen["homeTeam"]["name"])
    away = name_of(gen["awayTeam"]["name"])
    if not home or not away:
        return None
    date = gen["matchTimeUTCDate"][:10]
    mid = f"{date}|{home}|{away}"
    hg, ag = (int(t["score"]) for t in hdr["teams"][:2])

    events = c["matchFacts"]["events"].get("events", [])
    ht = next(((e.get("homeScore"), e.get("awayScore")) for e in events
               if e.get("type") == "Half" and e.get("halfStrShort") == "HT"), (None, None))
    ref = c["matchFacts"].get("infoBox", {}).get("Referee")
    referee = ref.get("text") if isinstance(ref, dict) else ref

    row = {"match_id": mid, "date": date, "round": int(gen.get("matchRound") or 0),
           "home": home, "away": away, "hg": hg, "ag": ag, "referee": referee,
           "home_ht": ht[0], "away_ht": ht[1], "fotmob_id": int(gen["matchId"])}

    # екипна статистика по период
    stats = []
    for pname, plabel in PERIODS.items():
        groups = c.get("stats", {}).get("Periods", {}).get(pname, {}).get("stats", [])
        vals = [{}, {}]
        for g in groups:
            for s in g.get("stats", []):
                col = TEAM_STAT_KEYS.get(s.get("key"))
                st = s.get("stats")
                if not col or not isinstance(st, list) or len(st) != 2:
                    continue
                for side in (0, 1):
                    if col not in vals[side] and _num(st[side]) is not None:
                        vals[side][col] = _num(st[side])
        for side, team in enumerate((home, away)):
            if vals[side]:
                stats.append({"match_id": mid, "team": team, "period": plabel, **vals[side]})

    # голове (is_home по ръста на резултата — вярно и при автоголове)
    goals, prev = [], (0, 0)
    for e in sorted((e for e in events if e.get("type") == "Goal" and not e.get("isPenaltyShootoutEvent")),
                    key=lambda e: (e.get("time") or 0, e.get("overloadTime") or 0)):
        ns = e.get("newScore") or [None, None]
        if ns[0] is None:
            continue
        is_home = 1 if ns[0] > prev[0] else 0
        prev = (ns[0], ns[1])
        kind = "ownGoal" if e.get("ownGoal") else (
            "penalty" if "pen" in str(e.get("goalDescription") or "").lower() else "regular")
        goals.append({"match_id": mid, "minute": e.get("time"), "added_time": e.get("overloadTime"),
                      "is_home": is_home, "scorer": (e.get("player") or {}).get("name"),
                      "kind": kind, "home_score": ns[0], "away_score": ns[1]})

    # играчи
    lineup = c.get("lineup", {})
    starters = {p["id"] for side in ("homeTeam", "awayTeam")
                for p in (lineup.get(side) or {}).get("starters", [])}
    home_id = gen["homeTeam"]["id"]
    players = []
    for pid, p in (c.get("playerStats") or {}).items():
        vals = {}
        for grp in p.get("stats", []):
            for label, obj in (grp.get("stats") or {}).items():
                col = PLAYER_LABELS.get(label)
                if col and isinstance(obj, dict):
                    v = _num((obj.get("stat") or {}).get("value"))
                    if v is not None:
                        vals.setdefault(col, v)
        players.append({"match_id": mid, "team": home if p.get("teamId") == home_id else away,
                        "sofa_player_id": int(pid), "name": p.get("name"),
                        "position": str(p.get("usualPosition") or p.get("positionId") or ""),
                        "started": 1 if int(pid) in starters else 0, "captain": None, **vals})
    return {"match": row, "stats": stats, "goals": goals, "players": players}


def _insert(con, table, rows):
    for r in rows:
        cols = ",".join(r)
        con.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES({','.join('?' * len(r))})",
                    list(r.values()))


# ------------------------------------------------------------------- build
def build(league: str, max_new: int) -> None:
    fid, sofa_db = LEAGUES[league]
    lg = _get(f"https://www.fotmob.com/api/data/leagues?id={fid}&ccode3=BGR",
              RAW / "leagues" / f"{fid}.json", LEAGUE_LIST_TTL)
    fixtures = [m for m in lg["fixtures"]["allMatches"] if m["status"].get("finished")
                and not m["status"].get("cancelled")]
    print(f"{league}: {len(fixtures)} завършени мача във FotMob")

    sofa_path = DATA_DIR / sofa_db
    our_names = []
    if sofa_path.exists():
        sc = sqlite3.connect(sofa_path)
        our_names = [r[0] for r in sc.execute("SELECT home FROM matches UNION SELECT away FROM matches")]
        sc.close()
    fm_names = sorted({m["home"]["name"] for m in fixtures} | {m["away"]["name"] for m in fixtures})
    nmap = build_name_map(fm_names, our_names) if our_names else {n: n for n in fm_names}
    print("Имена FotMob -> наши:")
    for k, v in nmap.items():
        print(f"   {k:28s} -> {v}")
    missing = [k for k, v in nmap.items() if v is None]
    if missing:
        print(f"⚠ без съвпадение: {missing} — тези мачове се пропускат")

    out = DATA_DIR / f"fotmob_test_{league.lower()}.db"
    con = sqlite3.connect(out)
    con.executescript(SCHEMA)
    have = {r[0] for r in con.execute("SELECT fotmob_id FROM matches")}
    todo = [m for m in reversed(fixtures) if int(m["id"]) not in have]   # най-новите първо
    new_requests = 0
    done = 0
    for m in todo:
        cache = RAW / "matches" / f"{m['id']}.json"
        if not cache.exists():
            if new_requests >= max_new:
                break
            new_requests += 1
        try:
            md = _get(f"https://www.fotmob.com/api/data/matchDetails?matchId={m['id']}", cache, None)
        except Blocked as exc:
            print(f"⛔ СПИРАМ — {exc}")
            break
        parsed = parse_match(md, lambda n: nmap.get(n))
        if not parsed:
            continue
        _insert(con, "matches", [parsed["match"]])
        _insert(con, "stats", parsed["stats"])
        _insert(con, "goal_events", parsed["goals"])
        _insert(con, "player_match", parsed["players"])
        done += 1
        if done % 10 == 0:
            con.commit()
            print(f"   ... {done} мача")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"Готово: {done} нови мача, общо {n} в {out.name}; нови заявки: {new_requests}")
    con.close()


# ---------------------------------------------------------------- validate
def validate(league: str) -> None:
    fid, sofa_db = LEAGUES[league]
    a = sqlite3.connect(DATA_DIR / sofa_db)
    b = sqlite3.connect(DATA_DIR / f"fotmob_test_{league.lower()}.db")
    fm = {(r[0], r[1], r[2]): r for r in b.execute(
        "SELECT home, away, round, date, hg, ag, home_ht, away_ht, referee, match_id FROM matches")}
    rows = a.execute("SELECT home, away, round, date, hg, ag, home_ht, away_ht, referee, match_id FROM matches").fetchall()
    common = [(r, fm[(r[0], r[1], r[2])]) for r in rows if (r[0], r[1], r[2]) in fm]
    print(f"Общи мачове (home,away,round): {len(common)} от {len(rows)} наши / {len(fm)} FotMob")
    if not common:
        return

    def rate(label, pairs):
        ok = sum(1 for x, y in pairs if x == y)
        print(f"  {label:26s} {ok}/{len(pairs)}  ({100 * ok / len(pairs):.0f}%)")
        return [(x, y) for x, y in pairs if x != y]

    rate("резултат (hg,ag)", [((o[4], o[5]), (f[4], f[5])) for o, f in common])
    rate("HT резултат", [((o[6], o[7]), (f[6], f[7])) for o, f in common if o[6] is not None])
    rate("съдия", [(o[8], f[8]) for o, f in common if o[8]])
    rate("дата", [(o[3], f[3]) for o, f in common])

    def team_stat(con, mid, team, col):
        r = con.execute(f"SELECT {col} FROM stats WHERE match_id=? AND team=? AND period='full'", (mid, team)).fetchone()
        return r[0] if r else None

    for col, tol in (("shots", 0), ("corners", 0), ("sot", 0), ("yellows", 0), ("saves", 0), ("xg", 0.35)):
        pairs = []
        for o, f in common:
            for team in (o[0], o[1]):
                x, y = team_stat(a, o[9], team, col), team_stat(b, f[9], team, col)
                if x is not None and y is not None:
                    pairs.append((x, y))
        if pairs:
            ok = sum(1 for x, y in pairs if abs(x - y) <= tol)
            diff = sum(abs(x - y) for x, y in pairs) / len(pairs)
            print(f"  екипно {col:16s} {ok}/{len(pairs)} в допуск ±{tol}   ср.разлика {diff:.2f}")

    # голове: брой и минути
    g_ok = g_tot = 0
    for o, f in common:
        ga = sorted((r[0], r[1]) for r in a.execute("SELECT minute, is_home FROM goal_events WHERE match_id=?", (o[9],)))
        gb = sorted((r[0], r[1]) for r in b.execute("SELECT minute, is_home FROM goal_events WHERE match_id=?", (f[9],)))
        g_tot += 1
        g_ok += ga == gb
    print(f"  голове (минути+страна)    {g_ok}/{g_tot}")

    # играчов xG: покритие (Sofascore няма за Allsvenskan)
    pa = a.execute("SELECT COUNT(*), COUNT(xg) FROM player_match").fetchone()
    pb = b.execute("SELECT COUNT(*), COUNT(xg), COUNT(xa) FROM player_match").fetchone()
    print(f"  играчов xG: Sofascore {pa[1]}/{pa[0]}  |  FotMob {pb[1]}/{pb[0]} (xA {pb[2]})")


# ------------------------------------------------------------------- merge
# (2026-10-03) Връзка на FotMob към ЖИВИТЕ sofa_<лига>.db, докато Sofascore е
# блокиран: добавя САМО липсващите завършени мачове (по home,away,round) и
# записва предстоящите кръгове в raw_sofa/rounds/ във формата, който чете
# next_fixtures._next_round_fixtures(). --dry-run работи върху копия в
# data/_merge_sandbox/ и не пипа нищо живо.
TOUR_SEASON = {
    "Bundesliga2": (44, 97406), "Championship": (18, 97037), "BelgiumPL": (38, 96616),
    "LaLiga": (8, 97268), "EPL": (17, 96668), "Ligue1": (34, 96127), "SerieA": (23, 95836),
    "Bundesliga": (35, 97464),   # добавена 2026-10-16 (след 5-ия кръг), започва от празна база
}
RAW_ROUNDS = DATA_DIR / "raw_sofa" / "rounds"


def _load_names(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT home FROM matches UNION SELECT away FROM matches")]


def _write_upcoming(league: str, fixtures_all: list, nmap: dict, dry: bool) -> int:
    import datetime
    tour, season = TOUR_SEASON[league]
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    by_round: dict[int, list] = {}
    for m in fixtures_all:
        st = m["status"]
        if st.get("finished") or st.get("cancelled") or st.get("started"):
            continue
        ts = datetime.datetime.fromisoformat(st["utcTime"].replace("Z", "+00:00")).timestamp()
        h, a = nmap.get(m["home"]["name"]), nmap.get(m["away"]["name"])
        if ts < now or not h or not a:
            continue
        by_round.setdefault(int(m["roundName"]), []).append({
            "id": int(m["id"]), "homeTeam": {"name": h, "shortName": h},
            "awayTeam": {"name": a, "shortName": a}, "startTimestamp": int(ts),
            "status": {"type": "notstarted", "description": "Not started"},
            "_source": "fotmob"})
    # само най-близките 2 кръга (останалите не са нужни и пълнят кеша)
    written = 0
    for rd in sorted(by_round)[:2]:
        path = RAW_ROUNDS / f"{tour}_{season}_r{rd}.json"
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            old = obj.get("events", [])
            from_fotmob = bool(old) and all(e.get("_source") == "fotmob" for e in old)
            if old and not from_fotmob:
                # Файл от Sofascore (с неговите event id-та — по тях са записаните прогнози): ПАЗИМ id-тата,
                # но обновяваме само часовете от FotMob (2026-10-26). Sofascore дава временен час
                # (напр. 15:00) за всички бъдещи кръгове от август и никога не го обновява сега, когато е
                # блокиран — Милан–Болоня стоеше „27.10 15:00", а реалният старт е 28.10 19:30.
                pair = {(e["homeTeam"]["name"], e["awayTeam"]["name"]): e for e in by_round[rd]}
                changed = 0
                for e in old:
                    f = pair.get((e["homeTeam"]["name"], e["awayTeam"]["name"]))
                    if f and e.get("startTimestamp") != f["startTimestamp"]:
                        e["startTimestamp"] = f["startTimestamp"]
                        changed += 1
                if changed and not dry:
                    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
                written += changed
                continue
        written += len(by_round[rd])
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"events": by_round[rd]}, ensure_ascii=False), encoding="utf-8")
    return written


def merge_league(league: str, max_new: int, dry: bool) -> dict:
    import shutil
    fid, sofa_db = LEAGUES[league]
    live = DATA_DIR / sofa_db
    if dry:
        target = DATA_DIR / "_merge_sandbox" / sofa_db
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(live, target)
    else:
        bak = DATA_DIR / "archive" / "pre_fotmob_20261003"
        bak.mkdir(parents=True, exist_ok=True)
        if not (bak / sofa_db).exists():
            shutil.copyfile(live, bak / sofa_db)
        target = live

    lg = _get(f"https://www.fotmob.com/api/data/leagues?id={fid}&ccode3=BGR",
              RAW / "leagues" / f"{fid}.json", LEAGUE_LIST_TTL)
    all_fx = lg["fixtures"]["allMatches"]
    finished = [m for m in all_fx if m["status"].get("finished") and not m["status"].get("cancelled")]
    con = sqlite3.connect(target)
    ours = _load_names(con)
    fm_names = sorted({m["home"]["name"] for m in all_fx} | {m["away"]["name"] for m in all_fx})
    # празна (току-що създадена) база няма имена за съпоставяне -> FotMob имената стават наши
    nmap = build_name_map(fm_names, ours) if ours else {n: n for n in fm_names}
    bad = [k for k, v in nmap.items() if v is None]
    if bad:
        print(f"  ⚠ {league}: без съвпадение на имена: {bad}")
    have = {(r[0], r[1], r[2]) for r in con.execute("SELECT home, away, round FROM matches")}
    pid_map = {}
    for team, name, pid in con.execute("SELECT team, name, sofa_player_id FROM player_match WHERE sofa_player_id > 0"):
        pid_map.setdefault((team, _norm(name or "")), pid)

    merged = skipped = new_req = 0
    for m in reversed(finished):
        h, a = nmap.get(m["home"]["name"]), nmap.get(m["away"]["name"])
        if not h or not a or (h, a, int(m["roundName"])) in have:
            continue
        cache = RAW / "matches" / f"{m['id']}.json"
        if not cache.exists():
            if new_req >= max_new:
                skipped += 1
                continue
            new_req += 1
        try:
            md = _get(f"https://www.fotmob.com/api/data/matchDetails?matchId={m['id']}", cache, None)
        except Blocked as exc:
            print(f"  ⛔ СПИРАМ — {exc}")
            break
        p = parse_match(md, lambda n: nmap.get(n))
        if not p:
            continue
        p["match"].pop("fotmob_id", None)
        for pl in p["players"]:
            pl["sofa_player_id"] = pid_map.get((pl["team"], _norm(pl["name"] or "")), -pl["sofa_player_id"])
        _insert(con, "matches", [p["match"]])
        _insert(con, "stats", p["stats"])
        _insert(con, "goal_events", p["goals"])
        _insert(con, "player_match", p["players"])
        merged += 1
    con.commit()
    con.close()
    upcoming = _write_upcoming(league, all_fx, nmap, dry)
    return {"merged": merged, "skipped_over_budget": skipped, "requests": new_req, "upcoming_written": upcoming}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default=None, choices=list(LEAGUES),
                    help="по подразбиране: Allsvenskan за build/validate, всичките 7 активни за --merge")
    ap.add_argument("--max-new", type=int, default=40, help="макс. НОВИ заявки към FotMob за пускане")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--merge", action="store_true", help="добави липсващите мачове в ЖИВАТА sofa_<лига>.db")
    ap.add_argument("--dry-run", action="store_true", help="с --merge: работи върху копие в data/_merge_sandbox/")
    args = ap.parse_args()
    if args.merge:
        leagues = [args.league] if args.league else list(TOUR_SEASON)
        budget = args.max_new
        for lgname in leagues:
            res = merge_league(lgname, budget, args.dry_run)
            budget -= res["requests"]
            print(f"{lgname:13s} {res}")
    elif args.validate:
        validate(args.league or "Allsvenskan")
    else:
        build(args.league or "Allsvenskan", args.max_new)
