from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .auth import DEFAULT_TIMEOUT, authenticate
from .errors import VkpwError
from .storage import append_entry, build_entry

logger = logging.getLogger("vkpw")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vkpw",
        description="Получение cookies ВКонтакте через браузерный вход по логину и паролю.",
        epilog="Для установки Chromium, если нет Chrome/Edge/Brave: vkpw install-browser",
    )
    parser.add_argument("-o", "--output", metavar="FILE", help="JSON-файл для cookies")
    parser.add_argument(
        "-l",
        "--log-file",
        metavar="FILE",
        help="файл для журнала этапов входа",
    )
    parser.add_argument("--login", help="Логин (пароль безопасно запрашивается отдельно)")
    parser.add_argument(
        "credentials",
        nargs="?",
        metavar="LOGIN:PASSWORD",
        help="Логин и пароль одной строкой (требует явный -o/--output)",
    )
    parser.add_argument(
        "--browser-path",
        metavar="PATH",
        help="Путь к установленному Chromium/Chrome/Edge/Brave",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Таймаут шагов входа в секундах (по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _parse_credentials(value: str) -> tuple[str, str]:
    login, separator, password = value.partition(":")
    if not separator:
        raise VkpwError("Ожидается формат LOGIN:PASSWORD")

    login = login.strip()
    password = "".join(password.split())
    if not login or not password:
        raise VkpwError("Логин и пароль не должны быть пустыми")
    return login, password


def _read_line(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        return input().strip()
    except EOFError:
        raise VkpwError("Не удалось прочитать ввод из терминала") from None


def _install_browser() -> int:
    print("Устанавливаю Chromium для Playwright…", file=sys.stderr, flush=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
        )
    except OSError as exc:
        print(f"Ошибка: не удалось запустить установщик Playwright: {exc}", file=sys.stderr)
        return 1
    if result.returncode:
        print("Ошибка: Playwright не смог установить Chromium", file=sys.stderr)
        return result.returncode
    print("Chromium установлен.", file=sys.stderr)
    return 0


async def _run_login(args: argparse.Namespace, *, login: str, password: str) -> tuple[int, dict[str, str]]:
    session = await authenticate(
        login,
        password,
        browser_path=args.browser_path,
        timeout=args.timeout,
    )
    return session.user_id, session.cookies


def main(argv: Sequence[str] | None = None) -> int:
    _setup_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "install-browser":
        if len(arguments) > 1:
            print("Ошибка: install-browser не принимает аргументы", file=sys.stderr)
            return 2
        return _install_browser()

    parser = build_parser()
    args = parser.parse_args(arguments)
    log_handler = None
    previous_level = logger.level
    previous_propagate = logger.propagate
    if args.log_file:
        try:
            log_handler = logging.FileHandler(
                Path(args.log_file).expanduser(),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Ошибка: не удалось открыть файл журнала: {exc}", file=sys.stderr)
            return 2
        log_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(log_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    try:
        logger.info("Запуск процесса входа")
        if args.timeout <= 0:
            logger.info("Ошибка проверки аргументов: таймаут должен быть больше нуля")
            parser.error("--timeout должен быть больше нуля")
        if args.credentials is not None and args.login is not None:
            logger.info("Ошибка проверки аргументов: переданы оба способа указания логина")
            parser.error("укажите либо LOGIN:PASSWORD, либо --login")
        if args.credentials is not None and args.output is None:
            logger.info("Ошибка проверки аргументов: для LOGIN:PASSWORD нужен файл вывода")
            parser.error("для LOGIN:PASSWORD обязательно явно укажите -o/--output")

        if args.credentials is not None:
            try:
                login, password = _parse_credentials(args.credentials)
            except VkpwError as exc:
                logger.info("Не удалось разобрать учетные данные: %s", exc)
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 2
        else:
            if not sys.stdin.isatty():
                logger.info("Не удалось получить учетные данные: терминал не интерактивный")
                print(
                    "Ошибка: vkpw требует интерактивный терминал для ввода логина и пароля",
                    file=sys.stderr,
                )
                return 2
            login = args.login or _read_line("Логин VK (телефон или почта): ")
            if not login:
                logger.info("Не удалось получить учетные данные: логин не задан")
                print("Ошибка: логин не задан", file=sys.stderr)
                return 2
            password = None

        if password is None:
            password = getpass.getpass("Пароль VK: ")
        logger.info("Начинаю авторизацию VK")
        user_id, cookies = asyncio.run(_run_login(args, login=login, password=password))
        logger.info("Авторизация VK завершена")
        entry = build_entry(cookies, user_id)
        logger.info("Данные сессии прошли проверку перед сохранением")
        target = args.output
        if target is None:
            target = _read_line("Укажите путь к JSON-файлу для cookies: ")
            if not target:
                raise VkpwError("Путь к файлу не задан, cookies не сохранены")
        logger.info("Записываю данные сессии в указанный файл")
        saved = append_entry(Path(target), entry)
        logger.info("Cookies успешно сохранены")
    except VkpwError as exc:
        logger.info("Ошибка в процессе входа: %s", exc)
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.info("Вход отменён пользователем")
        print("\nВход отменён.", file=sys.stderr)
        return 130
    finally:
        if log_handler is not None:
            logger.removeHandler(log_handler)
            log_handler.close()
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate

    print(f"OK: user_id={user_id}, cookies сохранены в {saved.resolve()}", file=sys.stderr)
    return 0


def _setup_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except ValueError:
                continue


if __name__ == "__main__":
    raise SystemExit(main())
