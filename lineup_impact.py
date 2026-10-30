"""Ефект от липсващи титулярни играчи върху lambda/mu — прикача се към
LINEUP_CLAMP от dixon_coles.py (дефиниран, но неизползван до сега).

Spec (изграден с потребителя разговорно):
  - Всеки титулярен играч се слага в АТАКА (Нападатели + атакуващи халфове)
    или ЗАЩИТА (Защитници + дефанзивни халфове).
  - Полузащитниците се класифицират по СЕЗОННА статистика, не по номинална
    позиция: defensive_score = tackles+interceptions+recoveries,
    attacking_score = key_passes+assists+xa; по-високият дял определя групата.
  - "Титуляр" = играч, стартирал в >=50% от изиграните мачове на отбора
    този сезон (STARTER_THRESHOLD) — не само единствения топ играч.
  - Рейтинг = блендиран 60% последни 5 (реално оценени) мача / 40% сезонен
    среден (същия принцип като Форма групата в model_b_heuristic.py).
  - За ВСЕКИ липсващ титуляр в дадена група: delta = неговия рейтинг минус
    средния на ОСТАНАЛИТЕ (неотсъстващи) титуляри в групата; персонален
    factor = 1 - LINEUP_K*delta (НЕклампнат поединично).
  - Персоналните фактори на групата се умножават един по друг, и чак
    КРАЙНИЯТ резултат се clamp-ва в LINEUP_CLAMP (0.75-1.25) — изрично
    поискано от потребителя ("да не се прекалява... 3 титуляра не значи
    -5 гола"), за да не се разпада ефектът неограничено при много
    отсъствия едновременно.
  - LINEUP_K е интуитивна стойност (0.25) — не е backtest-калибрирана
    (изисква реконструкция на исторически "липсващ играч" случаи, което не
    е направено). Третирай като начална стойност за тест, не като финална.

Употреба:
    from lineup_impact import lineup_factors
    factors = lineup_factors(con, "Viking FK", as_of, missing_names={"Bård Finne"})
    # factors == {"attack": 0.91, "defense": 1.0}
"""
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd

from dixon_coles import LINEUP_CLAMP
from config import DATA_DIR

BEST_EFFORT_TIMEOUT = 6  # секунди (2026-08-26, по искане на потребителя) — тези
# извиквания са best-effort, никога критичен път (виж докстринговете по-долу),
# грациозно връщат None/празно при грешка. Понижено от 15 — при забой на Sofascore
# 15с чакане на второстепенен endpoint (missing players/GK атрибути) е излишно
# скъпо за update_all.py, докато тези endpoint-и нормално отговарят под 1с.


_ATTACK_WEIGHT_PRIOR = {"F": 0.85, "M": 0.5, "D": 0.15, "G": 0.05}
# Позиционен приор за _attack_weight() по-долу — начални, НЕ backtest-
# калибрирани стойности (същия статус като HOME_ADV/SHRINK_K другаде).


# ------------------------------------------- дисков кеш за живите заявки
# (2026-09-08) fetch_sofa.Fetcher кешира историческите мачове, но тези шест
# извиквания (състави на предстоящи мачове + играчови атрибути) го заобикаляха
# и удряха API-то ПРИ ВСЯКА регенерация на отчета: ~80 мача × (2-3 състава +
# ~22 играча за v2) ≈ 2000 заявки на пускане. Пет регенерации в един ден
# докараха 403 от Cloudflare върху ЦЕЛИЯ api.sofascore.com.
LIVE_CACHE_DIR = DATA_DIR / "raw_sofa" / "live"
LINEUP_TTL = 30 * 60        # прогнозните състави се менят до началото на мача
ATTRS_TTL = 24 * 3600       # сезонни агрегати на играч — менят се бавно
BLOCK_COOLDOWN = 15 * 60    # пауза за живите заявки след 403 (виж _cached_json)
_BLOCKED_UNTIL = 0.0
# Sofascore е ИЗКЛЮЧЕН от генерирането (2026-10-26, по решение на потребителя: "където нещо трябва да
# се проверява от Sofascore го махни, защото вече не работи"). Заявки към api.sofascore.com не се
# пращат; _cached_json връща само каквото вече има в дисковия кеш (стари състави/атрибути), а
# актуалните състави идват от FotMob (fotmob_lineups.py). Върни True само ако блокът падне.
SOFASCORE_ENABLED = False
TOP_PLAYERS_TTL = 6 * 3600  # лийг-широки лидерборд статистики (спасявания и
# т.н.) — обновяват се веднъж на кръг, но 6ч позволява същия ден да хване
# нов кръг без да чака 24ч (2026-09-19, добавено за вратарски спасявания)


