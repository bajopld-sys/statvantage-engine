"""Снима прогнозите на всички модели за предстоящия кръг във всяка лига, преди
мачовете да са изиграни - виж model_predictions_log.py докстринга за защо.

Ползва СЪЩИЯ build_comparison() като make_team_report.py (next_fixtures.py),
затова е логично да се пуска непосредствено до make_team_report.py (виж
update_all.py). UNIQUE(event_id, model) в схемата прави повторно пускане
преди кръга да приключи безопасно - вече записаните прогнози не се презаписват
(нарочно - искаме "какво е казал модела ПРЕДИ мача", не последната преизчислена
версия ден по-късно).

Употреба:
    python engine/record_predictions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from next_fixtures import build_comparison
from model_predictions_log import connect, MODELS, MODEL_LABELS
from make_team_report import LEAGUES, LEAGUE_SOFA_IDS

import datetime


SIDE_PICK_THRESHOLD = 0.41  # по искане на потребителя (2026-08-09): "Нека да се взима
# страна (1 или 2) само когато % е над 41%" — конкретен пример, довел до промяната:
# 34%/30%/36% преди се записваше като уверен pick "2" в model_predictions.db, а е
# практически равностойно на 1/X с разлика от 2пп. Огледално на LOW_CONFIDENCE_
# THRESHOLD в make_team_report.py (флага на "Средно" реда), но тук РЕШАВА какво се
# записва/грейдва, не само дисплей предупреждение.


def _pick_1x2_from_probs(p_home: float, p_draw: float, p_away: float) -> str:
    """Взима страна (1/2) само ако реално мине прага — иначе "X" (равен) като
    fallback, дори да не е реално най-високото число (изрично избрано от
    потребителя пред "без pick"/изключване от track record)."""
    if p_home > SIDE_PICK_THRESHOLD or p_away > SIDE_PICK_THRESHOLD:
        return "1" if p_home >= p_away else "2"
    return "X"


OU_BTTS_THRESHOLD = 0.55  # по искане на потребителя (2026-08-09): "за да се взима залога
# за тези 2 пазара [Над2.5/ГГ] трябва да е над 55%" — конкретен пример: Under 52% (реално
# Over 48%) преди се записваше като уверен pick "Under", а разликата спрямо 50/50 е
# незначителна. За разлика от 1X2 (има реален трети изход "X"), тук няма естествен
# fallback — под прага НЯМА pick изобщо (None), мачът просто не се брои в hit/miss за
# този пазар (виж resolve_predictions.py/track_record_html — вече игнорират None picks).


def _pick_binary(p: float | None, above_label: str, below_label: str) -> str | None:
    if p is None:
        return None
    if p > OU_BTTS_THRESHOLD:
        return above_label
    if p < 1 - OU_BTTS_THRESHOLD:
        return below_label
    return None


def _pick_from_da_ne(pick: str | None, yes_label: str, no_label: str) -> str | None:
    """Модел Б-то "ДА"/"НЕ"/None -> нашите етикети; None остава None (без pick)."""
    if pick is None:
        return None
    return yes_label if pick == "ДА" else no_label


def _extract(model: str, m: dict | None) -> dict | None:
    """Нормализира разнородните model dict-ове (DC/v1/v2/model_g имат
    p_home/p_draw/p_away, Модел Б няма вероятности - само рангови pick-ове)
    до общ набор колони за model_predictions таблицата."""
    if m is None:
        return None
    if model == "model_b":
        return {
            "p_home": None, "p_draw": None, "p_away": None,
            "pick_1x2": m["pick_1x2"],  # вече "1"/"X"/"2"
            # ПОПРАВКА (2026-09-04): Модел Б вече може да върне pick=None
            # (неутрална зона — виж MARKET_NEUTRAL_BAND в model_b_heuristic.py).
            # Преди `== "ДА" else "UNDER"` мълчаливо превръщаше липсата на pick
            # в уверен pick за ПРОТИВОПОЛОЖНАТА страна.
            "p_over25": None, "pick_over25": _pick_from_da_ne(m["over25"]["pick"], "OVER", "UNDER"),
            "p_btts": None, "pick_btts": _pick_from_da_ne(m["btts"]["pick"], "YES", "NO"),
        }
    p_home, p_draw, p_away = m["p_home"], m["p_draw"], m["p_away"]
    p_over25 = m.get("p_over25")
    p_btts = m.get("p_btts")
    return {
        "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
        "pick_1x2": _pick_1x2_from_probs(p_home, p_draw, p_away),
        "p_over25": p_over25, "pick_over25": _pick_binary(p_over25, "OVER", "UNDER"),
        "p_btts": p_btts, "pick_btts": _pick_binary(p_btts, "YES", "NO"),
    }


def record_league(con, league: str, db_path: Path, tournament_id: int, season_id: int) -> int:
    rows = build_comparison(league, db_path, tournament_id, season_id, max_matches=16)
    recorded_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    inserted = 0
    for r in rows:
        # часът на още неизигран мач се обновява при всяко пускане (2026-10-26): INSERT OR IGNORE
        # пазеше първия записан час, а FotMob дава временен час, преди мачът да е насрочен
        con.execute("UPDATE model_predictions SET match_date=? WHERE event_id=? AND status='pending'",
                    (r["date"], r["event_id"]))
        for model in MODELS:
            data = _extract(model, r.get(model))
            if data is None:
                continue
            cur = con.execute(
                """INSERT OR IGNORE INTO model_predictions
                   (recorded_at, league, event_id, home, away, match_date, model,
                    p_home, p_draw, p_away, pick_1x2, p_over25, pick_over25, p_btts, pick_btts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (recorded_at, league, r["event_id"], r["home"], r["away"], r["date"], model,
                 data["p_home"], data["p_draw"], data["p_away"], data["pick_1x2"],
                 data["p_over25"], data["pick_over25"], data["p_btts"], data["pick_btts"]),
            )
            inserted += cur.rowcount
    return inserted


def main() -> None:
    con = connect()
    total = 0
    for league, db_path in LEAGUES:
        if league not in LEAGUE_SOFA_IDS:
            continue
        tid, sid = LEAGUE_SOFA_IDS[league]
        if not db_path.exists():
            continue
        n = record_league(con, league, db_path, tid, sid)
        print(f"{league}: {n} нови записа")
        total += n
    con.commit()
    con.close()
    print(f"\nОбщо нови записа: {total}" if total else "\nНяма нови мачове/модели за записване (вече заснети).")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
