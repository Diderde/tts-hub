# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""科大讯飞适配器。训练侧为两级鉴权的 HTTP；合成仅有 WebSocket，本 adapter
明确降级并提供签名 URL。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from ..core.errors import AuthError, ProviderError, QuotaError, TTSHubError
from ..core.signing import kiryu_coco
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
from .base import KasumiToyama

__all__ = ["DEFAULT_TEXT_ID", "TRAIN_HOST", "WS_HOST", "WS_PATH", "ArareNakamachi"]

TRAIN_HOST = "opentrain.xfyousheng.com"
TOKEN_HOST = "avatar-hci.xfyousheng.com"
TOKEN_PATH = "/aiauth/v1/token"
WS_HOST = "cn-huabei-1.xf-yun.com"
WS_PATH = "/v1/private/voice_clone"
DEFAULT_TEXT_ID = 5001
RESOURCE_TYPE = 12
MAX_SAMPLE_BYTES = 3 * 1024 * 1024
WS_TEXT_BYTES = 8000

IFLYTEK_CODE_KINDS: dict[str, type[TTSHubError]] = {
    "10000": AuthError,  # token 过期
    "10001": AuthError,  # 缺 X-AppId / X-Token
    "10015": AuthError,  # 无权操作
    "10016": AuthError,  # 无效 appid
    "10019": AuthError,  # 授权过期
    "10020": AuthError,  # IP 未授权
    "10018": QuotaError,  # 未分配训练路数
    "10021": QuotaError,  # 未分配训练次数
    "11200": QuotaError,  # 功能未授权/授权到期
    "11201": QuotaError,  # 每日交互次数超限
    "10010": QuotaError,
    "000004": AuthError,
    "000006": AuthError,
    "000007": AuthError,  # sign 校验失败
    "20001": ProviderError,  # textId 无效
    "20002": ProviderError,  # textSegId 无效
    "60000": ProviderError,  # 任务不存在
    "90001": ProviderError,
    "90002": ProviderError,
}

TRAINING_STATUS_MAP = {0: STATUS_FAILED, 1: STATUS_READY}


