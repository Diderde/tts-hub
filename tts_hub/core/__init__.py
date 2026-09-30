# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""核心层：类型、错误、协议、出站网络、可选音频工具。

本层只依赖标准库与 httpx，不含任何厂商知识——厂商差异全部下沉到
:mod:`tts_hub.providers`。
"""

from __future__ import annotations

from .errors import (
    AuthError,
    ProviderError,
    QuotaError,
    ReviewRejectedError,
    TTSHubError,
)
from .net import MocaAoba, akai_haato, hoshimachi_suisei, natsuiro_matsuri, robocosan, sakura_miko
from .polling import ChisatoShirasagi
from .provider import TTSProvider
from .types import (
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

__all__ = [
    "STATUS_DISABLED",
    "STATUS_FAILED",
    "STATUS_READY",
    "STATUS_TRAINING",
    "AudioResult",
    "AuthError",
    "ChisatoShirasagi",
    "CloneTask",
    "MocaAoba",
    "ModelInfo",
    "ProviderError",
    "QuotaError",
    "ReviewRejectedError",
    "SampleInput",
    "TTSHubError",
    "TTSProvider",
    "VoiceInfo",
    "VoiceRef",
    "akai_haato",
    "hoshimachi_suisei",
    "natsuiro_matsuri",
    "robocosan",
    "sakura_miko",
]
