#!/usr/bin/env python3
"""
Сервис-обёртка: запускает синхронизацию (main.main) каждые N минут и
корректно останавливается по SIGINT/SIGTERM (docker stop, Ctrl+C).

Поведение при остановке:
  - сигнал во время ожидания — завершаемся сразу;
  - сигнал во время синхронизации — текущий запуск доезжает до конца
    (не бросаем роутеры в полуприменённом состоянии), затем выходим;
  - повторный сигнал — немедленный выход.

Запуск:
    python service.py -c config.json5 -i 5
    python service.py -c config.json5 --interval 5 --device gw-branch -v

Переменные окружения: SYNC_INTERVAL_MINUTES (используется, если -i не задан;
можно положить в .env — уже установленные переменные окружения приоритетнее).
Код возврата: последний код возврата синхронизации (0/1/2/3) либо 0, если
остановились во время ожидания до первого запуска.
"""

import argparse
import logging
import os
import signal
import sys
import threading

from dotenv import load_dotenv

import main as dns_sync

LOG = logging.getLogger("dns-sync.service")

STOP = threading.Event()
STOP_SIGNUM: list[int] = []


def _handle_signal(signum: int, frame) -> None:
    del frame  # неиспользуется
    if STOP.is_set():
        # повторный сигнал — принудительный выход
        LOG.warning("повторный сигнал %s — немедленный выход", signum)
        sys.exit(130)
    STOP_SIGNUM.clear()
    STOP_SIGNUM.append(signum)
    STOP.set()
    # если сейчас идёт синхронизация — лишь просигнализируем; она доедет
    # и цикл завершится. Если спим в wait() — проснёмся и выйдем.


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Периодический запуск mikrotik-dns-sync с корректной "
                    "обработкой SIGINT/SIGTERM",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-c", "--config", default="config.json5",
                        help="путь к конфигурации JSON5 (передаётся в main.py)")
    parser.add_argument("-i", "--interval", type=float,
                        default=None, metavar="MINUTES",
                        help="интервал между запусками в минутах "
                             "(или переменная SYNC_INTERVAL_MINUTES)")
    parser.add_argument("--device", action="append", metavar="NAME",
                        help="передать в main.py --device (можно повторять)")
    parser.add_argument("--force", action="store_true",
                        help="передать в main.py --force")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="передать в main.py -v")
    return parser.parse_args(argv)


def build_sync_argv(args: argparse.Namespace) -> list[str]:
    argv = ["-c", args.config]
    if args.device:
        for name in args.device:
            argv += ["--device", name]
    if args.force:
        argv.append("--force")
    if args.verbose:
        argv.append("-v")
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # .env подгружается до чтения SYNC_INTERVAL_MINUTES и передачи
    # управления в main.main (его password_env); окружение приоритетнее .env.
    load_dotenv()

    interval = args.interval
    if interval is None:
        try:
            interval = float(os.environ.get("SYNC_INTERVAL_MINUTES", "5"))
        except ValueError:
            interval = 5.0
    if interval <= 0:
        LOG.critical("интервал должен быть положительным (минуты), получено %r",
                     interval)
        return 3

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    sighup = getattr(signal, "SIGHUP", None)
    for sig in (signal.SIGINT, signal.SIGTERM, sighup):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # сигнал недоступен на этой платформе/потоке

    sync_argv = build_sync_argv(args)
    LOG.info("сервис запущен: интервал %.1f мин, %s",
             interval, " ".join(sync_argv))

    last_rc = 0
    first = True
    while not STOP.is_set():
        if not first:
            LOG.info("ожидание %.1f мин до следующего запуска", interval)
            if STOP.wait(interval * 60):
                break
        first = False

        LOG.info("=== запуск синхронизации ===")
        try:
            last_rc = dns_sync.main(sync_argv)
        except SystemExit as exc:
            last_rc = int(exc.code or 0)
        if STOP.is_set():
            break
        if last_rc == 3:
            # ошибка конфигурации не исправится сама — падаем сразу,
            # чтобы оркестратор (restart: unless-stopped / on-failure)
            # перезапустил нас с задержкой
            LOG.critical("ошибка конфигурации (код 3) — сервис останавливается")
            return 3

    if STOP_SIGNUM:
        LOG.info("получен сигнал %s — корректная остановка", STOP_SIGNUM[0])
    else:
        LOG.info("остановка")
    return last_rc


if __name__ == "__main__":
    sys.exit(main())
