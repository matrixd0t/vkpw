from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error as PlaywrightError

from vkpw.auth import (
    AUTH_APP_ID,
    REQUIRED_COOKIES,
    WEB_TOKEN_APP_ID,
    BrowserSession,
    _wait_for_password_field,
    authenticate,
    build_auth_url,
    extract_session_cookies,
    verify_session,
)
from vkpw.errors import ManualVerificationRequired, VkpwError


class FakeOpener:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = iter(payloads)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return io.BytesIO(json.dumps(next(self.payloads)).encode("utf-8"))


class AuthTests(unittest.TestCase):
    def test_auth_url_contains_vk_id_bootstrap_parameters(self) -> None:
        url = build_auth_url(uuid="abcdef")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["app_id"], [str(AUTH_APP_ID)])
        self.assertEqual(query["uuid"], ["abcdef"])
        self.assertEqual(query["response_type"], ["silent_token"])

    def test_cookie_extraction_keeps_only_vk_session_cookies(self) -> None:
        cookies = extract_session_cookies(
            [
                {"name": "p", "value": "p-value", "domain": ".vk.ru"},
                {"name": "remixsid", "value": "sid-value", "domain": "login.vk.ru"},
                {"name": "remixsid", "value": "other", "domain": ".example.org"},
                {"name": "other", "value": "ignored", "domain": ".vk.ru"},
            ]
        )
        self.assertEqual(cookies, {"p": "p-value", "remixsid": "sid-value"})

    def test_verify_session_uses_web_token_and_users_get(self) -> None:
        opener = FakeOpener(
            [
                {"data": {"access_token": "private-token"}},
                {"response": [{"id": 580106999}]},
            ]
        )

        session = verify_session(
            {"p": "p-value", "remixsid": "sid-value"},
            opener=opener,
        )

        self.assertEqual(session, BrowserSession(580106999, {"p": "p-value", "remixsid": "sid-value"}))
        self.assertEqual(len(opener.requests), 2)
        first_request, _ = opener.requests[0]
        first_data = urllib.parse.parse_qs(first_request.data.decode())
        self.assertEqual(first_data, {"version": ["1"], "app_id": [str(WEB_TOKEN_APP_ID)]})
        second_request, _ = opener.requests[1]
        self.assertTrue(second_request.full_url.endswith("v=5.199"))
        self.assertNotIn("private-token", second_request.full_url)
        self.assertEqual(
            urllib.parse.parse_qs(second_request.data.decode()),
            {"access_token": ["private-token"]},
        )

    def test_verify_session_rejects_missing_required_cookies(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "remixsid"):
            verify_session({"p": "p-value"}, opener=Mock())

    def test_cookie_constant_is_vk_compatible(self) -> None:
        self.assertEqual(REQUIRED_COOKIES, ("p", "remixsid"))


class _FakeLocator:
    async def is_visible(self) -> bool:
        return True

    async def count(self) -> int:
        return 0

    async def fill(self, value: str) -> None:
        pass


class _FakeBodyLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def inner_text(self, timeout: int) -> str:
        if self.page.state == "code":
            if time.monotonic() - self.page.started_at < self.page.countdown_after:
                return self.page.challenge_text.replace("00:30", "")
            return self.page.challenge_text
        if self.page.state == "methods":
            return "Select verification method"
        return "Enter your account password"


class _FakePasswordLocator(_FakeLocator):
    def __init__(self, page) -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return self.page.state == "password"


class _FakeOtherMethodsButton:
    def __init__(self, page) -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return self.page.state == "code" and self.page.has_other_methods_button

    async def click(self, timeout: int) -> None:
        self.page.state = "methods"


class _FakeTextCandidate:
    def __init__(self, page, label: str) -> None:
        self.page = page
        self.label = label

    async def is_visible(self) -> bool:
        return self.label in self.page.labels_for_state()

    async def click(self, timeout: int) -> None:
        if self.page.state == "code" and self.label == self.page.other_options_label:
            self.page.state = "methods"
        elif self.page.state == "methods" and self.label == self.page.password_label:
            self.page.state = "password"


class _FakeTextMatches:
    def __init__(self, page, pattern) -> None:
        self.page = page
        self.pattern = pattern

    def matches(self) -> list[str]:
        return [label for label in self.page.labels_for_state() if self.pattern.search(label)]

    async def count(self) -> int:
        return len(self.matches())

    def nth(self, index: int) -> _FakeTextCandidate:
        return _FakeTextCandidate(self.page, self.matches()[index])


