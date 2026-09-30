# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""MiniMax 适配器。合成 POST /v1/t2a_v2（音频为 hex 编码）；
克隆 /v1/files/upload(purpose=voice_clone) 后 /v1/voice_clone；错误一律以
HTTP 200 返回，成败看 base_resp.status_code。复刻音色 168 小时未被正式调用会被删除。"""

from __future__ import annotations

import hashlib
import json
import re
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

__all__ = ["SaayaYamabuki", "aki_rosenthal"]

SYNTH_MODELS = (
    "speech-2.8-hd",
    "speech-2.8-turbo",
    "speech-2.6-hd",
    "speech-2.6-turbo",
    "speech-02-hd",
    "speech-02-turbo",
    "speech-01-hd",
    "speech-01-turbo",
)
DEFAULT_SYNTH_MODEL = "speech-2.8-turbo"
DEFAULT_CLONE_MODEL = "speech-2.8-hd"
CHAR_LIMIT = 10000
CLONE_TTL_HOURS = 168

_VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,255}$")


def aki_rosenthal(name: str, *, salt: str = "") -> str:
    """按 MiniMax 的 ``voice_id`` 规则生成合法且可复现的 ID。

    规则：长度 [8,256]、首字符英文字母、仅字母数字与 ``-``/``_``、末位非 ``-``/``_``。
    中文名会被过滤掉，因此统一加 ``ttshub`` 前缀并以内容哈希保证唯一性。
    """
    cleaned = re.sub(r"[^0-9A-Za-z_-]", "", name) or "voice"
    if not cleaned[0].isalpha():
        cleaned = "v" + cleaned
    digest = hashlib.sha256(f"{name}|{salt}".encode()).hexdigest()[:8]
    candidate = f"ttshub{cleaned}-{digest}"[:256].rstrip("-_")
    if not _VOICE_ID_RE.match(candidate):  # pragma: no cover - 兜底，正常不会到
        candidate = f"ttshubvoice-{digest}"
    return candidate


class SaayaYamabuki(KasumiToyama):
    """MiniMax 适配器（name = ``minimax``）。"""

    name = "minimax"
    base_url = "https://api.minimax.cn"
    default_model = DEFAULT_SYNTH_MODEL
    clone_model = DEFAULT_CLONE_MODEL
    clone_ttl_hours = CLONE_TTL_HOURS

    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {
        "1000": ProviderError,
        "1001": ProviderError,
        "1002": QuotaError,
        "1004": AuthError,
        "1008": QuotaError,
        "1024": ProviderError,
        "1026": ReviewRejectedError,
        "1027": ReviewRejectedError,
        "1033": ProviderError,
        "1039": ProviderError,
        "1041": QuotaError,
        "1042": ProviderError,
        "1043": ReviewRejectedError,  # ASR 相似度检查失败
        "1044": ReviewRejectedError,  # 克隆提示词相似度检查失败
        "2013": ProviderError,
        "20132": ProviderError,
        "2037": ProviderError,  # 样本时长不符（10s~5min）
        "2038": AuthError,  # 无复刻权限：未完成实名认证
        "2039": ProviderError,  # voice_id 重复
        "2042": AuthError,  # 无权访问该 voice_id
        "2045": QuotaError,
        "2048": ProviderError,  # 提示音频超过 8s
        "2049": AuthError,
        "2056": QuotaError,
    }


    def business_error(self, payload: Mapping[str, Any]) -> tuple[str | None, str] | None:
        """MiniMax 用 ``base_resp.status_code`` 报错，HTTP 状态码永远是 200。"""
        base = payload.get("base_resp")
        if isinstance(base, Mapping):
            code = base.get("status_code")
            if code not in (0, None, "0"):
                return str(code), str(base.get("status_msg") or "")
        return None

    def send(self, method: str, path: str, **kw: Any) -> Any:
        """附带可选的 ``GroupId`` 查询参数。

        现行官方文档里没有任何 GroupId 参数（token 内嵌 group 概念），因此**默认不发**；
        仅当调用方在 providers.yaml 里显式配了 ``group_id`` 才作为查询参数附带。
        """
        group_id = self.setting("group_id")
        if group_id:
            params = dict(kw.get("params") or {})
            params.setdefault("GroupId", str(group_id))
            kw["params"] = params
        return super().send(method, path, **kw)


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=model,
                display_name=model,
                supports_clone=True,
                supports_stream=True,
                char_limit=CHAR_LIMIT,
                note="hd 3.50 元/万字符；turbo 2.00 元/万字符",
            )
            for model in SYNTH_MODELS
        ]

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("POST", "/v1/get_voice", body={"voice_type": "all"})
        out: list[VoiceInfo] = []
        for key, kind in (
            ("system_voice", "system"),
            ("voice_cloning", "cloned"),
            ("voice_generation", "cloned"),
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
        model: str = DEFAULT_CLONE_MODEL,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        voice_id: str | None = None,
        clone_prompt: SampleInput | None = None,
        prompt_text: str | None = None,
        text_validation: str | None = None,
        accuracy: float | None = None,
    ) -> CloneTask:
        """上传样本并复刻。

        ``preview_text`` 走的是"接口内试听"通道：它**不算正式调用**，音色仍需在
        :meth:`synthesize` 里用一次才不会被 168 小时后清除。
        """
        file_id = self.upload_sample(sample, purpose="voice_clone")
        vid = voice_id or aki_rosenthal(name or "voice", salt=sample.display_name)
        body: dict[str, Any] = {"file_id": file_id, "voice_id": vid}
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
            body["model"] = model
        if text_validation:
            body["text_validation"] = text_validation
            if accuracy is not None:
                body["accuracy"] = accuracy
        payload = self.send_json("POST", "/v1/voice_clone", body=body)
        kind, label = self.safety_flag(payload)
        if kind:
            raise ReviewRejectedError(
                f"样本音频命中风控（{label}）", vendor=self.name, code=str(kind), raw=payload
            )
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=vid,
            task_id=vid,
            model=body.get("model"),
            message=(
                f"复刻成功；注意复刻音色为临时音色，{CLONE_TTL_HOURS} 小时内未在合成接口"
                "正式调用会被系统删除（接口内试听不算）"
            ),
            raw=dict(payload),
        )

    def safety_flag(self, payload: Mapping[str, Any]) -> tuple[int, str]:
        """读取风控标记，兼容官方 schema（object）与 example（bool + 平级 int）两种形态。"""
        labels = {0: "正常", 1: "严重违规", 2: "色情", 3: "广告", 4: "违禁", 5: "谩骂", 6: "暴恐", 7: "其他"}
        flag = payload.get("input_sensitive")
        code = 0
        if isinstance(flag, bool):
            code = int(flag)
            flat = payload.get("input_sensitive_type")
            if isinstance(flat, int):
                code = flat
        elif isinstance(flag, Mapping):
            raw = flag.get("type")
            code = int(raw) if isinstance(raw, int) else 0
        elif isinstance(flag, int):
            code = flag
        return code, labels.get(code, f"未知({code})")

    def upload_sample(self, sample: SampleInput, *, purpose: str) -> int:
        """上传样本，返回 int64 的 file_id。"""
        filename, mime, raw = self.sample_bytes(sample)
        payload = self.send_json(
            "POST",
            "/v1/files/upload",
            data={"purpose": purpose},
            files={"file": (filename, raw, mime)},
        )
        info = payload.get("file")
        if not isinstance(info, Mapping) or info.get("file_id") in (None, ""):
            raise ProviderError("上传响应缺少 file.file_id", vendor=self.name, raw=payload)
        return int(info["file_id"])

    def clone_status(self, task_id: str) -> CloneTask:
        """同步型厂商：无轮询阶段，原样回显终态。"""
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=task_id,
            task_id=task_id,
            message="MiniMax 为同步复刻，无轮询阶段",
        )

    def delete_voice(self, voice_id: str) -> None:
        """删除复刻音色；官方明确警告删除后该 voice_id 无法再次使用。"""
        self.send_json("POST", "/v1/delete_voice", body={"voice_type": "voice_cloning", "voice_id": voice_id})


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str = DEFAULT_SYNTH_MODEL,
        stream: bool = False,
        audio_format: str = "mp3",
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model)
        if model not in SYNTH_MODELS:
            raise ProviderError(
                f"未知合成模型 {model!r}；可选：{', '.join(SYNTH_MODELS)}", vendor=self.name
            )
        if len(text) >= CHAR_LIMIT:
            raise ProviderError(
                f"文本 {len(text)} 字符达到 MiniMax 单次上限 {CHAR_LIMIT}（官方为 <），请切分",
                vendor=self.name,
            )
        body: dict[str, Any] = {
            "model": model,
            "text": text,
            "stream": stream,
            "voice_setting": {"voice_id": ref.raw, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"format": audio_format, "sample_rate": 32000},
        }
        started = now_ms()
        if stream:
            return AudioResult(
                vendor=self.name,
                model=model,
                voice_id=ref.raw,
                format=audio_format,
                chars=len(text),
                stream=True,
                chunks=self.stream_speech(body),
            )
        payload = self.send_json("POST", "/v1/t2a_v2", body=body)
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("audio"):
            raise ProviderError("合成响应缺少 data.audio", vendor=self.name, raw=payload)
        extra = payload.get("extra_info") or {}
        return AudioResult(
            vendor=self.name,
            model=model,
            voice_id=ref.raw,
            audio=bytes.fromhex(str(data["audio"])),
            format=str(extra.get("audio_format") or audio_format),
            sample_rate=int(extra["audio_sample_rate"]) if extra.get("audio_sample_rate") else None,
            chars=int(extra.get("usage_characters") or len(text)),
            latency_ms=int(now_ms() - started),
        )

    def stream_speech(self, body: Mapping[str, Any]) -> Iterator[bytes]:
        """流式合成：每个 chunk 的 ``data.audio`` 是 hex 片段；HTTP 200 也可能是错误体。"""
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        lines = self.http.stream_bytes(
            "POST", self.url("/v1/t2a_v2"), headers=self.headers(), content=payload
        )
        for line in akai_haato(lines):
            text = line.strip()
            if not text:
                continue
            if text.startswith("data:"):
                text = text[5:].strip()
            if not text or text == "[DONE]":
                continue
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            biz = self.business_error(event)
            if biz is not None:
                code, message = biz
                self.classify(200, {"base_resp": {"status_code": code, "status_msg": message}}, raw=event)
            chunk = (event.get("data") or {}).get("audio")
            if chunk:
                yield bytes.fromhex(str(chunk))
