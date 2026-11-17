from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from vkpw.cli import _install_browser, _parse_credentials, main
from vkpw.errors import VkpwError


class CredentialParsingTests(unittest.TestCase):
    def test_splits_on_first_colon_and_removes_password_whitespace(self) -> None:
        self.assertEqual(
            _parse_credentials(" user : pass word:tail \t"),
            ("user", "password:tail"),
        )

    def test_rejects_missing_separator_or_empty_parts(self) -> None:
        for value in ("login", ":password", "login:", "login: \t"):
            with self.subTest(value=value), self.assertRaises(VkpwError):
                _parse_credentials(value)


class CredentialsCliTests(unittest.TestCase):
    def test_credentials_require_explicit_output_without_prompting(self) -> None:
        stderr = io.StringIO()
        with (
            patch("vkpw.cli._setup_streams"),
            patch("vkpw.cli.sys.stdin", Mock(isatty=Mock(return_value=False))),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["login:secret"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("обязательно явно укажите -o/--output", stderr.getvalue())

    def test_credentials_run_without_tty_or_separate_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cookies.json"
            stderr = io.StringIO()
            with (
                patch("vkpw.cli._setup_streams"),
                patch("vkpw.cli.sys.stdin", Mock(isatty=Mock(return_value=False))),
                patch(
                    "vkpw.cli._run_login",
                    new_callable=AsyncMock,
                    return_value=(123, {"p": "p-value", "remixsid": "sid-value"}),
                ) as run_login,
                redirect_stderr(stderr),
            ):
                result = main(["login:secret", "-o", str(output)])

            self.assertEqual(result, 0)
            call_args = run_login.await_args
            self.assertEqual(call_args.kwargs["login"], "login")
            self.assertEqual(call_args.kwargs["password"], "secret")
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookies"][0]["user_id"], 123)
            self.assertNotIn("secret", stderr.getvalue())

    def test_log_file_records_progress_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cookies.json"
            log_file = Path(directory) / "vkpw.log"
            with (
                patch("vkpw.cli._setup_streams"),
                patch("vkpw.cli.sys.stdin", Mock(isatty=Mock(return_value=False))),
                patch(
                    "vkpw.cli._run_login",
                    new_callable=AsyncMock,
                    return_value=(123, {"p": "private-p", "remixsid": "private-sid"}),
                ),
                redirect_stderr(io.StringIO()),
            ):
                result = main(
                    ["login:secret", "-o", str(output), "-l", str(log_file)]
                )

            self.assertEqual(result, 0)
            log_contents = log_file.read_text(encoding="utf-8")
            self.assertIn("Запуск процесса входа", log_contents)
            self.assertIn("Начинаю авторизацию VK", log_contents)
            self.assertIn("Авторизация VK завершена", log_contents)
            self.assertIn("Cookies успешно сохранены", log_contents)
            for secret in ("secret", "private-p", "private-sid"):
                self.assertNotIn(secret, log_contents)

    def test_log_file_records_authentication_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cookies.json"
            log_file = Path(directory) / "vkpw.log"
            with (
                patch("vkpw.cli._setup_streams"),
                patch("vkpw.cli.sys.stdin", Mock(isatty=Mock(return_value=False))),
                patch(
                    "vkpw.cli._run_login",
                    new_callable=AsyncMock,
                    side_effect=VkpwError("VK отклонил вход"),
                ),
                redirect_stderr(io.StringIO()),
            ):
                result = main(
                    ["login:secret", "-o", str(output), "-l", str(log_file)]
                )

            self.assertEqual(result, 1)
            log_contents = log_file.read_text(encoding="utf-8")
            self.assertIn("Ошибка в процессе входа: VK отклонил вход", log_contents)
            self.assertNotIn("secret", log_contents)


class BrowserInstallCliTests(unittest.TestCase):
    def test_install_browser_confirms_the_managed_executable_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            browser_path = Path(directory) / "chrome"
            browser_path.touch()
            stderr = io.StringIO()
            with (
                patch("vkpw.cli.subprocess.run", return_value=Mock(returncode=0)),
                patch("vkpw.cli._playwright_chromium_path", return_value=browser_path),
                redirect_stderr(stderr),
            ):
                result = _install_browser()

            self.assertEqual(result, 0)
            self.assertIn(str(browser_path), stderr.getvalue())

    def test_install_browser_does_not_report_success_without_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            browser_path = Path(directory) / "missing-chrome"
            stderr = io.StringIO()
            with (
                patch("vkpw.cli.subprocess.run", return_value=Mock(returncode=0)),
                patch("vkpw.cli._playwright_chromium_path", return_value=browser_path),
                redirect_stderr(stderr),
            ):
                result = _install_browser()

            self.assertEqual(result, 1)
            self.assertIn(str(browser_path), stderr.getvalue())
            self.assertIn("PLAYWRIGHT_BROWSERS_PATH", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
