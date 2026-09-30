# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""FastAPI 出口层（P2 核心 REST + P5 管理台）。

可选依赖面：导入本包会连带导入 ``fastapi``。``tts_hub`` 顶层**不**导入它，
所以"没装 fastapi 也能用 SDK 与 CLI"这条承诺仍然成立。
"""

from __future__ import annotations

from .app import MAX_SAMPLE_BYTES, MEDIA_TYPES, STATUS_BY_KIND, create_app
from .console import ASSETS, WEB_DIR

__all__ = [
    "ASSETS",
    "MAX_SAMPLE_BYTES",
    "MEDIA_TYPES",
    "STATUS_BY_KIND",
    "WEB_DIR",
    "create_app",
]
