# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""云知声 Token Hub 适配器。无 REST 同步合成：实现为提交异步任务、
轮询到终态后取音频；voice_id 由调用方生成（规则同 MiniMax）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from ..core.polling import ChisatoShirasagi
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
from .minimax import aki_rosenthal  # voice_id 规则两家一致，复用同一生成器

__all__ = ["CHAR_LIMIT_BY_MODEL", "SYNTH_MODELS", "MutsumiWakaba"]

SYNTH_MODELS = ("u2-tts", "u2-tts-clone", "u2-tts-design")
CHAR_LIMIT_BY_MODEL = {"u2-tts": 50_000, "u2-tts-clone": 20_000, "u2-tts-design": 20_000}
DEFAULT_CHAR_LIMIT = 20_000
DEFAULT_SYNTH_MODEL = "u2-tts"
DEFAULT_CLONE_MODEL = "u2-tts-clone"
TASK_DONE = "Success"
TASK_FAILED = "Failed"
OUTPUT_PURPOSES = ("t2a_async_output", "t2a_async")


class MutsumiWakaba(KasumiToyama):
    """云知声适配器（name = ``unisound``）。"""

    name = "unisound"
    base_url = "https://maas-api.unisound.com/v1"
    default_model = DEFAULT_SYNTH_MODEL
    clone_model = DEFAULT_CLONE_MODEL

    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {
        "100101": AuthError,
        "100102": AuthError,
        "100103": AuthError,
        "100105": AuthError,
        "100106": AuthError,
        "100104": QuotaError,  # 需充值
        "100501": QuotaError,
        "220107": QuotaError,
        "220108": QuotaError,
        "220109": QuotaError,
        "100004": ReviewRejectedError,
        "100001": ProviderError,
        "100002": ProviderError,
        "100003": ProviderError,  # voice_id 重复
        "100099": ProviderError,
        "100301": ProviderError,
        "220001": ProviderError,
        "221001": ProviderError,
    }

    def __init__(self, api_key: str, **kw: Any) -> None:
        super().__init__(api_key, **kw)
        self.poller = ChisatoShirasagi(interval=2.0)


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=model,
                display_name=model,
                supports_clone=model != "u2-tts-design",
                supports_stream=False,  # 同步流式只有 WSS，纯 HTTP 不可用
                char_limit=CHAR_LIMIT_BY_MODEL.get(model, DEFAULT_CHAR_LIMIT),
                note="合成走 REST 异步任务；同步流式仅 WSS，本 adapter 未实现",
            )
            for model in SYNTH_MODELS
        ]

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("POST", "/audio/voices/query", body={"voice_type": "all"})
        out: list[VoiceInfo] = []
        for key, kind in (
            ("system_voice", "system"),
            ("voice_cloning", "cloned"),
            ("voice_design", "cloned"),
        ):
            for row in payload.get(key) or []:
                vid = row.get("voice_id")
                if not vid:
                    continue
                out.append(
                    VoiceInfo(
                        voice_id=str(vid),
                        display_name=str(row.get("voice_name") or vid),
                        kind=kind,
                        created_at=str(row.get("created_time")) if row.get("created_time") else None,
                    )
                )
        return out


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        voice_id: str | None = None,
        clone_prompt: SampleInput | None = None,
        prompt_text: str | None = None,
    ) -> CloneTask:
        file_id = self.upload_sample(sample, purpose="voice_clone")
        vid = voice_id or aki_rosenthal(name or "voice", salt=sample.display_name)
        body: dict[str, Any] = {
            "file_id": file_id,
            "voice_id": vid,
            "model": model or self.clone_model or DEFAULT_CLONE_MODEL,
        }
        if clone_prompt is not None:
            if not prompt_text:
                raise ProviderError("提供 clone_prompt 时必须同时给出 prompt_text", vendor=self.name)
            body["clone_prompt"] = {
                "prompt_audio": self.upload_sample(clone_prompt, purpose="prompt_audio"),
                "prompt_text": prompt_text,
            }
        text = preview_text if preview_text is not None else transcript
        if text:
            body["text"] = text
        payload = self.send_json("POST", "/audio/voices/clone", body=body)
        if payload.get("input_sensitive"):
            raise ReviewRejectedError(
                f"样本音频命中敏感词（type={payload.get('input_sensitive_type')}）",
                vendor=self.name,
                raw=payload,
            )
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=vid,
            task_id=vid,
            model=body["model"],
            message="复刻成功（云知声为同步复刻）；注意音色费在首次合成时才扣",
            raw=dict(payload),
        )

    def upload_sample(self, sample: SampleInput, *, purpose: str) -> int | str:
        """上传样本，返回 file_id（官方 SDK 里它可能是字符串，这里原样保留类型）。"""
        filename, mime, raw = self.sample_bytes(sample)
        payload = self.send_json(
            "POST",
            "/files/upload",
            data={"purpose": purpose},
            files={"file": (filename, raw, mime)},
        )
        info = payload.get("file")
        if not isinstance(info, Mapping) or info.get("file_id") in (None, ""):
            raise ProviderError("上传响应缺少 file.file_id", vendor=self.name, raw=payload)
        file_id = info["file_id"]
        if isinstance(file_id, (int, str)):
            return file_id
        return str(file_id)

    def clone_status(self, task_id: str) -> CloneTask:
        """同步型厂商：无轮询阶段，原样回显终态。"""
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=task_id,
            task_id=task_id,
            message="云知声为同步复刻，无轮询阶段",
        )

    def delete_voice(self, voice_id: str, *, voice_type: str = "voice_cloning") -> None:
        self.send_json(
            "POST", "/audio/voices/delete", body={"voice_id": voice_id, "voice_type": voice_type}
        )


    def create_speech_task(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.send_json("POST", "/audio/speech/tasks", body=dict(body))

    def speech_task(self, task_id: str) -> dict[str, Any]:
        """查询异步合成任务（返回体里的 status/file_id/download_url 决定下一步）。"""
        return dict(self.send_json("GET", "/audio/speech/tasks", params={"task_id": task_id}))

    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        **settings: Any,
    ) -> AudioResult:
        chosen = model or self.default_model or DEFAULT_SYNTH_MODEL
        ref = VoiceRef.of(voice, vendor=self.name, model=chosen)
        limit = CHAR_LIMIT_BY_MODEL.get(chosen, DEFAULT_CHAR_LIMIT)
        if len(text) > limit:
            raise ProviderError(
                f"文本 {len(text)} 字符超过 {chosen} 的异步合成上限 {limit}", vendor=self.name
            )
        if stream:
            raise ProviderError(
                "云知声没有 REST 同步合成接口：短文本流式只有 WebSocket（本 adapter 未实现）；"
                "请去掉 stream=True，改走异步任务（内部会自动轮询到完成再返回）",
                vendor=self.name,
            )
        voice_setting: dict[str, Any] = {"voice_id": ref.raw, "language": "zh"}
        voice_setting.update(settings.pop("voice_setting", None) or {})
        audio_setting: dict[str, Any] = {"audio_sample_rate": 32000, "format": "mp3"}
        audio_setting.update(settings.pop("audio_setting", None) or {})
        body: dict[str, Any] = {
            "model": chosen,
            "text": text,
            "voice_setting": voice_setting,
            "audio_setting": audio_setting,
        }
        body.update({k: v for k, v in settings.items() if v is not None})

        started = now_ms()
        created = self.create_speech_task(body)
        task_id = created.get("task_id")
        if not task_id:
            raise ProviderError("异步合成响应缺少 task_id", vendor=self.name, raw=created)

        final = self.poller.wait_until(
            lambda: self.speech_task(str(task_id)),
            is_done=lambda state: state.get("status") == TASK_DONE and bool(state.get("file_id")),
            is_failed=lambda state: state.get("status") == TASK_FAILED,
            describe=lambda state: f"状态 {state.get('status')}",
            vendor=self.name,
            task_id=str(task_id),
        )
        if final.get("status") == TASK_FAILED:
            raise ProviderError(
                f"异步合成任务失败：{final.get('base_resp', {}).get('status_msg') or final.get('status')}",
                vendor=self.name,
                raw=final,
            )
        audio = self.fetch_audio(final)
        return AudioResult(
            vendor=self.name,
            model=chosen,
            voice_id=ref.raw,
            audio=audio,
            format=str(audio_setting.get("format") or "mp3"),
            sample_rate=int(audio_setting.get("audio_sample_rate") or 32000),
            chars=int(created.get("usage_characters") or len(text)),
            latency_ms=int(now_ms() - started),
        )

    def fetch_audio(self, task_state: Mapping[str, Any]) -> bytes:
        """取任务音频：优先官方 SDK 用的 ``download_url``，回退到内容接口。

        ``purpose`` 在文档（``t2a_async``）与官方 SDK（``t2a_async_output``）之间不一致，
        两个都试一遍，避免因一个取值把整条链路卡死。
        """
        direct = task_state.get("download_url") or task_state.get("url")
        if direct:
            return self.http.download(str(direct))
        file_id = task_state.get("file_id")
        if file_id in (None, ""):
            raise ProviderError("任务已完成但响应里既无 download_url 也无 file_id", vendor=self.name)
        last: Exception | None = None
        for purpose in OUTPUT_PURPOSES:
            try:
                response = self.send(
                    "GET",
                    "/files/retrieve_content",
                    params={"file_id": file_id, "purpose": purpose},
                )
                return response.content
            except ProviderError as exc:  # purpose 取值不对就换下一个
                last = exc
        raise ProviderError(
            f"取音频失败（已试 purpose={list(OUTPUT_PURPOSES)}）：{last}", vendor=self.name
        )
