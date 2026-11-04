from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from vkpw.cli import _parse_credentials, main
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
        ):
            with self.assertRaises(SystemExit) as raised:
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


if __name__ == "__main__":
    unittest.main()
