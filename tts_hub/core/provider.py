# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""厂商适配器的统一契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import AudioResult, CloneTask, ModelInfo, SampleInput, VoiceInfo, VoiceRef

__all__ = ["TTSProvider"]


@runtime_checkable
class TTSProvider(Protocol):
    """一家厂商的适配器。

    与方案设计相比，``clone`` 多三个关键字参数：``name``（厂商侧音色名，智谱必需）、
    ``transcript``（样本源文本，阶跃/智谱可选但影响效果）与 ``preview_text``
    （复刻时朗读的试听文本，腾讯/讯飞的一句话复刻必需）。均为向后兼容的扩充，
    不改变方案设计的调用形态。
    """

    name: str

    def list_models(self) -> list[ModelInfo]:
        """列出该厂商可用于合成/克隆的模型。"""
        ...

    def list_voices(self) -> list[VoiceInfo]:
        """列出该厂商的系统音色与已克隆音色。"""
        ...

    def clone(
        self,
        sample: SampleInput,
        *,
        model: str,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
    ) -> CloneTask:
        """提交克隆；同步型厂商直接返回 ``ready`` + ``voice_id``。"""
        ...

    def clone_status(self, task_id: str) -> CloneTask:
        """轮询克隆任务；同步型厂商可原样回显终态。"""
        ...

    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef,
        model: str,
        stream: bool = False,
    ) -> AudioResult:
        """合成语音；``stream=True`` 时结果里是字节块迭代器。"""
        ...
