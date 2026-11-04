from __future__ import annotations

import io
import json
import unittest
import urllib.parse
from unittest.mock import AsyncMock, Mock, patch

from vkpw.auth import (
    AUTH_APP_ID,
    REQUIRED_COOKIES,
    WEB_TOKEN_APP_ID,
    BrowserSession,
    authenticate,
    build_auth_url,
    extract_session_cookies,
    verify_session,
)


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


class _FakeAuthPage:
    def __init__(self) -> None:
        self.login_locator = _FakeLocator()

    def locator(self, selector: str) -> _FakeLocator:
        return self.login_locator if selector == 'input[name="login"]' else _FakeLocator()

    async def goto(self, *args, **kwargs) -> None:
        pass


class _FakeAuthContext:
    def __init__(self) -> None:
        self.page = _FakeAuthPage()
        self.add_cookies = AsyncMock()

    async def new_page(self) -> _FakeAuthPage:
        return self.page


class _FakeAuthBrowser:
    def __init__(self) -> None:
        self.context = _FakeAuthContext()
        self.new_context = AsyncMock(return_value=self.context)
        self.close = AsyncMock()


class _FakePlaywrightManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        pass


class AuthIsolationTests(unittest.IsolatedAsyncioTestCase):
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
                "vkpw.auth._wait_for_session_cookies",
                new_callable=AsyncMock,
                return_value=session.cookies,
            ),
            patch("vkpw.auth.verify_session", return_value=session),
        ):
            result = await authenticate("1234567890", "secret")

        self.assertEqual(result, session)
        browser.new_context.assert_awaited_once_with(locale="ru-RU")
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


if __name__ == "__main__":
    unittest.main()
