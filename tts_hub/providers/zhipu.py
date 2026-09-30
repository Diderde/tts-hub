# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""智谱 AI 适配器。复刻 POST /voice/clone（试听文本 input 必填）；
合成 POST /audio/speech（非流式响应为二进制 wav）。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
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

__all__ = ["RanMitake"]

SYSTEM_VOICES = {
    "tongtong": "彤彤（默认）",
    "chuichui": "锤锤",
    "xiaochen": "小陈",
    "jam": "动动动物圈 jam",
    "kazi": "动动动物圈 kazi",
    "douji": "动动动物圈 douji",
    "luodo": "动动动物圈 luodo",
}

CLONE_MODEL = "glm-tts-clone"
SYNTH_MODEL = "glm-tts"
CHAR_LIMIT = 1024
DEFAULT_PREVIEW_TEXT = "欢迎使用声音复刻服务，这是一段用于试听的示例语音。"


class RanMitake(KasumiToyama):
    """智谱 AI 适配器（name = ``zhipu``）。"""

    name = "zhipu"
    base_url = "https://open.bigmodel.cn/api/paas/v4"
    default_model = SYNTH_MODEL
    clone_model = CLONE_MODEL

    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {
        "1000": AuthError,
        "1001": AuthError,
        "1003": AuthError,
        "1005": AuthError,
        "1220": AuthError,
        "1113": QuotaError,
        "1302": QuotaError,
        "1305": QuotaError,
        "1308": QuotaError,
        "1301": ReviewRejectedError,
    }


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=SYNTH_MODEL,
                display_name="GLM-TTS",
                supports_clone=False,
                supports_stream=True,
                char_limit=CHAR_LIMIT,
                note="复刻音色与系统音色共用 voice 参数",
            ),
            ModelInfo(
                id=CLONE_MODEL,
                display_name="GLM-TTS-Clone",
                supports_clone=True,
                supports_stream=False,
                note="仅用于音色复刻（6 元/次），不用于合成",
            ),
        ]

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("GET", "/voice/list")
        rows = payload.get("voice_list") or []
        if not isinstance(rows, list):
            raise ProviderError("音色列表响应缺少 voice_list 数组", vendor=self.name, raw=payload)
        cloned = [
            VoiceInfo(
                voice_id=str(row.get("voice", "")),
                display_name=str(row.get("voice_name") or row.get("voice", "")),
                kind="cloned",
                created_at=row.get("create_time"),
                note=row.get("download_url") or "",
            )
            for row in rows
            if row.get("voice")
        ]
        system = [
            VoiceInfo(voice_id=vid, display_name=label, kind="system", note="系统音色")
            for vid, label in SYSTEM_VOICES.items()
        ]
        return cloned + system


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str = CLONE_MODEL,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
    ) -> CloneTask:
        if model != CLONE_MODEL:
            raise ProviderError(
                f"智谱复刻只支持 model={CLONE_MODEL!r}，收到 {model!r}", vendor=self.name
            )
        if not name:
            raise ProviderError("智谱复刻要求提供音色名（voice_name）", vendor=self.name)
        file_id = self.upload_sample(sample)
        body: dict[str, Any] = {
            "model": model,
            "voice_name": name,
            "input": preview_text or DEFAULT_PREVIEW_TEXT,
            "file_id": file_id,
        }
        text = transcript if transcript is not None else sample.transcript
        if text:
            body["text"] = text  # 样本源文本，选填；给了能提升复刻质量
        payload = self.send_json("POST", "/voice/clone", body=body)
        voice_id = payload.get("voice")
        if not voice_id:
            raise ProviderError("复刻响应缺少 voice 字段（音色 ID）", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(voice_id),
            task_id=str(voice_id),
            model=model,
            message="复刻成功",
            raw=dict(payload),
        )

    def upload_sample(self, sample: SampleInput) -> str:
        """上传样本音频，返回 file_id（``purpose=voice-clone-input``）。"""
        filename, mime, raw = self.sample_bytes(sample)
        payload = self.send_json(
            "POST",
            "/files",
            data={"purpose": "voice-clone-input"},
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
            message="智谱为同步复刻，无轮询阶段",
        )

    def delete_voice(self, voice_id: str) -> None:
        """删除复刻音色（注意官方端点是 POST，不是 HTTP DELETE）。"""
        self.send_json("POST", "/voice/delete", body={"voice": voice_id})


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str = SYNTH_MODEL,
        stream: bool = False,
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model)
        if model != SYNTH_MODEL:
            raise ProviderError(
                f"智谱合成只支持 model={SYNTH_MODEL!r}，收到 {model!r}", vendor=self.name
            )
        if len(text) > CHAR_LIMIT:
            raise ProviderError(
                f"文本 {len(text)} 字符超过智谱单次上限 {CHAR_LIMIT}，请切分", vendor=self.name
            )
        started = now_ms()
        if stream:
            body = {"model": model, "input": text, "voice": ref.raw, "stream": True}
            return AudioResult(
                vendor=self.name,
                model=model,
                voice_id=ref.raw,
                format="pcm",
                sample_rate=24000,
                chars=len(text),
                stream=True,
                chunks=self.stream_speech(body),
            )
        body = {"model": model, "input": text, "voice": ref.raw, "response_format": "wav"}
        audio = self.send_bytes("POST", "/audio/speech", body=body)
        return AudioResult(
            vendor=self.name,
            model=model,
            voice_id=ref.raw,
            audio=audio,
            format="wav",
            sample_rate=24000,
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )

    def stream_speech(self, body: Mapping[str, Any]) -> Iterator[bytes]:
        """SSE 流：分片在 ``choices[0].delta.content``，是 base64 音频。"""
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
            if "error" in event:
                code, message = str(event["error"].get("code")), str(event["error"].get("message"))
                self.classify(400, {"error": {"code": code, "message": message}}, raw=event)
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield base64.b64decode(piece)
