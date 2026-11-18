from __future__ import annotations

import asyncio
import base64
import http.cookiejar
import inspect
import json
import logging
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .browser import launch_chromium
from .errors import ManualVerificationRequired, VkpwError

AUTH_URL = "https://id.vk.ru/auth"
AUTH_APP_ID = 7913379
WEB_TOKEN_URL = "https://login.vk.ru/?act=web_token"
WEB_TOKEN_APP_ID = 6287487
API_HOST = "https://api.vk.ru"
API_VERSION = "5.199"
REQUIRED_COOKIES = ("p", "remixsid")
DEFAULT_TIMEOUT = 60.0
_INITIAL_DEVICE_COOKIE = "1920/1080/2/!!-!!!!"
_CHALLENGE_SETTLE_WAIT = 5.0
_MAX_METHOD_WAIT = 35.0
_PHONE_RE = re.compile(r"^[+\d\s().-]+$")
_METHOD_COUNTDOWN_MARKERS = ("00:", "seconds", "second", "секунд")
_CHALLENGE_MARKERS = (
    "captcha",
    "капча",
    "капч",
    "не робот",
    "введите код",
    "код из max",
    "одноразовый код",
    "код из sms",
    "подтвердите вход",
    "подтверждение входа",
    "enter code",
    "code from max",
    "verification code",
    "two-factor",
    "suspicious activity",
    "подозрительная активность",
)
_LOGIN_ERROR_MARKERS = (
    "неверный пароль",
    "неправильный пароль",
    "проверьте правильность введённых данных",
    "проверьте правильность введенных данных",
    "аккаунт не найден",
    "не удалось войти",
    "incorrect password",
    "wrong password",
)
_OTHER_CONFIRMATION_OPTIONS = re.compile(
    r"(?:other\s+(?:(?:confirmation|verification)\s+)?(?:options?|methods?)|"
    r"друг(?:ие|ой)\s+(?:вариант\w*|способ\w*|метод\w*))",
    re.IGNORECASE,
)
_PASSWORD_METHOD = re.compile(r"^(?:password|пароль)$", re.IGNORECASE)

logger = logging.getLogger(__name__)

ManualChallengeHandler = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """Проверенные cookies браузерной сессии VK."""

    user_id: int
    cookies: dict[str, str]