def _cached_json(url: str, cache_key: str, ttl: int,
                 permanent_if=None, timeout: int = BEST_EFFORT_TIMEOUT):
    """GET с дисков кеш. Три поведения, всяко нарочно:
      1. пресен кеш (под ttl) или маркиран "permanent" -> връща се без заявка
      2. липсващ/остарял кеш -> тегли и записва
      3. заявката се провали (403/timeout/счупен JSON) -> връща ЗАСТАРЯЛ кеш,
         ако има такъв. По-добре вчерашен състав, отколкото нищо — точно този
         случай остави всичките 80 мача без състав при блока от 2026-09-08.
    `permanent_if(data) -> bool` маркира отговора като вечен (напр. потвърден
    състав вече не се мени)."""
    global _BLOCKED_UNTIL
    path = LIVE_CACHE_DIR / f"{cache_key}.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            fresh = (time.time() - path.stat().st_mtime) < ttl
            if cached.get("_permanent") or fresh:
                return cached.get("data")
    if not SOFASCORE_ENABLED:
        return cached.get("data") if cached else None
    # Прекъсвач (2026-10-03): след първия 403 не блъскаме блокираното API още
    # BLOCK_COOLDOWN секунди — спестява стотици безсмислени заявки при регенерация
    # на отчета и не удължава блока.
    if time.time() < _BLOCKED_UNTIL:
        return cached.get("data") if cached else None
    try:
        from curl_cffi import requests as _rq
        r = _rq.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code == 403:
            _BLOCKED_UNTIL = time.time() + BLOCK_COOLDOWN
            print(f"⚠ Sofascore 403 — пауза {BLOCK_COOLDOWN // 60} мин за живите заявки")
        if r.status_code != 200:
            return cached.get("data") if cached else None
        data = r.json()
    except Exception as exc:
        if cached is None:
            print(f"⚠ {cache_key}: {str(exc)[:70]}")
        return cached.get("data") if cached else None
    try:
        LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "data": data,
            "_permanent": bool(permanent_if(data)) if permanent_if else False,
        }), encoding="utf-8")
    except Exception:
        pass    # кешът е удобство, не критичен път
    return data


def lineups_json(event_id: int):
    """Съставите за мача, с кеш. Потвърденият състав се пази вечно.

    РЕЗЕРВА (2026-10-03): ако Sofascore не върне нищо (блок 403), пробва FotMob
    (fotmob_lineups.py) — контузени/наказани още дни напред, стартови 11 близо до
    мача. Резултатът НЕ се пише в Sofascore кеш ключа (виж докстринга там)."""
    data = _cached_json(
        f"https://api.sofascore.com/api/v1/event/{event_id}/lineups",
        f"lineups_{event_id}", LINEUP_TTL,
        permanent_if=lambda d: bool(d.get("confirmed")))
    # Sofascore блокиран (прекъсвачът е активен) -> каквото е в кеша е ОСТАРЯЛО
    # (напр. контузени отпреди седмици), затова пред него предпочитаме пресния
    # FotMob; потвърден състав (вечен кеш) не го пипаме.
    sofa_down = (not SOFASCORE_ENABLED) or time.time() < _BLOCKED_UNTIL
    if data is None or (sofa_down and not data.get("confirmed")):
        try:
            from fotmob_lineups import lineups_from_fotmob
            fm_data = lineups_from_fotmob(event_id)
            if fm_data is not None:
                data = fm_data
        except Exception as exc:
            print(f"⚠ fotmob резерва {event_id}: {str(exc)[:70]}")
    return data


