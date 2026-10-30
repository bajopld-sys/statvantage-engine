"""Sofascore фетчър → суров JSON кеш в data/raw_sofa/ (без парсване).

Заменя ръчното клипване: вместо по 3 MD файла на мач, дърпа същите данни
директно от вътрешното API на Sofascore, вече структурирани.

  /unique-tournament/{t}/seasons                        сезоните на турнира
  /unique-tournament/{t}/season/{s}/events/round/{n}    мачовете от кръга
  /event/{id}                отборите/резултата + СЪДИЯ (name, картони...)
  /event/{id}/statistics    отборна статистика, периоди ALL / 1ST / 2ND
  /event/{id}/lineups       състави + статистика НА НИВО ИГРАЧ (41 полета)
  /event/{id}/incidents     минути на головете, картони, смени

Съзнателно НЕ пише в sofa.db. Схемата на Sofascore се сменя без предупреждение;
ако парсер гръмне, суровите файлове са на диска и не се сваля наново. Парсването
е отделна стъпка (parse_sofa.py).

Достъп: обикновен requests получава 403 (Cloudflare). curl_cffi с TLS
отпечатък на Chrome минава — оттам и зависимостта.

Употреба:
    python engine/fetch_sofa.py --league EPL --year 25/26 --rounds 1-38
    python engine/fetch_sofa.py --league EPL --year 25/26 --rounds 1-2 --verify
    python engine/fetch_sofa.py --league EPL --year 25/26 --rounds auto --refresh
    python engine/fetch_sofa.py --list-seasons --league EPL
"""
import argparse
import json
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests as cr
except ImportError:  # pragma: no cover
    sys.exit("Липсва curl_cffi (обикновен requests получава 403 от Cloudflare):\n"
             "    python -m pip install curl_cffi")

API = "https://api.sofascore.com/api/v1"
from config import DATA_DIR
RAW = DATA_DIR / "raw_sofa"
SOFA_DB = DATA_DIR / "sofa.db"

# unique-tournament id-та; сезонът се резолвва по година през /seasons,
# защото id-то му е различно за всяка лига и всеки сезон.
LEAGUES = {
    "EPL": 17, "LALIGA": 8, "BUNDESLIGA": 35, "SERIEA": 23, "LIGUE1": 34,
    # LALIGA реактивирана 2026-08-16 — 25/26 сезонът приключи, 26/27 започна.
    # СТАРИЯТ sofa_laliga.db (25/26 данни) е архивиран в data/archive/
    # sofa_laliga_25-26.db (по избор на потребителя — чист файл за новия
    # сезон, не смесване, защото matches таблицата няма season_id колона
    # никъде в кода — form/дом-гост/etc. таблиците биха смятали през два
    # сезона накуп иначе). fetch_sofa.py --league LaLiga вече тегли ЧИСТО
    # 26/27 в нов sofa_laliga.db.
    # EPL/BUNDESLIGA/LIGUE1 — 25/26 (или по-стари) файлове архивирани СЪЩИЯ ден
    # (data/archive/sofa_epl_25-26.db, sofa_bundesliga_old-vault.db,
    # sofa_ligue1_25-26.db) при подготовка за 26/27, НО самите сезони още не са
    # започнали към 2026-08-16 (EPL/Ligue1 21.08, Bundesliga 28.08) — pipeline-ът
    # (тук + update_all.py + make_team_report.py) е конфигуриран предварително,
    # но update_all.py/make_team_report.py LEAGUES ги държат закоментирани,
    # докато не тръгнат реално.
    "ELITESERIEN": 20,   # Норвегия — сезонът е по календарна година ("2026"), не "25/26"
    "ALLSVENSKAN": 40,   # Швеция — също календарна година ("2026"), не "25/26"
    "FAROE": 673,        # Faroe Islands Premier League — календарна година ("2026").
                        # ВНИМАНИЕ (2026-08-03): id=188 ("Besta deild karla") се оказа
                        # Исландия, не Фарьорски острови — сгрешено първо, коригирано.
    "BUNDESLIGA2": 44,   # 2. Bundesliga (Германия, 2ро ниво) — добавена 2026-08-08,
                        # сезонът стартира 26/27 ("26/27" формат, не календарна година —
                        # различно от Eliteserien/Allsvenskan/Faroe). Отделен ключ от
                        # "BUNDESLIGA" (id=35, 1во ниво — все още на стария vault пайплайн,
                        # виж parse_sofa.py).
    "CHAMPIONSHIP": 18,  # Англия, 2ро ниво — добавена 2026-08-14, сезонът стартира
                        # 26/27 ("26/27" формат, не календарна година). Потвърдено чрез
                        # Sofascore search (category="England", единствен резултат за
                        # "championship" с тази категория — виж бележката в
                        # opta-engine-project паметта за риска от объркване с еднакви
                        # имена в различни държави).
    "LALIGA2": 54,       # Испания, 2ро ниво — "LaLiga 2" (преименувана от старото
                        # "Segunda División" — ВНИМАНИЕ, id=1476 "Segunda División"
                        # ("Spain") се оказа FUTSAL, не футбол, при проверка на
                        # sport полето — истинското футболно 2ро ниво е "LaLiga 2",
                        # id=54). Добавена 2026-08-16, сезонът стартира 26/27.
    "BELGIUMPL": 38,     # Белгия, 1во ниво (Jupiler Pro League) — добавена 2026-08-15,
                        # сезонът стартира 26/27 ("26/27" формат). Потвърдено чрез
                        # Sofascore search (category="Belgium", name="Pro League") —
                        # НЕ id=9 ("Challenger Pro League" — това е 2ро ниво в Белгия).
                        # Ключът е без "_" нарочно — main()'s args.league.upper() трябва
                        # да съвпадне точно с вътрешния league_name ("BelgiumPL") другаде.
}

