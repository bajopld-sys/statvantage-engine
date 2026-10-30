"""ЕДНА команда за обновяване на сайта през FotMob (2026-10-03), докато Sofascore
е блокиран и `update_all.py` не върши работа (той вика fetch_sofa/parse_sofa_api).

    python engine/update_fotmob.py

Стъпки (същият ред като update_all.py, но без Sofascore тегленето):
  1. fotmob_adapter.py --merge   нови завършени мачове -> живите sofa_<лига>.db
                                 (тегли САМО липсващите; всичко останало е от кеша)
  2. resolve_picks.py            сверява чакащите picks с новите резултати
  3. resolve_predictions.py      сверява записаните прогнози на моделите
  4. build_opta_elo.py           опреснява Opta-Elo рейтингите
  5. make_team_report.py         нов отчет (контузени/състави от FotMob резервата)
  6. публикуване (GitHub Pages)  както в update_all.publish()

По подразбиране НЕ записва нови прогнози и НЕ праща Telegram алерта — без прогнозни
състави track record-ът се замърсява. Добави --record, когато искаш и тях
(record_predictions.py + top_picks_alert.py).

    --max-new N    таван на НОВИТЕ заявки към FotMob за пускане (по подр. 150)
    --no-publish   не качва на сайта (само локално)
    --record       + record_predictions.py и top_picks_alert.py
    --rebuild      пресметни отчета наново за ВСИЧКИ лиги (иначе се ползва кешът на
                   пресметнатите данни за лиги с непроменена база, ≤45 мин. стар)
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_all import run, publish   # noqa: E402  (run() прекратява при грешка)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=150)
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    py = sys.executable
    os.chdir(Path(__file__).resolve().parent)   # update_all.run() пуска в текущата папка

    print("===== 1/5 FotMob -> нови мачове в базите =====")
    run([py, "fotmob_adapter.py", "--merge", "--max-new", str(args.max_new)])

    print("\n===== 2/5 Чакащи picks =====")
    run([py, "resolve_picks.py"])

    print("\n===== 3/5 Сверяване на прогнозите на моделите =====")
    run([py, "resolve_predictions.py"])

    print("\n===== 4/5 Opta-Elo =====")
    run([py, "build_opta_elo.py"])

    print("\n===== 5/5 Отчет =====")
    since_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run([py, "make_team_report.py"] + (["--rebuild"] if args.rebuild else []))

    if args.record:
        print("\n===== Запис на прогнозите + Telegram топ5 =====")
        run([py, "record_predictions.py"])
        run([py, "top_picks_alert.py", "--since", since_ts], check=False)

    if args.no_publish:
        print("\nГотово (без публикуване): team_report.html е обновен локално.")
    else:
        publish()
        print("\nГотово — сайтът е обновен.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
