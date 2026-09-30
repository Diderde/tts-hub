# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""阶跃星辰适配器。合成 POST /v1/audio/speech；复刻 POST /v1/audio/voices
（复刻仅支持 stepaudio-2.5-tts / step-tts-2 / step-tts-mini）。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Mapping
from typing import Any

from ..core.errors import ProviderError
from ..core.net import akai_haato
from ..core.types import (
    STATUS_READY,
    AudioResult,
    CloneTask,
    ModelInfo,
    SampleInput,
    VoiceInfo,
    VoiceRef,
)
from .base import KasumiToyama, now_ms

__all__ = ["ArisaIchigaya"]

CLONE_MODELS = ("stepaudio-2.5-tts", "step-tts-2", "step-tts-mini")
SYNTH_MODELS = ("stepaudio-3-tts", "stepaudio-2.5-tts", "step-tts-2", "step-tts-mini")
DEFAULT_CLONE_MODEL = "step-tts-2"
DEFAULT_SYNTH_MODEL = "stepaudio-2.5-tts"
CHAR_LIMIT = 1000


class ArisaIchigaya(KasumiToyama):
    """阶跃星辰适配器（name = ``stepfun``）。"""

    name = "stepfun"
    base_url = "https://api.stepfun.com/v1"
    default_model = DEFAULT_SYNTH_MODEL
    clone_model = DEFAULT_CLONE_MODEL


    def list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for model in SYNTH_MODELS:
            out.append(
                ModelInfo(
                    id=model,
                    display_name=model,
                    supports_clone=model in CLONE_MODELS,
                    supports_stream=True,
                    char_limit=CHAR_LIMIT,
                    note="" if model in CLONE_MODELS else "不支持音色复刻",
                )
            )
        return out

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("GET", "/audio/voices", params={"limit": 100})
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            raise ProviderError("音色列表响应缺少 data 数组", vendor=self.name, raw=payload)
        return [
            VoiceInfo(
                voice_id=str(row.get("id", "")),
                display_name=str(row.get("id", "")),
                kind="cloned",
                created_at=str(row.get("created_at")) if row.get("created_at") else None,
            )
            for row in rows
            if row.get("id")
        ]


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str = DEFAULT_CLONE_MODEL,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
    ) -> CloneTask:
        if model not in CLONE_MODELS:
            raise ProviderError(
                f"阶跃复刻不支持 model={model!r}；可选：{', '.join(CLONE_MODELS)}"
                "（stepaudio-3-tts 仅可用于合成）",
                vendor=self.name,
            )
        if preview_text is not None:
            raise ProviderError(
                "阶跃的复刻试听（voices-preview）本 adapter 未实现，preview_text 无法生效；"
                "如需试听请去掉 preview_text 后直接对复刻音色调用合成",
                vendor=self.name,
            )
        file_id = self.upload_sample(sample)
        body: dict[str, Any] = {"file_id": file_id, "model": model}
        text = transcript if transcript is not None else sample.transcript
        if text:
            body["text"] = text  # 不传则由平台 ASR 解析，但不传效果与耗时不控
        payload = self.send_json("POST", "/audio/voices", body=body, expect=(200, 201))
        voice_id = payload.get("id")
        if not voice_id:
            raise ProviderError("复刻响应缺少 id 字段（音色 ID）", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(voice_id),
            task_id=str(voice_id),
            model=model,
            message="复刻成功" + ("（该音色此前已创建，平台标记为重复请求）" if payload.get("duplicated") else ""),
            raw=dict(payload),
        )

    def upload_sample(self, sample: SampleInput) -> str:
        """上传样本，返回 file_id（复刻与试听共用）。"""
        filename, mime, raw = self.sample_bytes(sample)
        payload = self.send_json(
            "POST",
            "/files",
            data={"purpose": "storage"},
            files={"file": (filename, raw, mime)},
            expect=(200, 201),
        )
        file_id = payload.get("id")
        if not file_id:
            raise ProviderError("文件上传响应缺少 id（file_id）", vendor=self.name, raw=payload)
        return str(file_id)

    def clone_status(self, task_id: str) -> CloneTask:
        """同步型厂商：无轮询阶段，原样回显终态。"""
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=task_id,
            task_id=task_id,
            message="阶跃为同步复刻，无轮询阶段",
        )


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str = DEFAULT_SYNTH_MODEL,
        stream: bool = False,
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model)
        if model not in SYNTH_MODELS:
            raise ProviderError(
                f"未知合成模型 {model!r}；可选：{', '.join(SYNTH_MODELS)}", vendor=self.name
            )
        if len(text) > CHAR_LIMIT:
            raise ProviderError(
                f"文本 {len(text)} 字符超过阶跃单次上限 {CHAR_LIMIT}", vendor=self.name
            )
        body: dict[str, Any] = {"model": model, "input": text, "voice": ref.raw}
        started = now_ms()
        if stream:
            body["stream_format"] = "sse"
            chunks = self.stream_speech(body, started)
            return AudioResult(
                vendor=self.name,
                model=model,
                voice_id=ref.raw,
                format="mp3",
                chars=len(text),
                stream=True,
                chunks=chunks,
            )
        body["response_format"] = "mp3"
        audio = self.send_bytes("POST", "/audio/speech", body=body)
        return AudioResult(
            vendor=self.name,
            model=model,
            voice_id=ref.raw,
            audio=audio,
            format="mp3",
            sample_rate=24000,
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )

    def stream_speech(self, body: Mapping[str, Any], started: float) -> Iterator[bytes]:
        """SSE 流：分片是 base64 编码的音频片段。"""
        del started  # 首包耗时由调用方按需统计，这里不做无谓记账
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = self.headers()
        head["Accept"] = "text/event-stream"
        lines = self.http.stream_bytes(
            "POST", self.url("/audio/speech"), headers=head, content=payload
        )
        for line in akai_haato(lines):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if event.get("type") == "speech.audio.done":
                return
            piece = event.get("audio")
            if piece:
                yield base64.b64decode(piece)
