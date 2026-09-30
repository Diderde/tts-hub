# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""火山引擎适配器。静态头鉴权；复刻无 task_id，用 get_voice 轮询；合成支持纯
HTTP SSE 流式。后付费音色首次合成即扣槽位费，默认拦截、需显式确认后放行。"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

from ..core.errors import ProviderError, QuotaError, ReviewRejectedError, TTSHubError
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

__all__ = [
    "GET_VOICE_PATH",
    "HOST",
    "RESOURCE_IDS",
    "STATUS_MAP",
    "SYNTH_SSE_PATH",
    "VOICE_CLONE_PATH",
    "NyamuYutenji",
]

HOST = "openspeech.bytedance.com"
SYNTH_SSE_PATH = "/api/v3/tts/unidirectional/sse"
SYNTH_CHUNKED_PATH = "/api/v3/tts/unidirectional"
VOICE_CLONE_PATH = "/api/v3/tts/voice_clone"
GET_VOICE_PATH = "/api/v3/tts/get_voice"

RESOURCE_IDS = {
    "seed-tts-2.0": "豆包语音合成 2.0（字符版）",
    "seed-tts-1.0": "语音合成 1.0（字符版）",
    "seed-tts-1.0-concurr": "语音合成 1.0（并发版）",
    "seed-icl-2.0": "声音复刻 2.0（字符版）",
    "seed-icl-1.0": "声音复刻 1.0（字符版）",
    "seed-icl-1.0-concurr": "声音复刻 1.0（并发版）",
}
DEFAULT_RESOURCE_ID = "seed-tts-2.0"
CLONE_RESOURCE_ID = "seed-icl-2.0"

CODE_OK = 20000000
STATUS_MAP = {
    0: STATUS_FAILED,  # NotFound
    1: STATUS_TRAINING,  # Training
    2: STATUS_READY,  # Success
    3: STATUS_FAILED,  # Failed
    4: STATUS_READY,  # Active
}
POSTPAID_FIRST_CHARGE = 138.0
CLONE_TTL_HOURS = 24 * 7
MAX_SAMPLE_BYTES = 10 * 1024 * 1024

_CODE_KINDS: dict[str, type[TTSHubError]] = {
    "45000001": ProviderError,  # Invalid argument
    "45000000": QuotaError,  # 并发超限（quota exceeded for types: concurrency）
    "40402003": ProviderError,  # 文本超限
    "40402002": ProviderError,
    "45002000": ProviderError,
    "45002001": ProviderError,
    "45001101": ProviderError,  # 音频上传失败
    "45001102": ProviderError,  # ASR 转写失败
    "45001104": ReviewRejectedError,  # 声纹检测未通过
    "45001109": ReviewRejectedError,  # WER 检测错误（音频与文本差异过大）
    "45001114": ProviderError,  # 音频质量较差
    "45001122": ProviderError,  # ASR 未检测到人声
    "45001123": QuotaError,  # 达到上传次数上限
    "45001124": ReviewRejectedError,  # ASR 文本审核拒绝
    "45001125": ReviewRejectedError,  # demo 文本审核拒绝
    "45001127": ReviewRejectedError,  # prompt 音频审核拒绝
    "45001128": ReviewRejectedError,
    "55001310": ReviewRejectedError,  # 安审未过
    "55001311": ReviewRejectedError,  # 声纹未过
}