# Sofascore пише някои отбори с наставка, каквато базата няма.
ALIASES = {
    "Liverpool FC": "Liverpool",
    "Wolverhampton": "Wolverhampton Wanderers",
    "Nottingham Forest": "Nottingham Forest",
    "Tottenham Hotspur": "Tottenham Hotspur",
}

ENDPOINTS = ("event", "statistics", "lineups", "incidents")


def endpoint_url(eid: int, ep: str) -> str:
    """'event' е /event/{id} самò (базовите данни за мача — вкл. съдия);
    останалите са /event/{id}/{ep}."""
    return f"/event/{eid}" if ep == "event" else f"/event/{eid}/{ep}"


# ---------------------------------------------------------------- HTTP слой

class Fetcher:
    """GET с кеш на диска, пауза между заявките и backoff при отказ."""

    def __init__(self, delay: float = 1.5, refresh: bool = False,
                 tries: int = 4) -> None:
        self.delay = delay
        self.refresh = refresh
        self.tries = tries
        self.session = cr.Session(impersonate="chrome")
        self.hits = 0        # прочетени от кеша
        self.downloads = 0
        self.fails: list[str] = []
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str, cache: Path, force: bool | None = None) -> dict | None:
        """Връща JSON от `cache`, иначе тегли `path` и го записва там.

        None означава, че Sofascore няма такъв ресурс (404 — типично за
        мач без обявени състави) или че всички опити са се провалили.

        `force`: override на self.refresh за този единствен извик (виж
        fetch_round() — данните на ЗАВЪРШЕН мач никога не се променят, затова
        техните ендпойнти изрично минават с force=False дори при
        глобален --refresh, за да не се тегли излишно наново всеки път
        (2026-08-02, открито при засичане на времето за пълно обновяване —
        792s/лига се оказаха почти изцяло излишни повторни тегления на
        отдавна завършени мачове)."""
        refresh = self.refresh if force is None else force
        if cache.exists() and not refresh:
            self.hits += 1
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cache.unlink()      # орязан файл от прекъснато сваляне

        for attempt in range(1, self.tries + 1):
            self._throttle()
            try:
                r = self.session.get(f"{API}{path}", timeout=30)
            except Exception as exc:                    # мрежов срив
                if attempt == self.tries:
                    self.fails.append(f"{path} — {type(exc).__name__}")
                    return None
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 404:
                return None                             # липсва легитимно
            if r.status_code == 200:
                try:
                    data = r.json()
                except json.JSONDecodeError:
                    self.fails.append(f"{path} — невалиден JSON")
                    return None
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
                self.downloads += 1
                return data
            if attempt == self.tries:
                self.fails.append(f"{path} — HTTP {r.status_code}")
                return None
            # 403/429/5xx — Cloudflare или рейт лимит; изчакай по-дълго
            time.sleep(2 ** attempt * (3 if r.status_code in (403, 429) else 1))
        return None


# ------------------------------------------------------------------ помощни

