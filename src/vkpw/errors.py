from __future__ import annotations


class VkpwError(RuntimeError):
    """Ошибка браузерной авторизации или сохранения cookies."""


class BrowserNotFoundError(VkpwError):
    """Не найден совместимый браузер."""


class ManualVerificationRequired(VkpwError):
    """VK требует CAPTCHA, одноразовый код или другое ручное подтверждение."""


class StoreError(VkpwError):
    """Файл cookies имеет неподдерживаемую или повреждённую структуру."""
