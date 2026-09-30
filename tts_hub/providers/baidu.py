# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""百度智能云适配器。API Key + Secret Key 换 access_token（放 query，注意脱敏）；
复刻为同步创建；合成返回二进制，成败看 Content-Type 是否为 audio/*。"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, ClassVar

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
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

__all__ = ["TOKEN_PATH", "VOICE_PATH", "MisumiUika"]

VOICE_PATH = "/rest/2.0/speech/publiccloudspeech/v1/voice/clone"
TOKEN_PATH = "/oauth/2.0/token"

MODEL_PLACEHOLDER = "baidu-voice-clone"
CHAR_LIMIT = 500
CLONE_TTL_HOURS = 24 * 365


class MisumiUika(KasumiToyama):
    """百度智能云适配器（name = ``baidu``）。"""

    name = "baidu"
    base_url = "https://aip.baidubce.com"
    needs_secret = True
    default_model = MODEL_PLACEHOLDER
    clone_model = MODEL_PLACEHOLDER
    clone_ttl_hours = CLONE_TTL_HOURS

    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {
        "110": AuthError,
        "111": AuthError,
        "217": AuthError,
        "6": AuthError,
        "11007": AuthError,
        "11014": AuthError,
        "15": QuotaError,
        "17": QuotaError,
        "11002": QuotaError,
        "11003": QuotaError,
        "10014": QuotaError,
        "10015": QuotaError,
        "216604": QuotaError,
        "10021": ReviewRejectedError,
        "10027": ReviewRejectedError,
        "11015": ReviewRejectedError,
        "12002": ReviewRejectedError,
        "12004": ReviewRejectedError,
        "10020": ProviderError,
        "10022": ProviderError,
        "10023": ProviderError,
        "10025": ProviderError,
        "10026": ProviderError,
        "11000": ProviderError,
        "11004": ProviderError,
        "11006": ProviderError,
        "11008": ProviderError,
        "11009": ProviderError,
        "11010": ProviderError,
        "11011": ProviderError,
        "11012": ProviderError,
        "11013": ProviderError,
        "12000": ProviderError,
        "12001": ProviderError,
        "12003": ProviderError,
        "12005": ProviderError,
        "12006": ProviderError,
        "12007": ProviderError,
    }

    auth_mode: str = "token"


    def auth_headers(self) -> dict[str, str]:
        """百度默认把 token 放 query，不放头；只有 ``auth_mode=api_key`` 才用头。"""
        if self.auth_mode == "api_key":
            return {"Authorization": self.api_key}
        return {}

    def fetch_token(self) -> tuple[str, float]:
        """换取 access_token；失败时抛归一化的 ``AuthError``。"""
        payload = self.http.json_reply(
            "POST",
            self.url(TOKEN_PATH),
            params={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.api_secret,
            },
            expect=(200,),
        )
        token = payload.get("access_token")
        if not token:
            reason = payload.get("error_description") or payload.get("error") or "响应缺少 access_token"
            raise AuthError(
                f"换取 access_token 失败：{reason}",
                vendor=self.name,
                code=str(payload.get("error") or ""),
                raw=payload,
            )
        return str(token), float(payload.get("expires_in") or 0)

    def send(self, method: str, path: str, **kw: Any) -> Any:
        """把 access_token 附到 query；遇 110/111（token 失效/过期）重取一次再试。

        "重取一次"是官方建议的处理方式，重试上限刻意设为 1：再失败就是密钥本身的问题，
        无限重试只会把配额烧光。
        """
        if self.auth_mode == "api_key":
            return super().send(method, path, **kw)
        for attempt in (1, 2):
            params = dict(kw.pop("params", None) or {})
            params["access_token"] = self.access_token()
            try:
                return super().send(method, path, params=params, **kw)
            except AuthError as exc:
                if attempt == 1 and exc.code in ("110", "111"):
                    self._token = None
                    self._token_deadline = 0.0
                    continue
                raise
        raise ProviderError("access_token 重取后仍然失败", vendor=self.name)  # pragma: no cover


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=MODEL_PLACEHOLDER,
                display_name="百度大模型声音复刻",
                supports_clone=True,
                supports_stream=False,  # 流式仅 WebSocket，纯 HTTP 不可用
                char_limit=CHAR_LIMIT,
                note="官方未公开模型 ID，音色能力由 voice_id 承载；流式仅支持 WSS",
            )
        ]

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("POST", f"{VOICE_PATH}/list", body={"page": 1})
        items = ((payload.get("data") or {}).get("items")) or []
        return [
            VoiceInfo(
                voice_id=str(row.get("voice_id", "")),
                display_name=str(row.get("voice_name") or row.get("voice_id")),
                kind="cloned",
                created_at=str(row.get("create_time")) if row.get("create_time") else None,
                note=str(row.get("voice_desc") or ""),
            )
            for row in items
            if row.get("voice_id") is not None
        ]


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        lang: str = "zh",
        text_id: str | None = None,
    ) -> CloneTask:
        del model, preview_text  # 百度复刻无模型选择，也没有"试听文本"这一步
        if not name:
            raise ProviderError("百度复刻要求提供音色名（voice_name，同一账号下不可重复）", vendor=self.name)
        body: dict[str, Any] = {"voice_name": name, "lang": lang}
        if sample.url:
            body["audio_url"] = sample.url
        else:
            body["audio_file"] = base64.b64encode(sample.read_bytes()).decode("ascii")
        if text_id:
            body["text_id"] = text_id
        elif transcript:
            body["voice_desc"] = transcript[:200]

        payload = self.send_json("POST", f"{VOICE_PATH}/create", body=body)
        voice_id = (payload.get("data") or {}).get("voice_id")
        if voice_id is None:
            raise ProviderError("创建音色响应缺少 data.voice_id", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(voice_id),
            task_id=str(voice_id),
            model=MODEL_PLACEHOLDER,
            message="复刻成功（百度为同步创建，无训练轮询阶段）",
            raw=dict(payload),
        )

    def clone_status(self, task_id: str) -> CloneTask:
        """百度没有任务态；这里查一次音色详情，确认它还在。"""
        payload = self.send_json("POST", f"{VOICE_PATH}/detail", body={"voice_id": _as_int(task_id)})
        data = payload.get("data") or {}
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(data.get("voice_id") or task_id),
            task_id=task_id,
            model=MODEL_PLACEHOLDER,
            message=f"音色存在（{data.get('voice_name') or task_id}）；百度无训练轮询阶段",
            raw=dict(payload),
        )

    def delete_voice(self, voice_id: str) -> None:
        self.send_json("POST", f"{VOICE_PATH}/delete", body={"voice_id": _as_int(voice_id)})

    def training_text(self) -> dict[str, Any]:
        """取训练文本（指定文本复刻用；text_id 24 小时内有效，创建后即失效）。"""
        return dict(self.send_json("POST", f"{VOICE_PATH}/text"))


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        media_type: str = "mp3",
        **options: Any,
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model or self.default_model)
        if len(text) > CHAR_LIMIT:
            raise ProviderError(
                f"文本 {len(text)} 字符超过百度非流式单次上限 {CHAR_LIMIT}", vendor=self.name
            )
        if stream:
            raise ProviderError(
                "百度的流式合成只有 WebSocket 一种方式（官方未提供 HTTP 分块方案）；"
                "本 adapter 只实现 HTTP 非流式，请去掉 stream=True",
                vendor=self.name,
            )
        body: dict[str, Any] = {
            "voice_id": _as_int(ref.raw),
            "text": text,
            "media_type": media_type,
            "pitch": 5,
            "volume": 5,
            "speed": 5,
        }
        body.update({k: v for k, v in options.items() if v is not None})
        started = now_ms()
        response = self.send("POST", f"{VOICE_PATH}/tts", body=body)
        content_type = (response.headers.get("content-type") or "").lower()
        if not content_type.startswith("audio"):
            self.classify(response.status_code, response.content, raw=response.content[:2000])
        return AudioResult(
            vendor=self.name,
            model=model or MODEL_PLACEHOLDER,
            voice_id=ref.raw,
            audio=response.content,
            format=media_type,
            sample_rate=None,  # 官方只支持降采，未回传实际采样率
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )


def _as_int(value: Any) -> Any:
    """百度的 ``voice_id`` 文档标注为 int；这里容忍字符串形态，转不动就原样传。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


__all__ += ["_as_int"]
