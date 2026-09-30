# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""腾讯云适配器。TC3-HMAC-SHA256 请求签名；复刻 VRS 异步任务，成功音色为
FastVoiceType（合成时需同时传 VoiceType=200000000）；流式走 FlowTTS SSE（独立域名）。"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from ..core.net import akai_haato
from ..core.signing import amane_kanata
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

__all__ = ["FAST_VOICE_TYPE", "TTS_HOST", "VRS_HOST", "SakikoTogawa"]

VRS_HOST = "vrs.tencentcloudapi.com"
TTS_HOST = "tts.tencentcloudapi.com"
FLOWTTS_SSE_HOST = "trtc.ai.tencentcloudapi.com"
VRS_VERSION = "2020-08-24"
TTS_VERSION = "2019-08-23"
FLOWTTS_VERSION = "2019-07-22"
VRS_REGION = "ap-guangzhou"
FLOWTTS_REGION = "ap-guangzhou"
FLOWTTS_CHAR_LIMIT = 20000
FAST_VOICE_TYPE = 200000000
FAST_TASK_TYPE = 5
CJK_CHAR_LIMIT = 150
LATIN_CHAR_LIMIT = 500
CLONE_TTL_HOURS = 24 * 90
_DEFAULT_CODEC = "mp3"
_DEFAULT_SAMPLE_RATE = 24000
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f\u3040-\u30ff\uff00-\uffef]")

_STATUS_MAP = {
    0: STATUS_TRAINING,  # waiting
    1: STATUS_TRAINING,  # doing
    2: STATUS_READY,
    3: STATUS_FAILED,
}

_CODE_KINDS: dict[str, type[TTSHubError]] = {
    "AuthFailure": AuthError,
    "AuthFailure.InvalidAuthorization": AuthError,
    "AuthFailure.InvalidSecretId": AuthError,
    "AuthFailure.SecretIdNotFound": AuthError,
    "AuthFailure.SignatureExpire": AuthError,
    "AuthFailure.SignatureFailure": AuthError,
    "AuthFailure.UnauthorizedOperation": AuthError,
    "LimitExceeded": QuotaError,
    "LimitExceeded.AccessLimit": QuotaError,
    "LimitExceeded.ConcurrencyLimit": QuotaError,
    "LimitExceeded.VoiceCloneMaxNumLimit": QuotaError,
    "RequestLimitExceeded": QuotaError,
    "UnsupportedOperation.VRSQuotaExhausted": QuotaError,
    "UnsupportedOperation.PkgExhausted": QuotaError,
    "UnsupportedOperation.NoFreeAccount": QuotaError,
    "FailedOperation.VoiceNotQualified": ReviewRejectedError,
    "FailedOperation.VoiceEvaluateFailed": ReviewRejectedError,
    "InvalidParameterValue.VoiceName": ProviderError,
    "UnsupportedOperation.TextTooLong": ProviderError,
}


