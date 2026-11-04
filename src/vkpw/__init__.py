"""Браузерная авторизация ВКонтакте и сохранение cookies."""

from __future__ import annotations

from .auth import BrowserSession, authenticate
from .errors import BrowserNotFoundError, ManualVerificationRequired, VkpwError

__all__ = [
    "BrowserNotFoundError",
    "BrowserSession",
    "ManualVerificationRequired",
    "VkpwError",
    "authenticate",
]

__version__ = "0.1.0"