def build_auth_url(*, uuid: str | None = None) -> str:
    """Собрать URL VK ID, открывающий штатную страницу браузерного входа."""
    auth_action = base64.b64encode(
        json.dumps(
            {
                "name": "qr_auth",
                "token": "qr_auth_scanned",
                "entry": {"source": "main", "screen": "start"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    query = urllib.parse.urlencode(
        {
            "action": auth_action,
            "scheme": "dark",
            "is_redesigned": "1",
            "response_type": "silent_token",
            "v": "1.3.0",
            "redirect_uri": "https://vk.ru/",
            "uuid": uuid or "".join(secrets.choice(string.ascii_lowercase) for _ in range(6)),
            "app_id": str(AUTH_APP_ID),
        }
    )
    return f"{AUTH_URL}?{query}"


def extract_session_cookies(browser_cookies: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Оставить обязательные VK cookies, полученные на доменах VK."""
    found: dict[str, tuple[int, str]] = {}
    for cookie in browser_cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").lstrip(".").casefold()
        if name not in REQUIRED_COOKIES or not value:
            continue
        if domain != "vk.ru" and not domain.endswith(".vk.ru"):
            continue
        priority = 2 if domain == "vk.ru" else 1
        if name not in found or priority >= found[name][0]:
            found[name] = (priority, value)
    return {name: value for name, (_, value) in found.items()}


def _cookie_jar(cookies: dict[str, str]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for name in REQUIRED_COOKIES:
        value = cookies.get(name)
        if not value:
            raise VkpwError(f"В браузере отсутствует обязательная cookie {name}")
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=".vk.ru",
                domain_specified=True,
                domain_initial_dot=True,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""},
                rfc2109=False,
            )
        )
    return jar


def _request_json(
    opener: Any,
    request: urllib.request.Request,
    *,
    timeout: float,
    label: str,
) -> Any:
    logger.info("Этап начат: запрос VK %s", label)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        logger.info("Этап завершился ошибкой: запрос VK %s (HTTP %s)", label, exc.code)
        raise VkpwError(f"{label}: VK вернул HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        logger.info("Этап завершился ошибкой: запрос VK %s (сетевая ошибка)", label)
        raise VkpwError(f"{label}: сетевая ошибка ({type(exc.reason).__name__})") from None
    except OSError as exc:
        logger.info("Этап завершился ошибкой: запрос VK %s (сетевая ошибка)", label)
        raise VkpwError(f"{label}: сетевая ошибка ({type(exc).__name__})") from None
    except (UnicodeError, ValueError, TypeError) as exc:
        logger.info("Этап завершился ошибкой: некорректный ответ VK %s", label)
        raise VkpwError(f"{label}: VK вернул некорректный ответ") from exc
    logger.info("Этап пройден: запрос VK %s", label)
    return payload


def verify_session(
    cookies: dict[str, str],
    *,
    timeout: float = 30.0,
    opener: Any | None = None,
) -> BrowserSession:
    """Проверить cookies через web_token и users.get, как ожидает vkx/vkqr."""
    logger.info("Этап начат: проверка cookies сессии")
    try:
        jar = _cookie_jar(cookies)
    except VkpwError as exc:
        logger.info("Этап завершился ошибкой: проверка cookies сессии (%s)", exc)
        raise
    logger.info("Этап пройден: обязательные cookies сессии получены")
    client = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    web_token_request = urllib.request.Request(
        WEB_TOKEN_URL,
        data=urllib.parse.urlencode({"version": "1", "app_id": str(WEB_TOKEN_APP_ID)}).encode(),
        headers={
            "Origin": "https://vk.ru",
            "Referer": "https://vk.ru/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    web_token_payload = _request_json(
        client,
        web_token_request,
        timeout=timeout,
        label="web_token",
    )
    logger.info("Этап начат: проверка ответа web_token")
    try:
        access_token = web_token_payload["data"]["access_token"]
    except (KeyError, TypeError) as exc:
        logger.info("Этап завершился ошибкой: проверка ответа web_token")
        raise VkpwError("VK не принял cookies или не выдал веб-токен") from exc
    if not isinstance(access_token, str) or not access_token:
        logger.info("Этап завершился ошибкой: проверка ответа web_token")
        raise VkpwError("VK не выдал веб-токен для проверки cookies")
    logger.info("Этап пройден: ответ web_token проверен")

    users_url = f"{API_HOST}/method/users.get?{urllib.parse.urlencode({'v': API_VERSION})}"
    users_request = urllib.request.Request(
        users_url,
        data=urllib.parse.urlencode({"access_token": access_token}).encode(),
        headers={
            "Origin": "https://id.vk.ru",
            "Referer": "https://id.vk.ru/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    users_payload = _request_json(client, users_request, timeout=timeout, label="users.get")
    logger.info("Этап начат: проверка ответа users.get")
    if not isinstance(users_payload, dict):
        logger.info("Этап завершился ошибкой: проверка ответа users.get")
        raise VkpwError("users.get вернул неожиданный ответ")
    error = users_payload.get("error")
    if isinstance(error, dict):
        logger.info("Этап завершился ошибкой: проверка ответа users.get")
        raise VkpwError("VK не подтвердил авторизацию cookies через users.get")
    users = users_payload.get("response", users_payload)
    if not isinstance(users, list) or not users or not isinstance(users[0], dict):
        logger.info("Этап завершился ошибкой: проверка ответа users.get")
        raise VkpwError("users.get не вернул данные пользователя")
    try:
        user_id = int(users[0]["id"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.info("Этап завершился ошибкой: проверка ответа users.get")
        raise VkpwError("users.get не вернул корректный user_id") from exc
    if user_id <= 0:
        logger.info("Этап завершился ошибкой: проверка ответа users.get")
        raise VkpwError("users.get вернул некорректный user_id")

    refreshed = {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.name in REQUIRED_COOKIES and cookie.value
    }
    checked_cookies = {name: refreshed.get(name, cookies[name]) for name in REQUIRED_COOKIES}
    logger.info("Этап пройден: сессия VK подтверждена")
    return BrowserSession(user_id=user_id, cookies=checked_cookies)


async def _body_text(page: Any) -> str:
    try:
        return (await page.locator("body").inner_text(timeout=1500)).casefold()
    except PlaywrightError:
        return ""


async def _has_manual_challenge(page: Any) -> bool:
    text = await _body_text(page)
    if any(marker in text for marker in _CHALLENGE_MARKERS):
        return True
    try:
        return await page.locator(
            'iframe[src*="captcha" i]:visible, [data-testid*="captcha" i]:visible, '
            '[class*="captcha" i]:visible'
        ).count() > 0
    except PlaywrightError:
        return False


async def _has_login_error(page: Any) -> bool:
    text = await _body_text(page)
    return any(marker in text for marker in _LOGIN_ERROR_MARKERS)


async def _handle_manual_challenge(
    page: Any,
    handler: ManualChallengeHandler | None,
) -> None:
    if not await _has_manual_challenge(page):
        return
    logger.info("VK запросил дополнительную проверку; автоматический обход не выполняется")
    if handler is None:
        raise ManualVerificationRequired(
            "VK запросил CAPTCHA или дополнительное подтверждение. "
            "Автоматически такие проверки не обходятся."
        )
    result = handler()
    if inspect.isawaitable(result):
        await result
    logger.info("Обработчик дополнительной проверки завершил работу")


async def _wait_for_visible(
    page: Any,
    selector: str,
    *,
    description: str,
    timeout: float,
    manual_challenge: ManualChallengeHandler | None,
) -> Any:
    locator = page.locator(selector)
    deadline = time.monotonic() + timeout
    prompted = False
    while time.monotonic() < deadline:
        if await locator.is_visible():
            return locator
        if await _has_login_error(page):
            raise VkpwError("VK отклонил логин или запрос на вход")
        if await _has_manual_challenge(page):
            if manual_challenge is None:
                logger.info("VK запросил дополнительную проверку при ожидании этапа «%s»", description)
                raise ManualVerificationRequired(
                    "VK запросил CAPTCHA или дополнительное подтверждение. "
                    "Автоматически такие проверки не обходятся."
                )
            if not prompted:
                result = manual_challenge()
                if inspect.isawaitable(result):
                    await result
                prompted = True
                deadline = time.monotonic() + timeout
        await asyncio.sleep(0.25)
    raise VkpwError(f"Не дождался шага «{description}» в VK ID")


async def _click_continue(page: Any) -> None:
    button = page.get_by_role("button", name=re.compile(r"Продолжить|Continue", re.IGNORECASE))
    try:
        await button.last.click(timeout=15_000)
    except PlaywrightError as exc:
        raise VkpwError("Не нашёл кнопку «Продолжить» на странице VK ID") from exc


async def _click_visible_text(page: Any, pattern: re.Pattern[str]) -> bool:
    locator = page.get_by_text(pattern)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            await candidate.click(timeout=3_000)
        except PlaywrightError:
            continue
        return True
    return False


async def _click_other_verification_methods(page: Any) -> bool:
    button = page.locator('[data-test-id="other-verification-methods"]')
    try:
        if not await button.is_visible():
            return False
        await button.click(timeout=3_000)
    except PlaywrightError:
        return False
    return True


async def _wait_for_password_field(
    page: Any,
    *,
    timeout: float,
    manual_challenge: ManualChallengeHandler | None,
) -> Any:
    """Wait for the password form, selecting VK's offered password method if needed."""
    password_field = page.locator('input[name="password"]')
    deadline = time.monotonic() + timeout
    challenge_prompted = False
    challenge_detected_at: float | None = None
    options_opened = False
    password_method_selected = False
    countdown_deadline: float | None = None

    while time.monotonic() < deadline:
        if await password_field.is_visible():
            return password_field
        if await _has_login_error(page):
            raise VkpwError("VK отклонил логин или запрос на вход")

        if options_opened:
            if await _click_visible_text(page, _PASSWORD_METHOD):
                logger.info("В VK выбран штатный вход по паролю из других способов подтверждения")
                options_opened = False
                password_method_selected = True
                deadline = time.monotonic() + timeout
                continue
        elif not password_method_selected:
            if await _click_other_verification_methods(page):
                logger.info("В VK открыты другие способы подтверждения")
                options_opened = True
                deadline = time.monotonic() + timeout
                continue
            if await _has_manual_challenge(page):
                if await _click_visible_text(page, _OTHER_CONFIRMATION_OPTIONS):
                    logger.info("В VK открыты другие способы подтверждения")
                    options_opened = True
                    deadline = time.monotonic() + timeout
                    continue
                body_text = await _body_text(page)
                if any(marker in body_text for marker in _METHOD_COUNTDOWN_MARKERS):
                    if countdown_deadline is None:
                        countdown_deadline = time.monotonic() + min(_MAX_METHOD_WAIT, timeout)
                        logger.info(
                            "VK показывает таймер перед доступностью других способов подтверждения"
                        )
                    if time.monotonic() < countdown_deadline:
                        await asyncio.sleep(0.25)
                        continue
                else:
                    if challenge_detected_at is None:
                        challenge_detected_at = time.monotonic()
                    if time.monotonic() - challenge_detected_at < _CHALLENGE_SETTLE_WAIT:
                        await asyncio.sleep(0.25)
                        continue
                if manual_challenge is None:
                    logger.info("VK запросил дополнительную проверку при ожидании выбора метода входа")
                    if countdown_deadline is not None:
                        raise ManualVerificationRequired(
                            "VK не показал другие способы подтверждения после ожидания таймера. "
                            "Увеличьте --timeout или пройдите проверку вручную."
                        )
                    raise ManualVerificationRequired(
                        "VK запросил CAPTCHA или дополнительное подтверждение. "
                        "Автоматически такие проверки не обходятся."
                    )
                if not challenge_prompted:
                    result = manual_challenge()
                    if inspect.isawaitable(result):
                        await result
                    challenge_prompted = True
                    deadline = time.monotonic() + timeout

        await asyncio.sleep(0.25)

    if options_opened:
        raise ManualVerificationRequired(
            "VK предложил другие способы подтверждения, но не удалось выбрать вход по паролю. "
            "Требуется дополнительная проверка вручную."
        )
    if countdown_deadline is not None:
        raise ManualVerificationRequired(
            "VK не показал другие способы подтверждения до истечения таймаута. "
            "Увеличьте --timeout или пройдите проверку вручную."
        )
    if challenge_detected_at is not None:
        raise ManualVerificationRequired(
            "VK запросил CAPTCHA или дополнительное подтверждение. "
            "Автоматически такие проверки не обходятся."
        )
    raise VkpwError("Не дождался поля пароля в VK ID")


async def _wait_for_session_cookies(
    page: Any,
    context: Any,
    *,
    timeout: float,
    manual_challenge: ManualChallengeHandler | None,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    prompted = False
    while time.monotonic() < deadline:
        cookies = extract_session_cookies(await context.cookies())
        if await _has_manual_challenge(page):
            if manual_challenge is None:
                logger.info("VK запросил дополнительную проверку при ожидании cookies сессии")
                raise ManualVerificationRequired(
                    "VK запросил CAPTCHA или дополнительное подтверждение. "
                    "Автоматически такие проверки не обходятся."
                )
            if not prompted:
                result = manual_challenge()
                if inspect.isawaitable(result):
                    await result
                prompted = True
                deadline = time.monotonic() + timeout
            await asyncio.sleep(0.25)
            continue
        if all(cookies.get(name) for name in REQUIRED_COOKIES):
            return cookies
        if await _has_login_error(page):
            raise VkpwError("VK отклонил логин или пароль")
        await asyncio.sleep(0.25)
    raise VkpwError("После входа браузер не получил обязательные cookies p и remixsid")


async def authenticate(
    login: str,
    password: str,
    *,
    browser_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    manual_challenge: ManualChallengeHandler | None = None,
) -> BrowserSession:
    """Войти через штатную страницу VK ID и вернуть подтверждённые cookies."""
    logger.info("Начата авторизация VK")
    stage = "проверка входных данных"
    cleanup_failed = False
    try:
        if not login.strip():
            raise VkpwError("Логин не должен быть пустым")
        if not password:
            raise VkpwError("Пароль не должен быть пустым")
        if timeout <= 0:
            raise VkpwError("Таймаут должен быть больше нуля")
        logger.info("Этап пройден: проверка входных данных")

        stage = "запуск Playwright"
        async with async_playwright() as playwright:
            logger.info("Этап пройден: запуск Playwright")

            stage = "запуск браузера"
            browser = await launch_chromium(
                playwright,
                browser_path=browser_path,
            )
            logger.info("Этап пройден: браузер запущен в headless-режиме")
            try:
                stage = "создание чистого контекста браузера"
                context = await browser.new_context(locale="ru-RU")
                logger.info("Этап пройден: создан новый временный контекст браузера")

                stage = "установка начальной cookie устройства"
                await context.add_cookies(
                    [
                        {
                            "name": "remixmdevice",
                            "value": _INITIAL_DEVICE_COOKIE,
                            "domain": ".vk.ru",
                            "path": "/",
                        }
                    ]
                )
                logger.info("Этап пройден: начальная cookie устройства добавлена")

                stage = "создание страницы браузера"
                page = await context.new_page()
                logger.info("Этап пройден: страница VK ID создана")

                stage = "загрузка страницы VK ID"
                try:
                    await page.goto(
                        build_auth_url(),
                        wait_until="domcontentloaded",
                        timeout=int(timeout * 1000),
                    )
                except PlaywrightError as exc:
                    raise VkpwError(
                        f"Не удалось открыть страницу VK ID ({type(exc).__name__})"
                    ) from exc

                logger.info("Этап пройден: страница VK ID загружена")
                stage = "первичная проверка дополнительного подтверждения"
                await _handle_manual_challenge(page, manual_challenge)
                logger.info("Этап пройден: первичная проверка дополнительного подтверждения")

                stage = "поиск формы логина"
                login_field = page.locator('input[name="login"]')
                if not await login_field.is_visible():
                    stage = "переход к форме входа по логину"
                    enter_by_login = page.get_by_role(
                        "button",
                        name=re.compile(r"Войти.*телефону.*почте|phone or email", re.IGNORECASE),
                    )
                    try:
                        await enter_by_login.click(timeout=15_000)
                    except PlaywrightError as exc:
                        raise VkpwError(
                            "Не нашёл на странице VK ID переход к входу по логину"
                        ) from exc
                    logger.info("Этап пройден: выбран вход по логину VK ID")

                    stage = "поиск поля логина"
                    login_field = await _wait_for_visible(
                        page,
                        'input[name="login"]',
                        description="ввода логина",
                        timeout=timeout,
                        manual_challenge=manual_challenge,
                    )
                logger.info("Этап пройден: поле логина доступно")

                if not _PHONE_RE.fullmatch(login.strip()):
                    login_modes = page.locator('input[type="radio"][name="login-view"]')
                    if await login_modes.count() > 1:
                        stage = "выбор режима ввода почты или логина"
                        try:
                            await login_modes.nth(1).check(timeout=5_000)
                        except PlaywrightError as exc:
                            raise VkpwError(
                                "Не удалось выбрать ввод почты/логина вместо телефона"
                            ) from exc
                        logger.info("Этап пройден: выбран режим ввода почты или логина")

                stage = "отправка логина"
                await login_field.fill(login.strip())
                await _click_continue(page)
                logger.info("Этап пройден: логин отправлен")

                stage = "выбор метода и поиск поля пароля"
                password_field = await _wait_for_password_field(
                    page,
                    timeout=timeout,
                    manual_challenge=manual_challenge,
                )
                logger.info("Этап пройден: поле пароля доступно")

                stage = "отправка пароля"
                await password_field.fill(password)
                await _click_continue(page)
                logger.info("Этап пройден: пароль отправлен")

                stage = "получение cookies сессии"
                browser_cookies = await _wait_for_session_cookies(
                    page,
                    context,
                    timeout=timeout,
                    manual_challenge=manual_challenge,
                )
                logger.info("Этап пройден: обязательные cookies сессии получены")

                stage = "проверка сессии"
                session = await asyncio.to_thread(verify_session, browser_cookies)
                logger.info("Этап пройден: сессия подтверждена VK")
                return session
            finally:
                logger.info("Этап начат: закрытие временного браузера")
                try:
                    await browser.close()
                except Exception as exc:
                    cleanup_failed = True
                    logger.info(
                        "Этап завершился ошибкой: закрытие временного браузера (%s)",
                        type(exc).__name__,
                    )
                    raise
                logger.info("Этап пройден: временный браузер закрыт")
    except VkpwError as exc:
        if not cleanup_failed:
            logger.info("Этап завершился ошибкой: %s — %s", stage, exc)
        raise
    except PlaywrightError as exc:
        if not cleanup_failed:
            logger.info(
                "Этап завершился ошибкой: %s (ошибка управления браузером: %s)",
                stage,
                type(exc).__name__,
            )
        raise VkpwError(f"Ошибка управления браузером ({type(exc).__name__})") from exc
    except Exception as exc:
        if not cleanup_failed:
            logger.info(
                "Этап завершился неожиданной ошибкой: %s (%s)",
                stage,
                type(exc).__name__,
            )
        raise