def player_attributes_json(player_id: int):
    """attribute-overviews на играч, с 24-часов кеш."""
    return _cached_json(
        f"https://api.sofascore.com/api/v1/player/{player_id}/attribute-overviews",
        f"attrs_{player_id}", ATTRS_TTL)


def top_players_json(tournament_id: int, season_id: int):
    """Лийг-широк лидерборд по статистики (голове, спасявания, тъкли и т.н.)
    за целия сезон — /top-players/overall. Ключът 'saves' захранва секцията
    "Вратари — спасявания на мач" в отчета (2026-09-19)."""
    return _cached_json(
        f"https://api.sofascore.com/api/v1/unique-tournament/{tournament_id}/season/{season_id}/top-players/overall",
        f"toppl_{tournament_id}_{season_id}", TOP_PLAYERS_TTL)


def fetch_missing_players(event_id: int) -> tuple[set[str], set[str]]:
    """(домакин липсващи, гост липсващи) имена, от Sofascore
    /event/{id}/lineups missingPlayers. Празни сетове при грешка/липса
    (напр. твърде рано преди мача — виж докстринга на модула).

    ПОПРАВКА (2026-07): status_code!=200 остава тихо (често очаквано —
    невалиден/твърде ранен event_id), но НЕПРЕДВИДЕНА грешка (мрежа, счупен
    JSON, сменена Sofascore схема) вече отпечатва предупреждение — преди
    цялата функционалност можеше тихо да замлъкне при промяна на API-то,
    без никой да разбере."""
    try:
        data = lineups_json(event_id)   # кеширано, виж _cached_json()
        if data is None:
            return set(), set()
        home = {p["player"]["name"] for p in data.get("home", {}).get("missingPlayers", [])}
        away = {p["player"]["name"] for p in data.get("away", {}).get("missingPlayers", [])}
        return home, away
    except Exception as e:
        print(f"⚠ fetch_missing_players({event_id}) грешка: {e}")
        return set(), set()


# Sofascore `description` е човешки четим текст за контузии ("Thigh Injury"),
# но суров snake_case код за наказания ("red_card_suspension") — няма официален
# речник, затова превеждаме познатите кодове ръчно; непознат код пада на общото
# "почистване" (долни черти → интервали, главна буква) вместо суров ъндърскор низ.
REASON_LABELS = {
    "red_card_suspension": "наказание (червен картон)",
    "yellow_card_accumulation_suspension": "наказание (натрупани жълти картони)",
    "suspension": "наказание",
}


def _humanize_reason(raw: str | None) -> str:
    if not raw:
        return "неизвестна причина"
    if raw in REASON_LABELS:
        return REASON_LABELS[raw]
    if "_" in raw and raw.islower():
        return raw.replace("_", " ")
    return raw


def fetch_missing_players_detailed(event_id: int) -> tuple[list[dict], list[dict]]:
    """(домакин липсващи, гост липсващи) — разширена версия на
    fetch_missing_players() (2026-08-09, по искане на потребителя: "искам
    missingPlayers да се взима предвид, така ще знаем ако добър играч е
    контузен") — пази позиция и причина (Sofascore `description`, напр.
    "Thigh Injury"/"Unknown", или suspension код — виж _humanize_reason), не
    само името, за да може next_fixtures.py да филтрира по значимост
    (титуляр или резерва) и да покаже причината в отчета. Всеки елемент:
    {"name", "position", "reason"}."""
    try:
        data = lineups_json(event_id)   # кеширано, виж _cached_json()
        if data is None:
            return [], []

        def parse(side):
            out = []
            for p in side.get("missingPlayers", []):
                pl = p.get("player", {})
                out.append({"name": pl.get("name"), "position": pl.get("position"),
                            "reason": _humanize_reason(p.get("description"))})
            return out

        return parse(data.get("home", {})), parse(data.get("away", {}))
    except Exception as e:
        print(f"⚠ fetch_missing_players_detailed({event_id}) грешка: {e}")
        return [], []