def _md5_hex(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.md5(raw).hexdigest()


class ArareNakamachi(KasumiToyama):
    """科大讯飞适配器（name = ``iflytek``）。

    凭据映射：``api_key`` = 控制台的 **APIKey**，``api_secret`` = **APISecret**（合成签名用），
    另需在 providers.yaml 里给 ``appid``。
    """

    name = "iflytek"
    base_url = f"http://{TRAIN_HOST}"
    needs_secret = True
    default_model = "x5_clone"
    clone_model = "x5_clone"

    CODE_KINDS = IFLYTEK_CODE_KINDS


    @property
    def appid(self) -> str:
        value = self.setting("appid")
        if not value:
            raise ProviderError(
                "讯飞训练接口需要 appid：请在 providers.yaml 的 iflytek 下配置 appid",
                vendor=self.name,
            )
        return str(value)

    def auth_headers(self) -> dict[str, str]:
        return {}

    def fetch_token(self) -> tuple[str, float]:
        """换 ``accesstoken``；``Authorization`` 是双层 MD5，时间戳要与 body 里的一致。"""
        stamp = str(int(time.time() * 1000))
        body = {"base": {"appid": self.appid, "version": "v1", "timestamp": stamp}, "model": "remote"}
        payload = self.encode_body(body) or b"{}"
        authorization = _md5_hex(_md5_hex(f"{self.api_key}{stamp}") + payload.decode("utf-8"))
        response = self.http.raw_request(
            "POST",
            f"http://{TOKEN_HOST}{TOKEN_PATH}",
            headers={"Authorization": authorization, "Content-Type": "application/json"},
            content=payload,
        )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise AuthError("换取 accesstoken 失败：响应不是合法 JSON", vendor=self.name) from exc
        token = (parsed or {}).get("accesstoken")
        if not token:
            code = str((parsed or {}).get("retcode") or "")
            self.classify_error(code, str((parsed or {}).get("desc") or "响应缺少 accesstoken"))
        return str(token), float((parsed or {}).get("expiresin") or 7200)

    def signed_headers(self, method: str, url: str, payload: bytes | None) -> dict[str, str]:
        """讯飞训练的三个业务头。

        ``X-Sign = MD5(apikey + X-Time + MD5(body))``。这里对 ``payload``（即将发出的
        raw 字节）求 MD5，与官方 Java demo 一致；Python demo 用的是 dict repr，
        两者结果不同，服务端容忍哪一种**未抓到**。
        """
        stamp = str(int(time.time() * 1000))
        body_md5 = _md5_hex(payload if payload is not None else b"")
        sign = _md5_hex(f"{self.api_key}{stamp}{body_md5}")
        return {
            "X-AppId": self.appid,
            "X-Token": self.access_token(),
            "X-Time": stamp,
            "X-Sign": sign,
        }

    def classify_error(self, code: str, message: str, raw: Any = None) -> None:
        kind = IFLYTEK_CODE_KINDS.get(code)
        if kind is not None:
            raise kind(message or code, vendor=self.name, code=code, raw=raw)
        if code and code != "000000":
            raise ProviderError(message or code, vendor=self.name, code=code, raw=raw)

    def business_error(self, payload: Mapping[str, Any]) -> tuple[str | None, str] | None:
        """讯飞用 ``retcode`` 报错，成功是 ``"000000"``。"""
        code = payload.get("retcode")
        if code in (None, "000000", 0):
            return None
        return str(code), str(payload.get("desc") or payload.get("failedDesc") or "")


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="x5_clone",
                display_name="一句话复刻·标准版",
                supports_clone=True,
                supports_stream=False,  # 合成只有 WebSocket
                char_limit=None,
                note="合成仅 WSS；标准版每种语种需单独训练",
            ),
            ModelInfo(
                id="x6_clone",
                display_name="一句话复刻·多风格版",
                supports_clone=True,
                supports_stream=False,
                char_limit=None,
                note="训练时需带 engineVersion=omni_v1；一次训练覆盖八语种",
            ),
        ]

    def list_voices(self) -> list[VoiceInfo]:
        """官方没有"列出全部复刻音色"的接口，因此不做无根据的猜测。"""
        return []


    def training_text(self, *, text_id: int = DEFAULT_TEXT_ID) -> dict[str, Any]:
        """取训练文本。一句话复刻**必须照着它给的文本录**，没有自定义文本复刻。"""
        payload = self.send_json("POST", "/voice_train/task/traintext", body={"textId": text_id})
        data = payload.get("data") or {}
        if not data.get("textSegs"):
            raise ProviderError("训练文本接口没有返回 textSegs", vendor=self.name, raw=payload)
        return dict(data)

    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        text_id: int | None = None,
        text_seg_id: str | None = None,
        **options: Any,
    ) -> CloneTask:
        del transcript, preview_text
        if text_id is None or text_seg_id is None:
            data = self.training_text(text_id=text_id or DEFAULT_TEXT_ID)
            first = (data.get("textSegs") or [{}])[0]
            raise ProviderError(
                "一句话复刻必须照官方文本录制，本 adapter 不替你猜。\n"
                f"请朗读这段（textId={data.get('textId')}，textSegId={first.get('segId')}）：\n"
                f"  「{first.get('segText')}」\n"
                f"录好后带上 text_id={data.get('textId')} 与 text_seg_id={first.get('segId')} 重新调用。",
                vendor=self.name,
            )
        raw = sample.read_bytes() if not sample.url else None
        if raw is not None and len(raw) > MAX_SAMPLE_BYTES:
            raise ProviderError(
                f"训练音频 {len(raw)} 字节超过讯飞 3MB 上限", vendor=self.name
            )

        task_body: dict[str, Any] = {"resourceType": RESOURCE_TYPE}
        if model == "x6_clone" or options.pop("multi_style", False):
            task_body["engineVersion"] = "omni_v1"
        if name:
            task_body["taskName"] = name
        task_body.update({k: v for k, v in options.items() if v is not None})
        created = self.send_json("POST", "/voice_train/task/add", body=task_body)
        task_id = created.get("data")
        if not task_id:
            raise ProviderError("创建训练任务响应缺少 data(taskId)", vendor=self.name, raw=created)

        if raw is None:
            self.send_json(
                "POST",
                "/voice_train/audio/v1/add",
                body={
                    "taskId": task_id,
                    "audioUrl": sample.url,
                    "textId": text_id,
                    "textSegId": text_seg_id,
                },
            )
            self.send_json("POST", "/voice_train/task/submit", body={"taskId": task_id})
        else:
            self.send_json(
                "POST",
                "/voice_train/task/submitWithAudio",
                data={"taskId": task_id, "textId": text_id, "textSegId": text_seg_id},
                files={"file": (sample.display_name, raw, sample.mime or "audio/wav")},
            )
        return CloneTask(
            vendor=self.name,
            status=STATUS_TRAINING,
            task_id=str(task_id),
            model=model or self.clone_model,
            message="训练任务已提交（轮询型），用 clone_status 查到 trainStatus=1 后取 assetId 作为合成 res_id",
            raw=dict(created),
        )

    def clone_status(self, task_id: str) -> CloneTask:
        """``trainStatus``：``-1`` 训练中 / ``1`` 成功 / ``0`` 失败；``2`` 两处文档说法不一致，
        这里按"非终态"处理，与两边都不冲突。"""
        payload = self.send_json("POST", "/voice_train/task/result", body={"taskId": task_id})
        data = payload.get("data") or {}
        raw_status = data.get("trainStatus")
        status = TRAINING_STATUS_MAP.get(raw_status if isinstance(raw_status, int) else -1, STATUS_TRAINING)
        asset_id = data.get("assetId")
        return CloneTask(
            vendor=self.name,
            status=status,
            voice_id=str(asset_id) if (status == STATUS_READY and asset_id) else None,
            task_id=task_id,
            model=self.clone_model,
            message=str(data.get("failedDesc") or f"trainStatus={raw_status}"),
            raw=dict(data),
        )


    def signed_ws_url(self, *, scheme: str = "wss", date: str | None = None) -> str:
        """算好合成用的签名 URL，交给外部 WebSocket 客户端使用。

        讯飞签的是 **URL**（``host``/``date``/``request-line`` 三段 HMAC-SHA256），
        与训练那套 MD5 头完全无关。这里把地址算出来但不连——httpx 不支持 WebSocket。
        """
        return kiryu_coco(
            host=WS_HOST,
            path=WS_PATH,
            api_key=self.api_key,
            api_secret=self.api_secret or "",
            scheme=scheme,
            date=date,
        )

    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        **options: Any,
    ) -> AudioResult:
        del text, voice, model, stream, options
        raise ProviderError(
            "科大讯飞的一句话复刻**没有 HTTP 合成接口**：合成只有 WebSocket "
            f"（wss://{WS_HOST}{WS_PATH}），而长文本那个 HTTP 接口的 vcn 只支持 x4_* 预置音色、"
            "不收复刻的 res_id。\n"
            "本 adapter 不实现 WSS，所以不提供合成——需要的话可以用 "
            "`adapter.signed_ws_url()` 拿到算好的签名地址自行接 WebSocket，"
            "或改用别家厂商合成（逻辑音色可以在多家之间切换，这正是本项目的用途）。",
            vendor=self.name,
        )
