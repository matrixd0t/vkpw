from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from vkpw.errors import StoreError
from vkpw.storage import append_entry, build_entry, load_store


class StorageTests(unittest.TestCase):
    def test_build_entry_matches_vkqr_format(self) -> None:
        entry = build_entry({"p": "p-value", "remixsid": "sid-value", "other": "ignored"}, 123, created_at=456)
        self.assertEqual(
            entry,
            {"created_at": 456, "user_id": 123, "p": "p-value", "remixsid": "sid-value"},
        )

    def test_build_entry_requires_session_cookies_and_positive_user(self) -> None:
        with self.assertRaises(StoreError):
            build_entry({"p": "p-value"}, 123)
        with self.assertRaises(StoreError):
            build_entry({"p": "p-value", "remixsid": "sid-value"}, 0)

    def test_append_creates_vkqr_compatible_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "cookies.json"
            entry = build_entry({"p": "p-value", "remixsid": "sid-value"}, 123, created_at=456)

            saved = append_entry(path, entry)

            data = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(data, {"cookies": [entry]})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)

    def test_append_preserves_existing_entries_and_vkx_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            old_entry = {"created_at": 1, "user_id": 10, "p": "old-p", "remixsid": "old-sid"}
            original = {"cookies": [old_entry], "_vkx": {"states": [{"source": "keep"}]}, "extra": True}
            path.write_text(json.dumps(original), encoding="utf-8")
            new_entry = build_entry({"p": "new-p", "remixsid": "new-sid"}, 20, created_at=2)

            append_entry(path, new_entry)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cookies"], [old_entry, new_entry])
            self.assertEqual(saved["_vkx"], original["_vkx"])
            self.assertTrue(saved["extra"])

    def test_invalid_json_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            path.write_text("{broken", encoding="utf-8")
            before = path.read_bytes()
            entry = build_entry({"p": "p-value", "remixsid": "sid-value"}, 123)

            with self.assertRaises(StoreError):
                append_entry(path, entry)

            self.assertEqual(path.read_bytes(), before)

    def test_invalid_cookie_container_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.json"
            path.write_text('{"cookies": {}}', encoding="utf-8")
            with self.assertRaises(StoreError):
                load_store(path)


if __name__ == "__main__":
    unittest.main()