def fetch_lineup_starters_detailed(event_id: int) -> tuple[dict[str, tuple[int, str]], dict[str, tuple[int, str]], bool]:
    """(домакин {име: (sofa_player_id, позиция)}, гост {...}, confirmed) —
    като fetch_lineup_starters() по-долу, но пази и Sofascore ID-то и
    позицията директно от /event/{id}/lineups отговора (2026-08-28, по
    искане на потребителя за Модел В v2: "атрибутите няма да се променят
    драстично за 1 мач... нека няма значение дали играч има изигран мач").

    И двете полета (id/position) идват направо от Sofascore за ТОЗИ мач —
    НЕ от нашата локална player_match история. Позволява на Модел В v2 да
    тегли атрибутите на ВСЕКИ титуляр (вкл. чисто нов трансфер без нито
    един записан мач при нас), без да минава през local lookup, който
    преди отпадаше играча само защото ID-то му не е било известно локално."""
    try:
        data = lineups_json(event_id)   # cached, see _cached_json()
        if data is None:
            return {}, {}, False

        def starters(side):
            out = {}
            for p in side.get("players", []):
                if p.get("substitute", False):
                    continue
                pl = p.get("player", {})
                name, pid, pos = pl.get("name"), pl.get("id"), pl.get("position")
                if name and pid:
                    out[name] = (pid, pos)
            return out

        home = starters(data.get("home", {}))
        away = starters(data.get("away", {}))
        return home, away, bool(data.get("confirmed", False))
    except Exception as e:
        print(f"⚠ fetch_lineup_starters_detailed({event_id}) грешка: {e}")
        return {}, {}, False


def fetch_lineup_starters(event_id: int) -> tuple[set[str], set[str], bool]:
    """(домакин титуляри, гост титуляри, confirmed) — 11-те стартиращи имена
    (substitute=False) от Sofascore /event/{id}/lineups. Работи и преди
    официално потвърждение — Sofascore показва прогнозен състав по-рано
    (виж докстринга: потвърдено на реален мач деня преди началото). Празни
    сетове, ако Sofascore още няма и прогнозен състав (твърде рано).

    Тънка обвивка над fetch_lineup_starters_detailed() (2026-08-28) — само
    имената, за случаите (Модел В v1, Модел Б/Г, missing players), които не
    се нуждаят от Sofascore ID/позиция. Едно и също HTTP извикване, не две."""
    home, away, confirmed = fetch_lineup_starters_detailed(event_id)
    return set(home), set(away), confirmed

LINEUP_K = 0.25
RATING_RECENT_W, RATING_SEASON_W = 0.6, 0.4
MIDFIELD_MIN_MATCHES = 3  # под това — твърде малко данни да класифицираме, третираме като "attack" по подразбиране
STARTER_THRESHOLD = 0.5  # дял от мачовете на отбора, в които трябва да е стартирал, за да е "титуляр"


def _team_match_count(con: sqlite3.Connection, team: str, as_of: pd.Timestamp) -> int:
    row = con.execute(
        "SELECT COUNT(*) FROM matches WHERE (home = ? OR away = ?) AND date < ? "
        "AND hg IS NOT NULL",
        (team, team, as_of.strftime("%Y-%m-%d"))).fetchone()
    return row[0] if row else 0


