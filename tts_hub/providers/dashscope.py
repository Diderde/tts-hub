# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""阿里云百炼适配器。CosyVoice 复刻只收公网 URL，Qwen 系走 base64 Data URL；
音色注册后需查询审核状态；合成返回 24 小时有效的下载 URL，需二次获取。"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from ..core.net import akai_haato
from ..core.types import (
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
from .base import KasumiToyama, now_ms

__all__ = ["ENROLL_PATH", "QWEN_SYNTH_PATH", "SYNTH_PATH", "TakiShiina"]

ENROLL_PATH = "/api/v1/services/audio/tts/customization"
SYNTH_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
QWEN_SYNTH_PATH = "/api/v1/services/aigc/multimodal-generation/generation"

COSYVOICE_MODELS = (
    "cosyvoice-v3.5-plus",
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3-plus",
    "cosyvoice-v3-flash",
    "cosyvoice-v2",
)
QWEN_MODELS = ("qwen3-tts-vc-2026-01-22",)

DEFAULT_SYNTH_MODEL = "cosyvoice-v3-flash"
DEFAULT_TARGET_MODEL = "cosyvoice-v3-flash"
STATUS_MAP = {"OK": STATUS_READY, "DEPLOYING": STATUS_TRAINING, "UNDEPLOYED": STATUS_FAILED}
QWEN_CHAR_LIMIT = 600
CLONE_TTL_HOURS = 24 * 365


class TakiShiina(KasumiToyama):
    """阿里云百炼适配器（name = ``aliyun``）。"""

    name = "aliyun"
    base_url = "https://dashscope.aliyuncs.com"
    default_model = DEFAULT_SYNTH_MODEL
    clone_model = DEFAULT_TARGET_MODEL
    clone_ttl_hours = CLONE_TTL_HOURS
    clone_requires_url = True

    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {
        "Throttling.RateQuota": QuotaError,
        "Throttling.AllocationQuota": QuotaError,
        "Throttling.BurstRate": QuotaError,
        "Arrearage": QuotaError,
        "isv.OUT_OF_SERVICE": QuotaError,
        "DataInspectionFailed": ReviewRejectedError,
        "data_inspection_failed": ReviewRejectedError,
        "InvalidApiKey": AuthError,
        "invalid_api_key": AuthError,
        "NOT AUTHORIZED": AuthError,
        "AccessDenied.Unpurchased": AuthError,
        "Model.AccessDenied": AuthError,
        "InvalidParameter": ProviderError,
        "BadRequest.UnsupportedFileFormat": ProviderError,
        "BadRequest.InputDownloadFailed": ProviderError,
        "BadRequest.ResourceNotExist": ProviderError,
        "BadRequest.VoiceNotFound": ProviderError,
        "Audio.AudioShortError": ProviderError,
        "Audio.AudioSilentError": ProviderError,
        "Audio.AudioRateError": ProviderError,
        "Audio.DurationLimitError": ProviderError,
        "Audio.DecoderError": ProviderError,
        "Audio.PreprocessError": ProviderError,
        "ClientDisconnect": ProviderError,
    }


    def list_models(self) -> list[ModelInfo]:
        out = [
            ModelInfo(
                id=model,
                display_name=model,
                supports_clone=True,
                supports_stream=True,
                char_limit=None,  # 官方未给出 HTTP 非实时合成的字符上限，不编造
                note="复刻音色需与本模型的 target_model 一致，不可跨模型",
            )
            for model in COSYVOICE_MODELS
        ]
        out += [
            ModelInfo(
                id=model,
                display_name=model,
                supports_clone=True,
                supports_stream=False,
                char_limit=QWEN_CHAR_LIMIT,
                note="复刻 0.01 元/个；本地文件走这条（Data URL）",
            )
            for model in QWEN_MODELS
        ]
        return out

    def list_voices(self) -> list[VoiceInfo]:
        """列出复刻音色（CosyVoice 系带 status；Qwen 系不带）。"""
        payload = self.enroll({"action": "list_voice"}, model="voice-enrollment")
        rows = ((payload.get("output") or {}).get("voice_list")) or []
        return [
            VoiceInfo(
                voice_id=str(row.get("voice_id") or row.get("voice") or ""),
                display_name=str(row.get("voice_id") or row.get("voice") or ""),
                kind="cloned",
                model=row.get("target_model"),
                created_at=row.get("gmt_create"),
                note=str(row.get("status") or ""),
            )
            for row in rows
            if row.get("voice_id") or row.get("voice")
        ]


    def enroll(self, action: Mapping[str, Any], *, model: str) -> Mapping[str, Any]:
        """调音色管理端点（创建/查询/列表/删除共用同一 URL，靠 action 区分）。"""
        payload = self.send_json(
            "POST",
            ENROLL_PATH,
            body={"model": model, "input": dict(action)},
        )
        return payload

    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        target_model: str | None = None,
    ) -> CloneTask:
        target = target_model or model or self.clone_model or DEFAULT_TARGET_MODEL
        if target in QWEN_MODELS or target.startswith("qwen"):
            return self.clone_qwen(sample, target=target, name=name, transcript=transcript)
        return self.clone_cosyvoice(sample, target=target, name=name)

    def clone_cosyvoice(
        self, sample: SampleInput, *, target: str, name: str | None = None
    ) -> CloneTask:
        """CosyVoice 系：``input.url`` 必须是**公网可访问且免鉴权**的地址。"""
        if not sample.url:
            raise ProviderError(
                "阿里云 CosyVoice 系复刻只接受公网可访问的音频 URL；"
                "本地文件请改用 target_model=qwen3-tts-vc-2026-01-22（走 Data URL）",
                vendor=self.name,
            )
        prefix = self.voice_prefix(name or sample.display_name)
        payload = self.enroll(
            {"action": "create_voice", "target_model": target, "prefix": prefix, "url": sample.url},
            model="voice-enrollment",
        )
        output = payload.get("output") or {}
        voice_id = output.get("voice_id")
        if not voice_id:
            raise ProviderError("音色注册响应缺少 output.voice_id", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_TRAINING,
            voice_id=str(voice_id),
            task_id=str(voice_id),
            model=target,
            message=f"音色已创建（{voice_id}），正在审核；用 clone_status 查 DEPLOYING/OK/UNDEPLOYED",
            raw=dict(payload),
        )

    def clone_qwen(
        self, sample: SampleInput, *, target: str, name: str | None = None, transcript: str | None = None
    ) -> CloneTask:
        """Qwen-TTS 系：``input.audio.data`` 收 base64 Data URL，本地文件走这条。"""
        if sample.url:
            data_url = sample.url
        else:
            raw = sample.read_bytes()
            mime = sample.mime or "audio/wav"
            data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        action: dict[str, Any] = {
            "action": "create",
            "target_model": target,
            "preferred_name": self.voice_prefix(name or sample.display_name),
            "audio": {"data": data_url},
        }
        text = transcript if transcript is not None else sample.transcript
        if text:
            action["text"] = text
        payload = self.enroll(action, model="qwen-voice-enrollment")
        output = payload.get("output") or {}
        voice_id = output.get("voice")
        if not voice_id:
            raise ProviderError("音色注册响应缺少 output.voice", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(voice_id),
            task_id=str(voice_id),
            model=target,
            message="复刻成功（Qwen-TTS 系同步返回，无审核查询接口）",
            raw=dict(payload),
        )

    @staticmethod
    def voice_prefix(name: str) -> str:
        """按官方约束生成名称前缀：只允许数字与字母，最多 10 字符。"""
        cleaned = "".join(ch for ch in name if ch.isascii() and ch.isalnum())
        return (cleaned or "ttshub")[:10]

    def clone_status(self, task_id: str) -> CloneTask:
        """查询音色审核状态（``task_id`` 即音色 ID）。"""
        payload = self.enroll({"action": "query_voice", "voice_id": task_id}, model="voice-enrollment")
        output = payload.get("output") or {}
        raw_status = str(output.get("status") or "").upper()
        status = STATUS_MAP.get(raw_status, STATUS_TRAINING)
        message = {
            STATUS_READY: "审核通过",
            STATUS_TRAINING: "审核中",
            STATUS_FAILED: "审核未通过（该音色不可用）",
        }.get(status, f"未知状态 {raw_status!r}")
        return CloneTask(
            vendor=self.name,
            status=status,
            voice_id=task_id if status == STATUS_READY else None,
            task_id=task_id,
            model=output.get("target_model"),
            message=message,
            raw=dict(payload),
        )

    def delete_voice(self, voice_id: str) -> None:
        self.enroll({"action": "delete_voice", "voice_id": voice_id}, model="voice-enrollment")


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
    ) -> AudioResult:
        chosen = model or self.default_model or DEFAULT_SYNTH_MODEL
        ref = VoiceRef.of(voice, vendor=self.name, model=chosen)
        if chosen.startswith("qwen"):
            if len(text) > QWEN_CHAR_LIMIT:
                raise ProviderError(
                    f"文本 {len(text)} 字符超过 Qwen-TTS 单次上限 {QWEN_CHAR_LIMIT}",
                    vendor=self.name,
                )
            path = QWEN_SYNTH_PATH
            body = {"model": chosen, "input": {"text": text, "voice": ref.raw}}
        else:
            path = SYNTH_PATH
            body = {
                "model": chosen,
                "input": {"text": text, "voice": ref.raw, "format": "mp3", "sample_rate": 22050},
            }
        started = now_ms()
        if stream:
            return AudioResult(
                vendor=self.name,
                model=chosen,
                voice_id=ref.raw,
                format="mp3",
                sample_rate=22050,
                chars=len(text),
                stream=True,
                chunks=self.stream_speech(path, body),
            )
        payload = self.send_json("POST", path, body=body)
        audio_url = self.audio_url(payload)
        audio = self.http.download(audio_url)
        usage = payload.get("usage") or {}
        return AudioResult(
            vendor=self.name,
            model=chosen,
            voice_id=ref.raw,
            audio=audio,
            format="mp3",
            sample_rate=22050,
            chars=int(usage.get("characters") or len(text)),
            latency_ms=int(now_ms() - started),
        )

    @staticmethod
    def audio_url(payload: Mapping[str, Any]) -> str:
        """取 ``output.audio.url``；顺手把"音色与模型不匹配"这类错认成可读原因。"""
        output = payload.get("output") or {}
        audio = output.get("audio") or {}
        url = audio.get("url")
        if url:
            return str(url)
        message = str(output.get("finish_reason") or payload.get("message") or "")
        raise ProviderError(
            "合成响应里没有 output.audio.url"
            + (f"（finish_reason={message}）" if message else "")
            + "；常见原因：复刻音色的 target_model 与本次 model 不一致",
            vendor="aliyun",
            raw=payload,
        )

    def stream_speech(self, path: str, body: Mapping[str, Any]) -> Iterator[bytes]:
        """SSE 流式：分片在 ``output.audio.data``，是 base64 音频。"""
        head = self.headers()
        head["X-DashScope-SSE"] = "enable"
        head["Accept"] = "text/event-stream"
        lines = self.http.stream_bytes(
            "POST",
            self.url(path),
            headers=head,
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
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
            if event.get("code"):
                self.classify(400, event, raw=event)
            piece = ((event.get("output") or {}).get("audio") or {}).get("data")
            if piece:
                yield base64.b64decode(piece)