def norm(name: str) -> str:
    """Име на отбор за сравнение: без диакритика, без наставки, малки букви."""
    name = ALIASES.get(name, name)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    for suffix in (" FC", " AFC", " CF"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.lower().strip()


def resolve_season(f: Fetcher, tour: int, year: str) -> tuple[int, str]:
    """Година '25/26' → season id. Кешира се, викa се веднъж на пускане."""
    data = f.get(f"/unique-tournament/{tour}/seasons",
                 RAW / "seasons" / f"{tour}.json")
    if not data:
        sys.exit(f"Не мога да взема сезоните на турнир {tour}.")
    seasons = data.get("seasons", [])
    for s in seasons:
        if s.get("year") == year:
            return s["id"], s["name"]
    known = ", ".join(s.get("year", "?") for s in seasons[:8])
    sys.exit(f"Няма сезон '{year}' за турнир {tour}. Налични: {known} ...")


def parse_rounds(spec: str) -> list[int]:
    """'1-38' или '1,3,7' или '1-5,9' → списък кръгове."""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def _round_is_closed(cache: Path) -> bool:
    """True ако кешираният JSON на кръга съществува И всичките му мачове
    вече са 'finished' — такъв кръг математически не може да се промени
    повече (резултатът на завършен мач е окончателен), затова е безопасно
    да НЕ се презарежда дори при --refresh (2026-08-26, по искане на
    потребителя: "няма смисъл да се преглеждат завършили кръгове, там
    няма как да се променят стата"). Кръг с отложен/непочнал мач НЕ се
    брои за затворен — трябва да продължи да се проверява."""
    if not cache.exists():
        return False
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    events = data.get("events", [])
    if not events:
        return False
    return all(ev.get("status", {}).get("type") == "finished" for ev in events)


def auto_upper_round(f: Fetcher, tour: int, season: int, start: int = 1,
                     cap: int = 45, lookahead: int = 3) -> int:
    """Най-високият кръг, чийто прозорец вече е настъпил (поне 1 мач с
    startTimestamp <= сега) — независимо дали е завършен, отложен или
    все още 'Not started'. Не спира на първия дупка-кръг (отложен мач,
    докато цялата лига е играла напред) — сканира до `lookahead` кръга
    напред и връща най-високия наистина достигнат, за да не заклещва
    fetch-а зад единичен пренасрочен мач (виж Viking FK round 11/12).

    Кръгове, вече маркирани "затворени" (виж _round_is_closed) от предишно
    пускане, четат директно от кеша — не презареждат round-listing endpoint-а
    дори при --refresh (2026-08-26 оптимизация, виж докстринга на
    _round_is_closed за причината)."""
    now = time.time()
    highest = start - 1
    rd = start
    while rd <= cap:
        cache = RAW / "rounds" / f"{tour}_{season}_r{rd}.json"
        force = f.refresh and not _round_is_closed(cache)
        data = f.get(f"/unique-tournament/{tour}/season/{season}/events/round/{rd}",
                     cache, force=force)
        events = (data or {}).get("events", [])
        if events and any(ev.get("startTimestamp", 0) <= now for ev in events):
            highest = rd
        elif not events or rd - highest > lookahead:
            break
        rd += 1
    return highest


def event_date(ev: dict) -> str:
    """Дата на мача в UTC — ISO YYYY-MM-DD."""
    ts = ev.get("startTimestamp")
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# -------------------------------------------------------------------- тегли

def fetch_round(f: Fetcher, tour: int, season: int, rd: int,
                endpoints: tuple[str, ...]) -> list[dict]:
    """Тегли кръга и всички негови мачове. Връща обобщение по мач.

    Round-listing заявката тук съвпада 1:1 с тази в auto_upper_round() —
    без _round_is_closed() проверката би презареждала СЪЩИЯ кръг ВТОРИ път
    в рамките на едно и също пускане (веднъж там, веднъж тук), чист излишък
    за кръг, който вече знаем, че е затворен (2026-08-26 оптимизация)."""
    rounds_cache = RAW / "rounds" / f"{tour}_{season}_r{rd}.json"
    force = f.refresh and not _round_is_closed(rounds_cache)
    data = f.get(f"/unique-tournament/{tour}/season/{season}/events/round/{rd}",
                 rounds_cache, force=force)
    if not data:
        return []

    summary: list[dict] = []
    for ev in data.get("events", []):
        eid = ev["id"]
        finished = ev.get("status", {}).get("type") == "finished"
        row = {
            "event_id": eid,
            "round": rd,
            "date": event_date(ev),
            "home": ev["homeTeam"]["name"],
            "away": ev["awayTeam"]["name"],
            "hg": ev.get("homeScore", {}).get("current"),
            "ag": ev.get("awayScore", {}).get("current"),
            "finished": finished,
            "got": [],
        }
        if not finished:
            # Незавършен мач: статистиката е частична или липсва — не се тегли.
            #
            # ПРОМЯНА (2026-09-08): съставите вече СЕ теглят тук. Преди
            # коментарът тук гласеше "не ги кешираме, защото се променят до
            # първата свирка" — вярно за ПОСТОЯНен кеш (events/), но не и за
            # ЖИВИЯ TTL кеш (lineup_impact._cached_json, 30 мин), който точно
            # това решава. Без това make_team_report.py ги теглеше сам минути
            # по-късно — същите заявки, само че извън всякакъв кеш и без паузата
            # между заявките на Fetcher (виж паметта [[api-cost-discipline]]).
            if "lineups" in endpoints:
                try:
                    from lineup_impact import lineups_json
                    if lineups_json(eid) is not None:
                        row["got"].append("lineups~")   # ~ = жив кеш, не постоянен
                except Exception as exc:
                    print(f"⚠ състави {eid}: {str(exc)[:60]}")
                time.sleep(f.delay)   # същото темпо като Fetcher — не се бърза
            summary.append(row)
            continue

        for ep in endpoints:
            # force=False дори при --refresh: данните на завършен мач са
            # окончателни, повторно теглене е чист излишък (виж Fetcher.get).
            got = f.get(endpoint_url(eid, ep),
                        RAW / "events" / str(eid) / f"{ep}.json", force=False)
            if got is not None:
                row["got"].append(ep)
        summary.append(row)
    return summary


# ------------------------------------------------------------------ сверка

def load_db_stats() -> dict[tuple[str, str], dict]:
    """Сегашните пълномачови отборни числа от sofa.db, по (норм. домакин, гост)."""
    if not SOFA_DB.exists():
        return {}
    con = sqlite3.connect(SOFA_DB)
    out: dict[tuple[str, str], dict] = {}
    for mid, home, away in con.execute("SELECT match_id, home, away FROM matches"):
        rows = {
            team: dict(xg=xg, shots=shots, sot=sot, corners=corners)
            for team, xg, shots, sot, corners in con.execute(
                "SELECT team, xg, shots, sot, corners FROM stats "
                "WHERE match_id = ? AND period = 'full'", (mid,))
        }
        if rows:
            out[(norm(home), norm(away))] = {"home": home, "away": away,
                                             "stats": rows}
    con.close()
    return out


def stat_value(stats_json: dict, *names: str, period: str = "ALL") -> tuple:
    """Стойност (домакин, гост) по име на показател. Липсва → (None, None).

    Търси във всички групи, защото Sofascore мести показатели между тях, и
    приема няколко имена, защото ги преименува ('Expected goals' беше
    'Expected goals (xG)'). Групата нарочно не участва в търсенето — обвързване
    с нея вече веднъж накара сверката да мълчи вместо да гърми.
    """
    wanted = set(names)
    for per in stats_json.get("statistics", []):
        if per.get("period") != period:
            continue
        for g in per.get("groups", []):
            for item in g.get("statisticsItems", []):
                if item.get("name") in wanted:
                    return item.get("homeValue"), item.get("awayValue")
    return (None, None)


def verify(summary: list[dict]) -> None:
    """Сравнява изтегленото с това, което вече е в sofa.db."""
    db = load_db_stats()
    if not db:
        print("\nСверка: sofa.db е празна или липсва — пропускам.")
        return

    checked = matched = 0
    problems: list[str] = []
    for row in summary:
        if not row["finished"] or "statistics" not in row["got"]:
            continue
        key = (norm(row["home"]), norm(row["away"]))
        if key not in db:
            problems.append(f"{row['date']} {row['home']}–{row['away']}: "
                            f"няма го в базата (или имената не се връзват)")
            continue
        checked += 1
        entry = db[key]
        raw = json.loads((RAW / "events" / str(row["event_id"]) /
                          "statistics.json").read_text(encoding="utf-8"))

        ok = True
        for team_key, team_name in (("home", entry["home"]), ("away", entry["away"])):
            have = entry["stats"].get(team_name)
            if not have:
                continue
            idx = 0 if team_key == "home" else 1
            api_shots = stat_value(raw, "Total shots")[idx]
            api_xg = stat_value(raw, "Expected goals", "Expected goals (xG)")[idx]
            if have["shots"] is not None and api_shots is not None and \
                    abs(have["shots"] - api_shots) > 0.5:
                problems.append(f"{row['date']} {team_name}: удари база="
                                f"{have['shots']:.0f} API={api_shots}")
                ok = False
            if have["xg"] is not None and api_xg is not None and \
                    abs(have["xg"] - float(api_xg)) > 0.05:
                problems.append(f"{row['date']} {team_name}: xG база="
                                f"{have['xg']:.2f} API={api_xg}")
                ok = False
        matched += ok

    print(f"\nСверка срещу sofa.db: {checked} мача проверени, {matched} чисти.")
    for p in problems:
        print("  !", p)


# --------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="EPL",
                    help="EPL/LaLiga/Bundesliga/SerieA/Ligue1 или число "
                         "(unique-tournament id)")
    ap.add_argument("--year", default="25/26", help="сезон, напр. 25/26")
    ap.add_argument("--rounds", default="1",
                    help="'1-38', '1,3,7' или 'auto' (до текущия момент, "
                         "прескача отложени мачове без да спира)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="секунди между заявките (по-ниско = по-вероятен 429)")
    ap.add_argument("--refresh", action="store_true",
                    help="презаписва кеша вместо да го ползва")
    ap.add_argument("--no-lineups", action="store_true",
                    help="без съставите — само отборна статистика")
    ap.add_argument("--verify", action="store_true",
                    help="сверява изтегленото със сегашната sofa.db")
    ap.add_argument("--list-seasons", action="store_true",
                    help="изброява сезоните на лигата и излиза")
    args = ap.parse_args()

    key = args.league.upper()
    tour = int(args.league) if args.league.isdigit() else LEAGUES.get(key)
    if tour is None:
        sys.exit(f"Непозната лига '{args.league}'. Известни: "
                 f"{', '.join(LEAGUES)} — или подай числово id.")

    f = Fetcher(delay=args.delay, refresh=args.refresh)

    if args.list_seasons:
        data = f.get(f"/unique-tournament/{tour}/seasons",
                     RAW / "seasons" / f"{tour}.json")
        for s in (data or {}).get("seasons", []):
            print(f"  {s['year']:>7}  id={s['id']:<7} {s['name']}")
        return

    season, season_name = resolve_season(f, tour, args.year)
    endpoints = tuple(e for e in ENDPOINTS
                      if not (args.no_lineups and e == "lineups"))
    if args.rounds == "auto":
        upper = auto_upper_round(f, tour, season)
        rounds = list(range(1, upper + 1))
        print(f"auto: кръговете до текущия момент = 1-{upper} "
              f"(отложени мачове вътре не спират броенето напред)")
    else:
        rounds = parse_rounds(args.rounds)
    print(f"{season_name} (season id {season}) — кръгове {rounds[0]}–{rounds[-1]}, "
          f"пауза {args.delay}s")

    summary: list[dict] = []
    for rd in rounds:
        got = fetch_round(f, tour, season, rd, endpoints)
        summary.extend(got)
        done = sum(1 for r in got if r["finished"])
        print(f"  кръг {rd:>2}: {len(got)} мача ({done} завършени)")

    # index.json е ПО ЛИГА (index_EPL.json, index_LALIGA.json...) — общ файл
    # би се презаписвал при всяко пускане за друга лига, трие предишния кръг
    # sweep тихо. Мержва се по event_id, за да оцелеят кръгове от по-стари
    # пускания на СЪЩАТА лига (напр. довличане на кръгове 11-38 по-късно).
    RAW.mkdir(parents=True, exist_ok=True)
    index_path = RAW / f"index_{key}.json"
    existing = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    merged = {r["event_id"]: r for r in existing}
    merged.update({r["event_id"]: r for r in summary})
    all_rows = sorted(merged.values(), key=lambda r: (r["round"], r["date"] or ""))
    index_path.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1),
                          encoding="utf-8")

    with_lineups = sum(1 for r in summary if "lineups" in r["got"])
    print(f"\nМачове: {len(summary)}  със състави: {with_lineups}")
    print(f"Заявки: {f.downloads} изтеглени, {f.hits} от кеша")
    print(f"Кеш: {RAW}  (индекс: {index_path.name})")
    if f.fails:
        print(f"Провалени заявки: {len(f.fails)}")
        for x in f.fails[:20]:
            print("  !", x)

    if args.verify:
        if key != "EPL":
            print(f"\n--verify пропуснато: сверката е само срещу EPL sofa.db "
                 f"(няма паралелна ръчно-клипната база за {key}).")
        else:
            verify(summary)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