def _load_player_matches(con: sqlite3.Connection, team: str, as_of: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT pm.*, m.date FROM player_match pm JOIN matches m ON pm.match_id = m.match_id "
        "WHERE pm.team = ? AND m.date < ? AND pm.started = 1",
        con, params=(team, as_of.strftime("%Y-%m-%d")))
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def missing_regular_starters(con: sqlite3.Connection, team: str, as_of: pd.Timestamp,
                             missing: list[dict]) -> list[dict]:
    """Филтрира `missing` (от fetch_missing_players_detailed) до ОБИЧАЙНИТЕ
    титуляри (>=STARTER_THRESHOLD старта до момента — титуляр контузен е
    важен сигнал, резерва контузен е шум, огледално на прага в
    _titular_groups()) и прикача блендиран рейтинг (виж _blended_rating -
    60% последни 5 мача / 40% сезонен среден) на всеки — за да може Модел Г
    да тежи Състав/контузии по КАЧЕСТВОТО на отсъстващия, не само по броя
    (загубата на 8.0-рейтингов играч > загубата на 6.5-рейтингов).

    ЕДНО зареждане на играчовите мачове на отбора (2026-08-09, консолидация
    — преди имаше отделна is_regular_starter() + player_rating() функция,
    всяка с СВОЯ _load_player_matches() заявка на играч; за отбор с 2-3
    отсъстващи това бяха 4-6 отделни SQL четения на СЪЩИТЕ данни вместо 1)."""
    df = _load_player_matches(con, team, as_of)
    if df.empty:
        return []
    team_matches = _team_match_count(con, team, as_of)
    if team_matches == 0:
        return []
    out = []
    for m in missing:
        name = m.get("name")
        if not name:
            continue
        rows = df[df["name"] == name]
        if len(rows) / team_matches < STARTER_THRESHOLD:
            continue
        out.append({**m, "rating": _blended_rating(rows) if not rows.empty else None})
    return out


def _sofa_attack_defense(player_id: int) -> tuple[float, float] | None:
    """(attacking, defending) от Sofascore /player/{id}/attribute-overviews,
    текущ сезон (yearShift=0). None ако липсва/грешка — вика се само за
    класификация на халфове, не е критичен път."""
    try:
        data = player_attributes_json(player_id)   # кеширано, виж _cached_json()
        if data is None:
            return None
        for ov in data.get("playerAttributeOverviews", []):
            if ov.get("yearShift") == 0 and ov.get("position") == "M":
                return float(ov["attacking"]), float(ov["defending"])
    except Exception as e:
        print(f"⚠ _sofa_attack_defense({player_id}) грешка: {e}")
        return None
    return None


def _classify_midfielder(rows: pd.DataFrame) -> str:
    """rows = този играч, всичките му мачове тази поредица. Връща 'attack'/'defense'.

    Първо опитва директния Sofascore attribute-overview (defending срещу
    attacking — по-авторитетен сигнал, потвърден срещу реален екранен пример
    от потребителя). Пада обратно на статистическата евристика
    (tackles+interceptions+recoveries срещу key_passes+assists+xa), ако
    Sofascore нямат данни за играча."""
    player_id = rows["sofa_player_id"].iloc[0] if "sofa_player_id" in rows else None
    if player_id is not None:
        att_def = _sofa_attack_defense(int(player_id))
        if att_def is not None:
            attacking, defending = att_def
            return "defense" if defending > attacking else "attack"

    defensive = rows[["tackles", "interceptions", "recoveries"]].fillna(0).sum().sum()
    attacking = rows[["key_passes", "assists", "xa"]].fillna(0).sum().sum()
    return "defense" if defensive > attacking else "attack"


def _player_group(position: str, rows: pd.DataFrame) -> str | None:
    """ПОПРАВКА (2026-07): вратарите (position == 'G') преди се връщаха None
    — изобщо не участваха в defense_index на v1 и в DC lineup-adjustment
    фийчъра. Смяна на титулярен вратар е един от най-силните единични
    ефекти във футбола (различна школа, различен reach/reflexes) — сега
    вратарят участва изцяло в защитната група (тежи 100% там, за разлика
    от халфовете, които се делят пропорционално)."""
    if position == "F":
        return "attack"
    if position in ("D", "G"):
        return "defense"
    if position == "M":
        if len(rows) < MIDFIELD_MIN_MATCHES:
            return "attack"  # недостатъчно данни — по подразбиране (атакуващ профил, по-честия случай)
        return _classify_midfielder(rows)
    return None  # непозната позиция — не участва


