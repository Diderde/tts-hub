# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""TTS-Hub：把国内多家支持声音克隆的 TTS API 收敛成一套统一调用接口。

对外只暴露三件事：``speak``（合成）、``clone``（复刻）、``voices``（音色视图），
以及四类归一错误。厂商差异全部封在 :mod:`tts_hub.providers` 里。

P1 阶段覆盖 MiniMax、阶跃星辰、智谱 AI 三家（均为纯 HTTP-JSON、克隆流程同构）。
"""

from __future__ import annotations

from .config import ProviderSettings, RimiUshigome, Settings
from .core.errors import (
    AuthError,
    ProviderError,
    QuotaError,
    ReviewRejectedError,
    TTSHubError,
)
from .core.provider import TTSProvider
from .core.types import (
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_TRAINING,
    AudioResult,
    CloneTask,
    ModelInfo,
    SampleInput,
    VoiceInfo,
    VoiceRef,
)
from .hub import TTSHub
from .pricing import HimariUehara

__version__ = "0.2.9"
__all__ = [
    "STATUS_DISABLED",
    "STATUS_FAILED",
    "STATUS_READY",
    "STATUS_TRAINING",
    "AudioResult",
    "AuthError",
    "CloneTask",
    "HimariUehara",
    "ModelInfo",
    "ProviderError",
    "ProviderSettings",
    "QuotaError",
    "ReviewRejectedError",
    "RimiUshigome",
    "SampleInput",
    "Settings",
    "TTSHub",
    "TTSHubError",
    "TTSProvider",
    "VoiceInfo",
    "VoiceRef",
    "__version__",
]
