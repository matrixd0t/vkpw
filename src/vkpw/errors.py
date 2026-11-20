from __future__ import annotations

from pathlib import Path


class VkpwError(RuntimeError):
    """Ошибка браузерной авторизации или сохранения cookies."""

    screenshot_path: Path | None = None
    """Путь к скриншоту страницы при ошибке, если он сохранялся."""


class BrowserNotFoundError(VkpwError):
    """Не найден совместимый браузер."""


class ManualVerificationRequired(VkpwError):
    """VK требует CAPTCHA, одноразовый код или другое ручное подтверждение."""


class StoreError(VkpwError):
    """Файл cookies имеет неподдерживаемую или повреждённую структуру."""