class SakikoTogawa(KasumiToyama):
    """腾讯云适配器（name = ``tencent``）。

    ``SecretId`` 走 ``api_key``、``SecretKey`` 走 ``api_secret``（因此 ``needs_secret`` 为真）。
    """

    name = "tencent"
    base_url = f"https://{VRS_HOST}"
    needs_secret = True
    default_model = "tts-texttovoice"
    clone_model = "vrs-onesentence"
    clone_ttl_hours = CLONE_TTL_HOURS


    def prepare_call(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        host: str = VRS_HOST,
        service: str = "vrs",
        version: str = VRS_VERSION,
        region: str = VRS_REGION,
    ) -> tuple[str, dict[str, str], bytes]:
        """把一次云 API 调用准备成 ``(url, headers, body)``。

        拆出来是为了让 SSE 那条路能复用同一套签名——SSE 的响应不是 JSON，
        不能走 :meth:`call`，但请求侧的签名逻辑完全一样。
        """
        body = self.encode_body(dict(payload)) or b"{}"
        headers = amane_kanata(
            secret_id=self.api_key,
            secret_key=self.api_secret or "",
            service=service,
            action=action,
            version=version,
            region=region,
            host=host,
            payload=body,
        )
        return f"https://{host}/", headers, body

    def call(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        host: str = VRS_HOST,
        service: str = "vrs",
        version: str = VRS_VERSION,
        region: str = VRS_REGION,
    ) -> Mapping[str, Any]:
        """按 TC3 签名发一次云 API 调用，返回 ``Response`` 对象。

        刻意不复用基类的 ``send()``：签名的 host/service/action 是**每次调用各不相同**的
        上下文，塞进实例属性会在多线程（FastAPI 线程池）下串号；这里全走局部变量。
        """
        url, headers, body = self.prepare_call(
            action, payload, host=host, service=service, version=version, region=region
        )
        response = self.http.raw_request("POST", url, headers=headers, content=body)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ProviderError(
                "腾讯云返回的不是合法 JSON", vendor=self.name, status=response.status_code
            ) from exc
        if not isinstance(parsed, Mapping) or "Response" not in parsed:
            raise ProviderError(
                "腾讯云响应缺少 Response 包装", vendor=self.name, raw=parsed
            )
        envelope = parsed["Response"]
        if not isinstance(envelope, Mapping):
            raise ProviderError("腾讯云 Response 不是对象", vendor=self.name, raw=envelope)
        error = envelope.get("Error")
        if error:
            self.classify_error(str(error.get("Code") or ""), str(error.get("Message") or ""), envelope)
        return envelope

    def classify_error(self, code: str, message: str, raw: Any = None) -> None:
        """把 ``Response.Error.Code`` 映射成四类归一错误并抛出。"""
        kind = _CODE_KINDS.get(code)
        if kind is None:  # 前缀兜底：AuthFailure.X / LimitExceeded.X …
            head = code.split(".", 1)[0]
            kind = _CODE_KINDS.get(head, ProviderError)
        raise kind(message or code, vendor=self.name, code=code, raw=raw)


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="tts-texttovoice",
                display_name="腾讯云语音合成 TextToVoice",
                supports_clone=True,
                supports_stream=False,
                char_limit=CJK_CHAR_LIMIT,
                note="非流式一次返回整段 base64 音频；中文 150 字 / 英文 500 字母",
            )
        ]

    def list_voices(self) -> list[VoiceInfo]:
        """复刻音色列表。一句话版（TaskType=5）与基础版（TaskType=0）分两次拉再合并。"""
        out: list[VoiceInfo] = []
        for task_type, kind_note in ((0, "基础版"), (FAST_TASK_TYPE, "一句话版")):
            envelope = self.call("GetVRSVoiceTypes", {"TaskType": task_type})
            for row in ((envelope.get("Data") or {}).get("VoiceTypeList")) or []:
                fast = row.get("FastVoiceType")
                out.append(
                    VoiceInfo(
                        voice_id=str(fast or row.get("VoiceType") or ""),
                        display_name=str(row.get("VoiceName") or ""),
                        kind="cloned",
                        created_at=row.get("DateCreated"),
                        note=kind_note,
                    )
                )
        return [v for v in out if v.voice_id]


    def training_text(self, *, task_type: int = FAST_TASK_TYPE) -> tuple[str, str]:
        """取训练文本，返回 ``(TextId, 要念的文本)``。

        官方注明：一句话版的 TextId **7 天有效，且成功创建一次复刻任务后即失效**。
        """
        envelope = self.call("GetTrainingText", {"TaskType": task_type})
        rows = ((envelope.get("Data") or {}).get("TrainingTextList")) or []
        if not rows:
            raise ProviderError("获取训练文本失败：返回列表为空", vendor=self.name, raw=envelope)
        return str(rows[0].get("TextId") or ""), str(rows[0].get("Text") or "")

    def detect_quality(self, sample: SampleInput, *, text_id: str, type_id: int = 2) -> str:
        """提交录音做音质检测（TypeId=2），返回 AudioId。

        这是腾讯云唯一接受音频的入口——**没有独立的上传接口**。
        """
        raw = sample.read_bytes()
        envelope = self.call(
            "DetectEnvAndSoundQuality",
            {
                "TextId": text_id,
                "AudioData": base64.b64encode(raw).decode("ascii"),
                "TypeId": type_id,
                "TaskType": FAST_TASK_TYPE,
            },
        )
        data = envelope.get("Data") or {}
        code = data.get("DetectionCode")
        if code not in (0, None):
            raise ReviewRejectedError(
                f"录音未通过检测（DetectionCode={code}）：{data.get('DetectionMsg') or ''}",
                vendor=self.name,
                code=str(code),
                raw=dict(data),
            )
        audio_id = data.get("AudioId")
        if not audio_id:
            raise ProviderError(
                "音质检测没有返回 AudioId（TypeId 是否为 2？环境检测不返回该字段）",
                vendor=self.name,
                raw=dict(data),
            )
        return str(audio_id)

    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        text_id: str | None = None,
        gender: int = 2,
        language: int = 1,
        **options: Any,
    ) -> CloneTask:
        del model, transcript, preview_text
        if not name:
            raise ProviderError("腾讯云复刻要求提供音色名（VoiceName）", vendor=self.name)
        if not text_id:
            fetched_id, text = self.training_text()
            raise ProviderError(
                f"一句话复刻必须照官方文本录制后才能提交，本 adapter 不替你猜。\n"
                f"请朗读下面这段（TextId={fetched_id}，7 天内有效），"
                f"录好后用 text_id={fetched_id} 重新调用：\n  「{text}」",
                vendor=self.name,
            )
        audio_id = self.detect_quality(sample, text_id=text_id)
        envelope = self.call(
            "CreateVRSTask",
            {
                "SessionId": uuid.uuid4().hex,
                "VoiceName": name,
                "VoiceGender": gender,
                "VoiceLanguage": language,
                "AudioIdList": [audio_id],
                "TaskType": FAST_TASK_TYPE,
                **{k: v for k, v in options.items() if v is not None},
            },
        )
        task_id = (envelope.get("Data") or {}).get("TaskId")
        if not task_id:
            raise ProviderError("创建复刻任务响应缺少 Data.TaskId", vendor=self.name, raw=envelope)
        return CloneTask(
            vendor=self.name,
            status=STATUS_TRAINING,
            task_id=str(task_id),
            model=self.clone_model,
            message="复刻任务已提交（异步训练），用 clone_status 轮询到 Status=2",
            raw=dict(envelope),
        )

    def clone_status(self, task_id: str) -> CloneTask:
        """轮询复刻任务；成功后音色 ID 是 ``FastVoiceType``。"""
        envelope = self.call("DescribeVRSTaskStatus", {"TaskId": task_id})
        data = envelope.get("Data") or {}
        status_code = data.get("Status")
        status = _STATUS_MAP.get(status_code if isinstance(status_code, int) else -1, STATUS_TRAINING)
        fast = data.get("FastVoiceType")
        voice_id = str(fast) if (status == STATUS_READY and fast) else None
        if status == STATUS_READY and not fast:
            voice_type = data.get("VoiceType")
            voice_id = str(voice_type) if voice_type else None
        return CloneTask(
            vendor=self.name,
            status=status,
            voice_id=voice_id,
            task_id=task_id,
            model=self.clone_model,
            message=str(data.get("ErrorMsg") or data.get("StatusStr") or f"Status={status_code}"),
            raw=dict(data),
        )


    def sse_chunks(
        self,
        text: str,
        voice: VoiceRef | str,
        *,
        sdk_app_id: int | str,
        model: str = "flow_02_turbo",
        region: str = FLOWTTS_REGION,
        audio_format: Mapping[str, Any] | None = None,
        language: str | None = None,
    ) -> Iterator[bytes]:
        """FlowTTS 的 SSE 流式合成。

        两处硬约束：① 域名**必须是** ``trtc.ai.tencentcloudapi.com``（官方逐字：
        "否则会调用接口失败"）；② `SdkAppId` 必填且 `Region` 必填。
        响应是 ``data: {"Type":"chunk","Audio":"<base64>","IsEnd":bool}``；
        出错则是 ``{"Type":"error","Error":{...}}``。
        """
        ref = VoiceRef.of(voice, vendor=self.name, model=model)
        payload: dict[str, Any] = {
            "Text": text,
            "Voice": {"VoiceId": ref.raw},
            "SdkAppId": int(sdk_app_id),
            "Model": model,
        }
        if audio_format:
            payload["AudioFormat"] = dict(audio_format)
        if language:
            payload["Language"] = language
        url, headers, body = self.prepare_call(
            "TextToSpeechSSE",
            payload,
            host=FLOWTTS_SSE_HOST,
            service="trtc",
            version=FLOWTTS_VERSION,
            region=region,
        )
        headers["Accept"] = "text/event-stream"
        for line in akai_haato(self.http.stream_bytes("POST", url, headers=headers, content=body)):
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, Mapping):
                continue
            if event.get("Type") == "error":
                error = event.get("Error") or {}
                self.classify_error(
                    str(error.get("Code") or ""), str(error.get("Message") or ""), event
                )
            piece = event.get("Audio")
            if piece:
                yield base64.b64decode(piece)
            if event.get("IsEnd"):
                return

    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        codec: str = _DEFAULT_CODEC,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        **options: Any,
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model or self.default_model)
        if stream:
            sdk_app_id = self.setting("sdk_app_id")
            if not sdk_app_id:
                raise ProviderError(
                    "腾讯云的流式合成走 FlowTTS 的 SSE，需要 TRTC 的 SdkAppId："
                    "请在 providers.yaml 的 tencent 下配置 sdk_app_id，"
                    "或去掉 stream=True 用非流式 TextToVoice",
                    vendor=self.name,
                )
            if len(text) > FLOWTTS_CHAR_LIMIT:
                raise ProviderError(
                    f"文本 {len(text)} 字符超过 FlowTTS SSE 上限 {FLOWTTS_CHAR_LIMIT}",
                    vendor=self.name,
                )
            return AudioResult(
                vendor=self.name,
                model=model or "flow_02_turbo",
                voice_id=ref.raw,
                format="pcm",
                sample_rate=24000,
                chars=len(text),
                stream=True,
                chunks=self.sse_chunks(
                    text,
                    voice,
                    sdk_app_id=sdk_app_id,
                    model=model or "flow_02_turbo",
                    region=str(self.setting("flowtts_region", FLOWTTS_REGION)),
                ),
            )
        limit = CJK_CHAR_LIMIT if _CJK_RE.search(text) else LATIN_CHAR_LIMIT
        if len(text) > limit:
            raise ProviderError(
                f"文本 {len(text)} 字符超过 TextToVoice 上限（含中文时 {CJK_CHAR_LIMIT}、"
                f"纯英文 {LATIN_CHAR_LIMIT}）",
                vendor=self.name,
            )
        body: dict[str, Any] = {
            "Text": text,
            "SessionId": uuid.uuid4().hex,
            "Codec": codec,
            "SampleRate": sample_rate,
        }
        if ref.raw.startswith("WCHN-"):
            body["VoiceType"] = FAST_VOICE_TYPE
            body["FastVoiceType"] = ref.raw
        else:
            body["VoiceType"] = self._as_int(ref.raw)
        body.update({k: v for k, v in options.items() if v is not None})

        started = now_ms()
        envelope = self.call("TextToVoice", body, host=TTS_HOST, service="tts", version=TTS_VERSION)
        encoded = envelope.get("Audio") or (envelope.get("Data") or {}).get("Audio")
        if not encoded:
            raise ProviderError("合成响应里没有 Audio 字段", vendor=self.name, raw=envelope)
        return AudioResult(
            vendor=self.name,
            model=model or self.default_model or "",
            voice_id=ref.raw,
            audio=base64.b64decode(encoded),
            format=codec,
            sample_rate=sample_rate,
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )

    @staticmethod
    def _as_int(value: Any) -> Any:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return value
