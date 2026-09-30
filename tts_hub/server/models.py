# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""HTTP 请求体模型；类名构成 /openapi.json 的对外契约。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TtsRequest",
    "VoiceBindRequest",
    "VoiceCreateRequest",
    "VoiceDefaultRequest",
    "VoiceUpdateRequest",
]


class TtsRequest(BaseModel):
    """``POST /api/tts`` 与 ``POST /api/tts/stream`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, description="待合成文本")
    voice: str = Field(..., min_length=1, description="逻辑音色名/ID，或厂商内 voice_id")
    vendor: str | None = Field(None, description="缺省用 providers.yaml 的 default_vendor")
    model: str | None = Field(None, description="缺省用该厂商的默认合成模型")
    fallback: bool = Field(False, description="为真时按 providers.yaml 的回退链依次尝试")
    format: str | None = Field(None, description="仅供流式接口使用，缺省 mp3")


class VoiceCreateRequest(BaseModel):
    """``POST /api/voices`` 的请求体：建逻辑音色，可同时绑定一家厂商音色。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="逻辑音色名")
    tags: str = Field("", description="逗号分隔的标签")
    vendor: str | None = Field(None, description="给了就同时建一条绑定")
    vendor_voice_id: str | None = Field(None, description="厂商侧音色 ID")
    model: str | None = Field(None, description="绑定所用模型")
    status: str = Field("ready", description="ready / training / failed / disabled")


class VoiceBindRequest(BaseModel):
    """``POST /api/voices/{id}/bindings`` 的请求体：给已有逻辑音色补一条厂商绑定。"""

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(..., min_length=1, description="厂商标识")
    vendor_voice_id: str = Field(..., min_length=1, description="厂商侧音色 ID")
    model: str | None = Field(None, description="绑定所用模型")
    status: str = Field("ready", description="ready / training / failed / disabled")


class VoiceDefaultRequest(BaseModel):
    """``POST /api/voices/{id}/default`` 的请求体：把某厂商绑定设为该音色的首选。"""

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(..., min_length=1, description="要设为默认的厂商")


class VoiceUpdateRequest(BaseModel):
    """``PATCH /api/voices/{id}`` 的请求体：改逻辑音色的名字/标签（只改给了值的项）。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, description="新名字；不改就别给")
    tags: str | None = Field(None, description="新标签；给空串表示清空")
