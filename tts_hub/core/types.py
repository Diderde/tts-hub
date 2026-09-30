# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""统一数据模型：音色、克隆任务、合成结果、样本输入。"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "STATUS_DISABLED",
    "STATUS_FAILED",
    "STATUS_READY",
    "STATUS_TRAINING",
    "AudioResult",
    "CloneTask",
    "ModelInfo",
    "SampleInput",
    "VoiceInfo",
    "VoiceRef",
]

STATUS_READY = "ready"
STATUS_TRAINING = "training"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """厂商侧一个可用的合成/克隆模型。"""

    id: str
    display_name: str = ""
    supports_clone: bool = False
    supports_stream: bool = False
    char_limit: int | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "supports_clone": self.supports_clone,
            "supports_stream": self.supports_stream,
            "char_limit": self.char_limit,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    """厂商侧的一个音色（系统音色或已克隆音色）。"""

    voice_id: str
    display_name: str = ""
    kind: str = "system"  # system / cloned
    model: str | None = None
    created_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "model": self.model,
            "created_at": self.created_at,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class VoiceRef:
    """音色引用。

    ``raw`` 既可能是厂商内 voice_id，也可能是逻辑音色名；由 ``TTSHub`` 决定
    是否先查注册表解析。adapter 只认 ``raw``。
    """

    raw: str
    vendor: str | None = None
    model: str | None = None
    logical: bool = False

    @classmethod
    def of(cls, value: VoiceRef | str, **kw: Any) -> VoiceRef:
        if isinstance(value, VoiceRef):
            return value
        return cls(raw=str(value), **kw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "vendor": self.vendor,
            "model": self.model,
            "logical": self.logical,
        }


@dataclass(frozen=True, slots=True)
class SampleInput:
    """克隆样本：本地路径 / 公网 URL / 原始字节，三者取其一。

    URL 形式必须先过出站安全校验（拒绝环回/内网/保留地址）才能下载或转发，
    这一步由 :mod:`tts_hub.core.net` 负责，本类型自身不做网络访问。
    """

    path: pathlib.Path | None = None
    url: str | None = None
    data: bytes | None = None
    filename: str | None = None
    mime: str | None = None
    transcript: str | None = None  # 样本里说的内容；部分厂商（智谱/阶跃）需要

    @classmethod
    def from_path(cls, path: str | pathlib.Path, **kw: Any) -> SampleInput:
        return cls(path=pathlib.Path(path), **kw)

    @classmethod
    def from_bytes(cls, data: bytes, filename: str, **kw: Any) -> SampleInput:
        return cls(data=data, filename=filename, **kw)

    @classmethod
    def from_url(cls, url: str, **kw: Any) -> SampleInput:
        return cls(url=url, **kw)

    def supplied(self) -> bool:
        """是否真的给了样本内容（三者恰有其一才算合法）。"""
        return sum(x is not None for x in (self.path, self.url, self.data)) == 1

    def read_bytes(self) -> bytes:
        """读出样本字节；URL 形式拒绝在此直接读取（必须先经安全校验）。"""
        if self.data is not None:
            return self.data
        if self.path is not None:
            return pathlib.Path(self.path).read_bytes()
        raise ValueError("SampleInput 无本地内容（url 形式需先经出站校验下载）")

    @property
    def display_name(self) -> str:
        if self.filename:
            return self.filename
        if self.path is not None:
            return pathlib.Path(self.path).name
        if self.url:
            return self.url.rsplit("/", 1)[-1] or "sample"
        return "sample"

    def with_data(self, data: bytes, filename: str | None = None) -> SampleInput:
        """返回一个内容已落地的新样本（用于 URL 下载后转存）。"""
        return SampleInput(
            data=data,
            filename=filename or self.display_name,
            mime=self.mime,
            transcript=self.transcript,
        )


@dataclass(frozen=True, slots=True)
class CloneTask:
    """克隆任务。

    同步返回型厂商（MiniMax/阶跃/智谱）直接给 ``status=ready`` + ``voice_id``；
    轮询型厂商（P3 起的腾讯/讯飞/阿里）先给 ``task_id`` + ``status=training``。
    """

    vendor: str
    status: str = STATUS_READY
    voice_id: str | None = None
    task_id: str | None = None
    model: str | None = None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status == STATUS_READY

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "status": self.status,
            "voice_id": self.voice_id,
            "task_id": self.task_id,
            "model": self.model,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class AudioResult:
    """合成产物。

    非流式填 ``audio``；流式填 ``chunks``（字节块迭代器），二者互斥。
    """

    vendor: str = ""
    model: str = ""
    voice_id: str = ""
    audio: bytes | None = None
    format: str = "mp3"
    sample_rate: int | None = None
    chars: int = 0
    latency_ms: int = 0
    stream: bool = False
    chunks: Iterable[bytes] | None = None

    def iter_bytes(self) -> Iterator[bytes]:
        """统一成字节块迭代器：非流式产出一块，流式原样透传。"""
        if self.chunks is not None:
            yield from self.chunks
        elif self.audio is not None:
            yield self.audio

    def write_to(self, target: str | pathlib.Path) -> int:
        """把音频写到目标路径，返回写入字节数。"""
        path = pathlib.Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with path.open("wb") as fh:
            for chunk in self.iter_bytes():
                if chunk:
                    fh.write(chunk)
                    total += len(chunk)
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "voice_id": self.voice_id,
            "format": self.format,
            "sample_rate": self.sample_rate,
            "chars": self.chars,
            "latency_ms": self.latency_ms,
            "stream": self.stream,
            "bytes": len(self.audio) if self.audio is not None else None,
        }
