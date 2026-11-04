from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .auth import REQUIRED_COOKIES
from .errors import StoreError


def build_entry(
    cookies: dict[str, str],
    user_id: int,
    *,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Собрать запись в совместимом с vkqr и vkx формате."""
    missing = [name for name in REQUIRED_COOKIES if not cookies.get(name)]
    if missing:
        raise StoreError(f"В cookies отсутствуют обязательные значения: {', '.join(missing)}")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise StoreError("Для сохранения cookies требуется корректный user_id")
    return {
        "created_at": int(time.time()) if created_at is None else int(created_at),
        "user_id": user_id,
        "p": cookies["p"],
        "remixsid": cookies["remixsid"],
    }


def load_store(path: str | Path) -> dict[str, Any]:
    """Прочитать JSONStore vkqr/vkx, не изменяя файл при ошибочной структуре."""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return {"cookies": []}
    try:
        text = file_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise StoreError(f"Не удалось прочитать {file_path}: {exc}") from exc
    if not text:
        return {"cookies": []}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StoreError(f"Файл {file_path} содержит некорректный JSON") from exc
    if not isinstance(data, dict):
        raise StoreError(f"Файл {file_path}: ожидался JSON-объект верхнего уровня")
    entries = data.get("cookies")
    if entries is None:
        data["cookies"] = []
    elif not isinstance(entries, list):
        raise StoreError(f'Файл {file_path}: поле "cookies" должно быть списком')
    return data


def append_entry(path: str | Path, entry: dict[str, Any]) -> Path:
    """Атомарно дописать запись, сохранив остальные поля и старые аккаунты."""
    file_path = Path(path).expanduser()
    data = load_store(file_path)
    data["cookies"].append(entry)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            dir=file_path.parent,
            text=True,
        )
    except OSError as exc:
        raise StoreError(f"Не удалось подготовить файл {file_path}: {exc}") from exc

    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
            json.dump(data, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, file_path)
        if os.name != "nt":
            file_path.chmod(0o600)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise StoreError(f"Не удалось сохранить cookies в {file_path}: {exc}") from exc
    return file_path
