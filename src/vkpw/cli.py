from __future__ import annotations

import argparse
import asyncio
import getpass
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .auth import DEFAULT_TIMEOUT, authenticate
from .errors import VkpwError
from .storage import append_entry, build_entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vkpw",
        description="Получение cookies ВКонтакте через браузерный вход по логину и паролю.",
        epilog="Для установки Chromium, если нет Chrome/Edge/Brave: vkpw install-browser",
    )
    parser.add_argument("-o", "--output", metavar="FILE", help="JSON-файл для cookies")
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
    if args.timeout <= 0:
        parser.error("--timeout должен быть больше нуля")
    if args.credentials is not None and args.login is not None:
        parser.error("укажите либо LOGIN:PASSWORD, либо --login")
    if args.credentials is not None and args.output is None:
        parser.error("для LOGIN:PASSWORD обязательно явно укажите -o/--output")

    if args.credentials is not None:
        try:
            login, password = _parse_credentials(args.credentials)
        except VkpwError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
    else:
        if not sys.stdin.isatty():
            print(
                "Ошибка: vkpw требует интерактивный терминал для ввода логина и пароля",
                file=sys.stderr,
            )
            return 2
        login = args.login or _read_line("Логин VK (телефон или почта): ")
        if not login:
            print("Ошибка: логин не задан", file=sys.stderr)
            return 2
        password = None

    try:
        if password is None:
            password = getpass.getpass("Пароль VK: ")
        user_id, cookies = asyncio.run(_run_login(args, login=login, password=password))
        entry = build_entry(cookies, user_id)
        target = args.output
        if target is None:
            target = _read_line("Укажите путь к JSON-файлу для cookies: ")
            if not target:
                raise VkpwError("Путь к файлу не задан, cookies не сохранены")
        saved = append_entry(Path(target), entry)
    except VkpwError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nВход отменён.", file=sys.stderr)
        return 130

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