class _FakeMethodPage:
    def __init__(
        self,
        other_options_label: str,
        password_label: str,
        *,
        reveal_after: float = 0,
        countdown_after: float = 0,
        has_other_methods_button: bool = False,
        challenge_text: str = "Enter code 00:30",
    ) -> None:
        self.state = "code"
        self.other_options_label = other_options_label
        self.password_label = password_label
        self.reveal_after = reveal_after
        self.countdown_after = countdown_after
        self.has_other_methods_button = has_other_methods_button
        self.started_at = time.monotonic()
        self.challenge_text = challenge_text

    def labels_for_state(self) -> list[str]:
        if self.state == "code":
            if time.monotonic() - self.started_at < self.reveal_after:
                return []
            return [self.other_options_label] if self.other_options_label else []
        if self.state == "methods":
            return [self.password_label]
        return []

    def locator(self, selector: str):
        if selector == "body":
            return _FakeBodyLocator(self)
        if selector == 'input[name="password"]':
            return _FakePasswordLocator(self)
        if selector == '[data-test-id="other-verification-methods"]':
            return _FakeOtherMethodsButton(self)
        return _FakeLocator()

    def get_by_text(self, pattern):
        return _FakeTextMatches(self, pattern)


class _FakeAuthPage:
    def __init__(self) -> None:
        self.login_locator = _FakeLocator()
        self.screenshot = AsyncMock()
        self.evaluate = AsyncMock(
            return_value=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        )
        self.close = AsyncMock()

    def locator(self, selector: str) -> _FakeLocator:
        return self.login_locator if selector == 'input[name="login"]' else _FakeLocator()

    async def goto(self, *args, **kwargs) -> None:
        pass


class _FakeAuthContext:
    def __init__(self) -> None:
        self.page = _FakeAuthPage()
        self.add_cookies = AsyncMock()
        self.add_init_script = AsyncMock()
        self.close = AsyncMock()

    async def new_page(self) -> _FakeAuthPage:
        return self.page


class _FakeAuthBrowser:
    def __init__(self) -> None:
        self.context = _FakeAuthContext()
        self.new_context = AsyncMock(return_value=self.context)
        self.close = AsyncMock()


class _FakeRobotBody:
    def __init__(self, page) -> None:
        self.page = page

    async def inner_text(self, timeout: int | None = None) -> str:
        if self.page.state == "robot":
            return "Подтвердите, что вы не робот\nЯ не робот"
        return "Введите пароль от аккаунта"


class _FakeRobotPasswordLocator(_FakeLocator):
    def __init__(self, page) -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return self.page.state == "password"


class _FakeRobotHiddenLocator(_FakeLocator):
    async def is_visible(self) -> bool:
        return False


class _FakeRobotCheckboxElement:
    def __init__(self, page) -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return self.page.state == "robot"

    async def bounding_box(self):
        return {"x": 580.0, "y": 414.0, "width": 16.0, "height": 16.0}

    async def click(self, timeout: int | None = None) -> None:
        self.page.checkbox_clicks += 1
        self.page.state = "password"


class _FakeRobotCheckboxLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1 if self.page.state == "robot" else 0

    def nth(self, index: int) -> _FakeRobotCheckboxElement:
        return _FakeRobotCheckboxElement(self.page)


class _FakeRobotLabelElement:
    def __init__(self, page) -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return self.page.state == "robot"

    async def bounding_box(self):
        return {"x": 614.0, "y": 415.0, "width": 90.0, "height": 20.0}

    async def click(self, timeout: int | None = None) -> None:
        self.page.state = "password"


class _FakeRobotLabels:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> _FakeRobotLabelElement:
        return _FakeRobotLabelElement(self.page)


class _FakeRobotPage:
    """Страница VK ID: сначала окно «Я не робот», затем поле пароля."""

    def __init__(self) -> None:
        self.state = "robot"
        self.checkbox_clicks = 0
        self.main_frame = None

    @property
    def frames(self):
        return [self]

    def locator(self, selector: str):
        if selector == "body":
            return _FakeRobotBody(self)
        if selector == 'input[name="password"]':
            return _FakeRobotPasswordLocator(self)
        if selector == '[data-test-id="other-verification-methods"]':
            return _FakeRobotHiddenLocator()
        if "captcha" in selector.lower():
            return _FakeRobotHiddenLocator()
        return _FakeRobotCheckboxLocator(self)

    def get_by_text(self, pattern):
        return _FakeRobotLabels(self)


class _FakePlaywrightManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        pass


class AuthIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_code_screen_switches_to_offered_password_method(self) -> None:
        for other_options, password in (
            ("Other confirmation options", "Password"),
            ("Другие варианты", "Пароль"),
        ):
            with self.subTest(language=other_options):
                page = _FakeMethodPage(other_options, password)
                field = await _wait_for_password_field(
                    page,
                    timeout=1,
                    manual_challenge=None,
                )
                self.assertEqual(page.state, "password")
                self.assertTrue(await field.is_visible())

    async def test_other_verification_methods_test_id_works_without_text_marker(self) -> None:
        page = _FakeMethodPage(
            "",
            "Password",
            has_other_methods_button=True,
            challenge_text="Вам поступил звонок-сброс. Введите последние 6 цифр номера.",
        )
        field = await _wait_for_password_field(
            page,
            timeout=1,
            manual_challenge=None,
        )

        self.assertEqual(page.state, "password")
        self.assertTrue(await field.is_visible())

    async def test_waits_for_other_methods_button_after_max_countdown(self) -> None:
        page = _FakeMethodPage(
            "Other confirmation options",
            "Password",
            reveal_after=0.02,
        )
        with patch("vkpw.auth._MAX_METHOD_WAIT", 1):
            field = await _wait_for_password_field(
                page,
                timeout=2,
                manual_challenge=None,
            )

        self.assertEqual(page.state, "password")
        self.assertTrue(await field.is_visible())

    async def test_waits_for_countdown_to_render_before_manual_challenge_error(self) -> None:
        page = _FakeMethodPage(
            "Other confirmation options",
            "Password",
            reveal_after=0.04,
            countdown_after=0.02,
        )
        with (
            patch("vkpw.auth._CHALLENGE_SETTLE_WAIT", 0.5),
            patch("vkpw.auth._MAX_METHOD_WAIT", 1),
        ):
            field = await _wait_for_password_field(
                page,
                timeout=2,
                manual_challenge=None,
            )

        self.assertEqual(page.state, "password")
        self.assertTrue(await field.is_visible())

    async def test_code_screen_without_password_alternative_requires_manual_verification(self) -> None:
        page = _FakeMethodPage(
            "",
            "Password",
            challenge_text="Verification code required",
        )
        with (
            patch("vkpw.auth._CHALLENGE_SETTLE_WAIT", 0.01),
            self.assertRaises(ManualVerificationRequired),
        ):
            await _wait_for_password_field(page, timeout=1, manual_challenge=None)

    async def test_robot_check_is_passed_without_manual_verification(self) -> None:
        page = _FakeRobotPage()
        with patch("vkpw.challenge._SETTLE_SECONDS", 0.2):
            field = await _wait_for_password_field(
                page,
                timeout=2,
                manual_challenge=None,
            )

        self.assertEqual(page.state, "password")
        self.assertEqual(page.checkbox_clicks, 1)
        self.assertTrue(await field.is_visible())

    async def test_authentication_uses_fresh_context_without_profile_state(self) -> None:
        browser = _FakeAuthBrowser()
        session = BrowserSession(123, {"p": "p-value", "remixsid": "sid-value"})
        with (
            patch("vkpw.auth.async_playwright", return_value=_FakePlaywrightManager()),
            patch("vkpw.auth.launch_chromium", new_callable=AsyncMock, return_value=browser),
            patch("vkpw.auth._handle_manual_challenge", new_callable=AsyncMock),
            patch("vkpw.auth._click_continue", new_callable=AsyncMock),
            patch("vkpw.auth._wait_for_visible", new_callable=AsyncMock, return_value=_FakeLocator()),
            patch(
                "vkpw.auth._wait_for_password_field",
                new_callable=AsyncMock,
                return_value=_FakeLocator(),
            ),
            patch(
                "vkpw.auth._wait_for_session_cookies",
                new_callable=AsyncMock,
                return_value=session.cookies,
            ),
            patch("vkpw.auth.verify_session", return_value=session),
            self.assertLogs("vkpw.auth", level="INFO") as logs,
        ):
            result = await authenticate("1234567890", "secret")

        self.assertEqual(result, session)
        log_contents = "\n".join(logs.output)
        self.assertIn("Этап пройден: страница VK ID загружена", log_contents)
        self.assertIn("Этап пройден: пароль отправлен", log_contents)
        self.assertIn("Этап пройден: сессия подтверждена VK", log_contents)
        self.assertNotIn("secret", log_contents)
        self.assertNotIn("p-value", log_contents)
        browser.new_context.assert_awaited_once_with(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1920, "height": 1080},
        )
        browser.context.add_cookies.assert_awaited_once_with(
            [
                {
                    "name": "remixmdevice",
                    "value": "1920/1080/2/!!-!!!!",
                    "domain": ".vk.ru",
                    "path": "/",
                }
            ]
        )
        browser.close.assert_awaited_once()

    async def _authenticate_with_password_field_error(
        self,
        *,
        screenshot_path: str | Path | None,
        error: Exception,
        screenshot_side_effect: Exception | None = None,
    ) -> tuple[_FakeAuthBrowser, Exception]:
        browser = _FakeAuthBrowser()
        if screenshot_side_effect is not None:
            browser.context.page.screenshot.side_effect = screenshot_side_effect
        with (
            patch("vkpw.auth.async_playwright", return_value=_FakePlaywrightManager()),
            patch("vkpw.auth.launch_chromium", new_callable=AsyncMock, return_value=browser),
            patch("vkpw.auth._handle_manual_challenge", new_callable=AsyncMock),
            patch("vkpw.auth._click_continue", new_callable=AsyncMock),
            patch(
                "vkpw.auth._wait_for_password_field",
                new_callable=AsyncMock,
                side_effect=error,
            ),
        ):
            try:
                await authenticate("1234567890", "secret", screenshot_path=screenshot_path)
            except Exception as exc:  # noqa: BLE001 - тест проверяет тип ниже
                return browser, exc
        self.fail("authenticate должен был завершиться ошибкой")

    async def test_failure_saves_screenshot_before_browser_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "screenshots" / "failure.png"
            browser, exc = await self._authenticate_with_password_field_error(
                screenshot_path=target,
                error=VkpwError("VK отклонил логин"),
            )

            self.assertIsInstance(exc, VkpwError)
            assert isinstance(exc, VkpwError)
            self.assertEqual(exc.screenshot_path, target)
            browser.context.page.screenshot.assert_awaited_once_with(
                path=str(target), full_page=True, timeout=10_000
            )
            browser.close.assert_awaited_once()
            self.assertTrue(target.parent.is_dir())

    async def test_failure_without_screenshot_path_does_not_screenshot(self) -> None:
        browser, exc = await self._authenticate_with_password_field_error(
            screenshot_path=None,
            error=VkpwError("VK отклонил логин"),
        )

        self.assertIsInstance(exc, VkpwError)
        assert isinstance(exc, VkpwError)
        self.assertIsNone(exc.screenshot_path)
        browser.context.page.screenshot.assert_not_awaited()
        browser.close.assert_awaited_once()

    async def test_failure_screenshot_error_does_not_mask_original_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "failure.png"
            browser, exc = await self._authenticate_with_password_field_error(
                screenshot_path=target,
                error=VkpwError("VK отклонил логин"),
                screenshot_side_effect=RuntimeError("no disk"),
            )

            self.assertIsInstance(exc, VkpwError)
            assert isinstance(exc, VkpwError)
            self.assertEqual(str(exc), "VK отклонил логин")
            self.assertIsNone(exc.screenshot_path)
            browser.context.page.screenshot.assert_awaited_once()
            browser.close.assert_awaited_once()

    async def test_playwright_error_keeps_screenshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "failure.png"
            browser, exc = await self._authenticate_with_password_field_error(
                screenshot_path=str(target),
                error=PlaywrightError("browser died"),
            )

            self.assertIsInstance(exc, VkpwError)
            assert isinstance(exc, VkpwError)
            self.assertEqual(exc.screenshot_path, target)
            browser.context.page.screenshot.assert_awaited_once()
            browser.close.assert_awaited_once()

    async def test_success_does_not_screenshot(self) -> None:
        browser = _FakeAuthBrowser()
        session = BrowserSession(123, {"p": "p-value", "remixsid": "sid-value"})
        with (
            patch("vkpw.auth.async_playwright", return_value=_FakePlaywrightManager()),
            patch("vkpw.auth.launch_chromium", new_callable=AsyncMock, return_value=browser),
            patch("vkpw.auth._handle_manual_challenge", new_callable=AsyncMock),
            patch("vkpw.auth._click_continue", new_callable=AsyncMock),
            patch(
                "vkpw.auth._wait_for_password_field",
                new_callable=AsyncMock,
                return_value=_FakeLocator(),
            ),
            patch(
                "vkpw.auth._wait_for_session_cookies",
                new_callable=AsyncMock,
                return_value=session.cookies,
            ),
            patch("vkpw.auth.verify_session", return_value=session),
        ):
            result = await authenticate(
                "1234567890", "secret", screenshot_path="/tmp/vkpw-failure.png"
            )

        self.assertEqual(result, session)
        browser.context.page.screenshot.assert_not_awaited()
        browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
