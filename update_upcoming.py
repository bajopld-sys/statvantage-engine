"""Poller за ЕДИНИЧНИ мачове (2026-09-19, по искане на потребителя: "за
всеки мач 50мин преди началото да се обновява сайта, а не за всички лиги
едновременно") — вместо да чакаме ръчно "обнови лигите" накуп, тегли само
онова, което реално е близо до началото си.

Пуска се от Windows Task Scheduler на всеки 15 минути (виж бележката в
дъното за самата задача). За ВСЯКА активна лига (LEAGUE_SOFA_IDS от
make_team_report.py) проверява предстоящия кръг и за мач, чието начало
пада в прозореца [сега+45мин, сега+60мин] (засича "~50 минути преди" с
15-минутна стъпка на пипера, без пропуск между две преминавания) — топли
живия кеш на състава/атрибутите му (lineup_impact.py) и го маркира като
обработен.

ВАЖНО за цената: самото сканиране на фикстурите е БЕЗПЛАТНО (0 заявки към
Sofascore) — next_fixtures._next_round_fixtures() чете кешираните кръгови
JSON-и от data/raw_sofa/rounds/, пълнени от fetch_sofa.py при "обнови
лигите", не удря живото API. Само когато мач РЕАЛНО влезе в 50-минутния
прозорец, топлим неговия конкретен състав (1 заявка) + играчови атрибути
(~15-22 заявки) — точно същият _cached_json механизъм, който пази системата
от повторния 403 блок от 2026-09-08 (виж api-cost-discipline паметта).

Идемпотентност: обработените event_id-та се пазят в WARMED_STATE_PATH
(JSON, event_id -> unix ts на обработката), за да не топлим един и същ мач
на всяко от 3-4-те преминавания на пипера през неговия прозорец. Записите
по-стари от STATE_TTL (24ч) се чистят автоматично.

Регенерира+публикува отчета САМО ако нещо реално е било топлено този цикъл
(вика make_team_report.py + update_all.publish(), same as "обнови лигите")
— ако прозорецът е празен, скриптът не прави нищо (нулева цена).

Настройка на Windows Task Scheduler (веднъж, ръчно):
    schtasks /create /tn "StatVantage поглед по мач" /sc minute /mo 15 ^
      /tr "cmd /c cd /d <path-to-engine> && python update_upcoming.py >> <DATA_DIR>\\update_upcoming.log 2>>&1" ^
      /rl highest
Логът се пише в data/update_upcoming.log (във DATA_DIR) — прочети го при съмнение.

Употреба (ръчно тестване): python engine/update_upcoming.py
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from next_fixtures import _next_round_fixtures
from lineup_impact import lineups_json, player_attributes_json
from make_team_report import LEAGUE_SOFA_IDS

from config import DATA_DIR
WARMED_STATE_PATH = DATA_DIR / "raw_sofa" / "live" / "_upcoming_warmed.json"
WINDOW_LOW = 45 * 60   # секунди — не по-рано от 45 мин преди началото
WINDOW_HIGH = 60 * 60  # секунди — не по-късно от 60 мин преди началото
STATE_TTL = 24 * 3600  # прочиствай обработени записи по-стари от 24ч


def _load_state() -> dict:
    if WARMED_STATE_PATH.exists():
        try:
            return json.loads(WARMED_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    WARMED_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    WARMED_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _warm_match(ev: dict) -> None:
    """Топли състава на мача + атрибутите на всеки играч в него (best-effort,
    lineups_json/player_attributes_json вече поглъщат грешки поединично —
    виж lineup_impact.py докстринга за stale-fallback поведението)."""
    data = lineups_json(ev["id"])
    if not data:
        return
    for side in ("home", "away"):
        for p in data.get(side, {}).get("players", []):
            pid = p.get("player", {}).get("id")
            if pid:
                player_attributes_json(pid)


def main() -> None:
    now = datetime.datetime.now(datetime.UTC).timestamp()
    state = _load_state()
    state = {k: v for k, v in state.items() if now - v < STATE_TTL}

    warmed = []
    for league, (tour, season) in LEAGUE_SOFA_IDS.items():
        try:
            events = _next_round_fixtures(tour, season)
        except Exception as exc:
            print(f"⚠ {league}: сканирането на кръга се провали — {str(exc)[:70]}")
            continue
        for ev in events:
            eid = str(ev["id"])
            ts = ev.get("startTimestamp", 0)
            delta = ts - now
            if WINDOW_LOW <= delta <= WINDOW_HIGH and eid not in state:
                home = ev.get("homeTeam", {}).get("name", "?")
                away = ev.get("awayTeam", {}).get("name", "?")
                print(f"[{league}] топля {home} - {away} (старт след {delta/60:.0f} мин)")
                _warm_match(ev)
                state[eid] = now
                warmed.append(f"{league}: {home} - {away}")

    _save_state(state)

    if not warmed:
        print("Нищо в 45-60 мин прозореца в момента — няма нищо за правене.")
        return

    print(f"\nТоплени {len(warmed)} мач(а), регенерирам и публикувам отчета...")
    engine_dir = Path(__file__).resolve().parent
    subprocess.run([sys.executable, "make_team_report.py"], cwd=engine_dir, check=True)

    from update_all import publish
    publish()
    print("Готово.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