def _attack_weight(position: str, rows: pd.DataFrame) -> float:
    """Дял (0.0-1.0) от собствения принос на играча, който да тежи в АТАКАТА
    на отбора (остатъкът, 1-тегло, тежи в защитата) — ЗАМЕНЯ бинарното
    _player_group() за v1's team_indices()/team_regular_indices() (2026-09-04,
    по случая ван Дайк: защитник, но реален гол-заплаха при статични положения
    — "то значи защитник не може да вкара гол, което е супер невярно").

    _player_group() по-горе гейтва D/G изцяло към "defense" — нула принос в
    attack_index, каквото и да им е реалното xg/xa. simulate_lineup_v2.py вече
    реши ТОЧНО този проблем за v2 (виж team_indices() там, случаят Sean
    Longstaff) — тежи всеки играч пропорционално на собствения му
    attacking/defending дял, никой не е 100%/0% бинарно. Тук е същия принцип,
    пренесен към v1's различни сигнали (xg/xa за атака, blended rating за
    защита — не directly сравними числа като v2's Sofascore ATT/DEF 0-99
    атрибути, затова тежестта не може директно да копира v2's формула).

    Позиционен приор + статистическа корекция (същия tackles+interceptions+
    recoveries vs key_passes+assists+xa сигнал като _classify_midfielder()
    по-горе, но НЕПРЕКЪСНАТ вместо бинарен, и приложен към ВСЯКА позиция —
    не само халфове). Shrinkage към приора при малка извадка (1-2 мача,
    напр. Гравенберх/Муньос по-горе) — same k=3 конвенция като SHRINK_K в
    simulate_lineup_v1.py, за да не доминира един-единствен шумен мач."""
    prior = _ATTACK_WEIGHT_PRIOR.get(position, 0.5)
    defensive_score = rows[["tackles", "interceptions", "recoveries"]].fillna(0).sum().sum()
    attacking_score = rows[["key_passes", "assists", "xa"]].fillna(0).sum().sum()
    total = defensive_score + attacking_score
    if total <= 0:
        return prior
    raw_weight = attacking_score / total
    n = len(rows)
    k = 3.0
    shrink = n / (n + k)
    return shrink * raw_weight + (1 - shrink) * prior


def _blended_rating(rows: pd.DataFrame) -> float | None:
    rated = rows.dropna(subset=["rating"])
    if rated.empty:
        return None
    # ПОПРАВКА (2026-07): season_avg преди включваше и последните 5 мача
    # (същите, взети пак в recent_avg) — реалното тегло на последните 5
    # излизаше по-високо от документираните RATING_RECENT_W/SEASON_W
    # (за 20 оценени мача: ~0.7 вместо 0.6). Сега season/older е
    # НЕПРЕСИЧАЩ СЕ остатък, огледално на Form групата в model_b_heuristic.py.
    recent = rated.tail(5)
    older = rated.iloc[:-5] if len(rated) > 5 else rated.iloc[0:0]
    recent_avg = recent["rating"].mean()
    if older.empty:
        return recent_avg
    return RATING_RECENT_W * recent_avg + RATING_SEASON_W * older["rating"].mean()


def _classify_all_players(df: pd.DataFrame) -> dict[str, tuple[str, float]]:
    """{име: (група, рейтинг)} за ВСЕКИ играч в df (без праг за титулярство) —
    нужно за оценка на реалния/прогнозния състав, който може да включва
    резерви извън обичайните титуляри."""
    out = {}
    for name, rows in df.groupby("name"):
        position = rows["position"].mode().iat[0] if not rows["position"].mode().empty else None
        group = _player_group(position, rows)
        if group is None:
            continue
        rating = _blended_rating(rows)
        if rating is None:
            continue
        out[name] = (group, rating)
    return out


def _titular_groups(df: pd.DataFrame, team_matches: int) -> dict[str, list[tuple[str, float]]]:
    """Обичайните титуляри (>=STARTER_THRESHOLD старта), групирани — базовата
    линия ('нормалният' състав), срещу която сравняваме реалния/прогнозния."""
    groups: dict[str, list[tuple[str, float]]] = {"attack": [], "defense": []}
    for name, rows in df.groupby("name"):
        if len(rows) / team_matches < STARTER_THRESHOLD:
            continue
        position = rows["position"].mode().iat[0] if not rows["position"].mode().empty else None
        group = _player_group(position, rows)
        if group is None:
            continue
        rating = _blended_rating(rows)
        if rating is None:
            continue
        groups[group].append((name, rating))
    return groups


def lineup_factors_from_starters(con: sqlite3.Connection, team: str, as_of: pd.Timestamp,
                                 starting_names: set[str]) -> dict[str, float]:
    """По-прецизен метод — сравнява групов среден рейтинг на ПРОГНОЗНИЯ/
    ПОТВЪРДЕНИЯ старов състав (starting_names, от fetch_lineup_starters)
    срещу обичайния (титулярния) групов среден. Хваща и колко добър е бил
    отсъстващия, И колко силен/слаб е заместникът — в едно сравнение,
    без да се налага 1-към-1 съпоставяне кой кого замества."""
    df = _load_player_matches(con, team, as_of)
    if df.empty or not starting_names:
        return {"attack": 1.0, "defense": 1.0}

    team_matches = _team_match_count(con, team, as_of)
    if team_matches == 0:
        return {"attack": 1.0, "defense": 1.0}

    normal_groups = _titular_groups(df, team_matches)
    all_players = _classify_all_players(df)

    factors = {}
    for group_name, normal_players in normal_groups.items():
        if not normal_players:
            factors[group_name] = 1.0
            continue
        normal_avg = sum(r for _, r in normal_players) / len(normal_players)

        actual = [all_players[n][1] for n in starting_names
                 if n in all_players and all_players[n][0] == group_name]
        if not actual:
            factors[group_name] = 1.0  # никой разпознат играч от групата в състава — няма данни
            continue
        actual_avg = sum(actual) / len(actual)

        delta = normal_avg - actual_avg  # положително = по-слаб от обичайното
        factor = 1 - LINEUP_K * delta
        # ПОПРАВКА (2026-07): преди клампваше горницата на 1.0 вместо
        # документирания LINEUP_CLAMP[1]=1.25 — за разлика от lineup_factors()
        # (fallback методът по-долу, само липсващи играчи), тук съставът
        # МОЖЕ легитимно да е по-силен от обичайния (напр. най-добрият
        # състав изваден за важен мач), не само по-слаб. min(1.0,...) тихо
        # изяждаше точно тази положителна корекция.
        factors[group_name] = max(LINEUP_CLAMP[0], min(LINEUP_CLAMP[1], factor))
    return factors


def lineup_factors(con: sqlite3.Connection, team: str, as_of: pd.Timestamp,
                   missing_names: set[str]) -> dict[str, float]:
    """Fallback метод (по-груб приближение) — само за случаите, в които
    Sofascore още няма никакъв състав (нито прогнозен) да покаже, само
    списък на контузени/наказани имена. Предпочитай
    lineup_factors_from_starters(), когато има 11-членен състав наличен."""
    df = _load_player_matches(con, team, as_of)
    if df.empty:
        return {"attack": 1.0, "defense": 1.0}

    team_matches = _team_match_count(con, team, as_of)
    if team_matches == 0:
        return {"attack": 1.0, "defense": 1.0}

    groups = _titular_groups(df, team_matches)

    factors = {}
    for group_name, players in groups.items():
        active = [(n, r) for n, r in players if n not in missing_names]
        missing_in_group = [(n, r) for n, r in players if n in missing_names]
        if not missing_in_group or not active:
            factors[group_name] = 1.0
            continue
        avg_active = sum(r for _, r in active) / len(active)
        combined = 1.0
        for _, rating in missing_in_group:
            delta = rating - avg_active
            combined *= (1 - LINEUP_K * delta)  # неклампнато поединично — виж докстринга
        # таван 1.0 — загуба на титуляр никога не подобрява отбора, само
        # неутрална в най-добрия случай (дори ако е под средното на групата)
        factors[group_name] = max(LINEUP_CLAMP[0], min(1.0, combined))
    return factors