class NyamuYutenji(KasumiToyama):
    """火山引擎适配器（name = ``volcengine``）。

    ``api_key`` 即控制台的 ``X-Api-Key``。厂商专属配置：

    - ``resource_id``：合成资源位（缺省 ``seed-tts-2.0``）
    - ``uid``：``req_params.user.uid``，缺省取 ``"tts-hub"``
    - ``billing``：``prepaid``（缺省）或 ``postpaid``
    - ``ack_first_charge``：postpaid 下设为 true 表示已知悉首次合成会扣费
    """

    name = "volcengine"
    base_url = f"https://{HOST}"
    default_model = DEFAULT_RESOURCE_ID
    clone_model = CLONE_RESOURCE_ID
    clone_ttl_hours = CLONE_TTL_HOURS


    @property
    def resource_id(self) -> str:
        return str(self.setting("resource_id", DEFAULT_RESOURCE_ID))

    @property
    def billing(self) -> str:
        return str(self.setting("billing", "prepaid")).lower()

    def headers(self, *, json_body: bool = True) -> dict[str, str]:
        """覆盖基类：火山用一整套静态头，不是 Bearer（因此不需要 ``auth_headers``）。"""
        head = {
            "X-Api-Key": self.api_key,
            "X-Api-Request-Id": uuid.uuid4().hex,
        }
        if json_body:
            head["Content-Type"] = "application/json"
        return head

    def synth_headers(self) -> dict[str, str]:
        """合成要比复刻多一个 ``X-Api-Resource-Id``；复刻发它会出问题。"""
        head = self.headers()
        head["X-Api-Resource-Id"] = self.resource_id
        return head


    def business_error(self, payload: Mapping[str, Any]) -> tuple[str | None, str] | None:
        """火山的 ``code`` 在**成功时也存在**（成功是 0 或 20000000），所以只在非成功时算错。"""
        code = payload.get("code")
        if code in (None, 0, CODE_OK):
            return None
        return str(code), str(payload.get("message") or "")

    def classify(self, status: int, payload: Any, *, raw: Any = None) -> Any:
        code, message = self._code_of(payload)
        kind = _CODE_KINDS.get(code or "")
        if kind is not None:
            raise kind(
                message or f"HTTP {status}", vendor=self.name, code=code, status=status, raw=raw
            )
        return super().classify(status, payload, raw=raw)

    @staticmethod
    def _code_of(payload: Any) -> tuple[str | None, str]:
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                return None, ""
        if isinstance(payload, Mapping):
            code = payload.get("code")
            if code is not None:
                return str(code), str(payload.get("message") or "")
        return None, ""


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=name,
                display_name=label,
                supports_clone=name.startswith("seed-icl"),
                supports_stream=True,  # SSE，纯 HTTP 就能流式
                char_limit=None,  # 官方未给 v3 单次字符硬上限
                note=f"X-Api-Resource-Id={name}",
            )
            for name, label in RESOURCE_IDS.items()
        ]

    def list_voices(self) -> list[VoiceInfo]:
        """火山没有"列全部音色"的 v3 接口，只能按 ID 逐个查——这里不做无根据的猜测。"""
        return []


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        speaker_id: str | None = None,
        custom_speaker_id: str | None = None,
        audio_format: str = "wav",
        language: int | None = None,
        **options: Any,
    ) -> CloneTask:
        del model
        raw = sample.read_bytes()
        if len(raw) > MAX_SAMPLE_BYTES:
            raise ProviderError(
                f"训练音频 {len(raw)} 字节超过火山 10MB 上限", vendor=self.name
            )
        body: dict[str, Any] = {"audio": {"data": base64.b64encode(raw).decode("ascii"), "format": audio_format}}
        if self.billing == "postpaid":
            body["speaker_id"] = "custom_speaker_id"
            if not custom_speaker_id:
                raise ProviderError(
                    "后付费模式下必须提供 custom_speaker_id（8~256 字符、首字符为字母）",
                    vendor=self.name,
                )
            body["custom_speaker_id"] = custom_speaker_id
        else:
            if not speaker_id:
                raise ProviderError(
                    "预付费模式必须提供 speaker_id（形如 S_xxxx，从控制台获取）", vendor=self.name
                )
            body["speaker_id"] = speaker_id
        text = transcript if transcript is not None else sample.transcript
        if text:
            body["text"] = text  # 参考文本；与音频差异过大会被 WER 检测拒（45001109）
        if language is not None:
            body["language"] = language
        demo = preview_text if preview_text is not None else text
        extra = {k: v for k, v in options.items() if v is not None}
        if demo:
            extra.setdefault("demo_text", demo)
        if extra:
            body["extra_params"] = extra

        payload = self.send_json("POST", VOICE_CLONE_PATH, body=body)
        status_code = payload.get("status")
        status = STATUS_MAP.get(status_code if isinstance(status_code, int) else -1, STATUS_TRAINING)
        if status_code in (2, 4):
            status = STATUS_READY
        voice_id = str(payload.get("speaker_id") or custom_speaker_id or speaker_id or "")
        return CloneTask(
            vendor=self.name,
            status=status,
            voice_id=voice_id if status == STATUS_READY else None,
            task_id=voice_id,
            model=CLONE_RESOURCE_ID,
            message=(
                f"复刻已提交（status={status_code}，Training=1 属正常）。"
                f"⚠️ 首次调用 v3 合成接口即扣音色槽位费 {POSTPAID_FIRST_CHARGE:.0f} 元并转正，"
                f"转正后该 ID 不可再训练；若 {CLONE_TTL_HOURS // 24} 天内不做合成，音色会被自动删除。"
            ),
            raw=dict(payload),
        )

    def clone_status(self, task_id: str, *, speaker_id: str | None = None) -> CloneTask:
        """用 ``get_voice`` 轮询；它没有 task_id，**请求体与训练同构**。"""
        body: dict[str, Any] = {"speaker_id": speaker_id or task_id}
        if self.billing == "postpaid":
            body["custom_speaker_id"] = task_id
        payload = self.send_json("POST", GET_VOICE_PATH, body=body)
        raw_status = payload.get("status")
        status = STATUS_MAP.get(raw_status if isinstance(raw_status, int) else -1, STATUS_TRAINING)
        return CloneTask(
            vendor=self.name,
            status=status,
            voice_id=task_id if status == STATUS_READY else None,
            task_id=task_id,
            model=CLONE_RESOURCE_ID,
            message=(
                f"status={raw_status}（0未找到/1训练中/2成功/3失败/4已激活；2 与 4 均可合成）"
            ),
            raw=dict(payload),
        )


    def check_charge_ack(self, *, ack: bool = False) -> None:
        """后付费音色首次合成会扣真金白银，**不确认就不放行**。

        这是本 adapter 唯一一处"故意挡路"的设计：138 元且不可逆，
        与其扣完再解释，不如在动手前把话说清楚。
        """
        if self.billing != "postpaid":
            return
        if ack or self.setting("ack_first_charge"):
            return
        raise ProviderError(
            f"后付费模式下首次调用合成接口会立即扣音色槽位费约 {POSTPAID_FIRST_CHARGE:.0f} 元，"
            "且该音色转正后不可再训练（复刻了没合成的音色会在 7 天后被自动删除）。"
            "确认要继续请传 ack_charge=True，或在 providers.yaml 里设 ack_first_charge: true",
            vendor=self.name,
        )

    def build_request(self, text: str, voice: VoiceRef | str, **options: Any) -> dict[str, Any]:
        ref = VoiceRef.of(voice, vendor=self.name, model=self.resource_id)
        speaker = ref.raw
        if speaker.startswith("custom_speaker_id:"):
            speaker = speaker.split(":", 1)[1]
        params: dict[str, Any] = {
            "text": text,
            "speaker": speaker,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
        }
        audio_params = options.pop("audio_params", None)
        if audio_params:
            params["audio_params"].update(audio_params)
        additions = options.pop("additions", None)
        if additions:
            params["additions"] = json.dumps(additions, ensure_ascii=False)
        params.update({k: v for k, v in options.items() if v is not None})
        return {"user": {"uid": str(self.setting("uid", "tts-hub"))}, "req_params": params}

    def sse_chunks(self, body: Mapping[str, Any]) -> Iterator[bytes]:
        """SSE 流：每块的 ``data`` 是 base64 音频，``code == 20000000`` 结束。"""
        head = self.synth_headers()
        head["Accept"] = "text/event-stream"
        lines = self.http.stream_bytes(
            "POST",
            self.url(SYNTH_SSE_PATH),
            headers=head,
            content=self.encode_body(dict(body)),
        )
        for line in akai_haato(lines):
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
            code = event.get("code")
            if code not in (None, 0, CODE_OK):
                self.classify(200, event, raw=event)
            piece = event.get("data")
            if piece:
                yield base64.b64decode(piece)
            if code == CODE_OK:
                return

    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        ack_charge: bool = False,
        **options: Any,
    ) -> AudioResult:
        self.check_charge_ack(ack=ack_charge)
        chosen = model or self.resource_id
        ref = VoiceRef.of(voice, vendor=self.name, model=chosen)
        body = self.build_request(text, ref, **options)
        started = now_ms()
        if stream:
            return AudioResult(
                vendor=self.name,
                model=chosen,
                voice_id=ref.raw,
                format="mp3",
                sample_rate=24000,
                chars=len(text),
                stream=True,
                chunks=self.sse_chunks(body),
            )
        audio = b"".join(self.sse_chunks(body))
        return AudioResult(
            vendor=self.name,
            model=chosen,
            voice_id=ref.raw,
            audio=audio,
            format="mp3",
            sample_rate=24000,
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )
