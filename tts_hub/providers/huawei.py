# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""华为云 SIS 适配器。IAM 项目级 Token 鉴权；零样本同步注册，音色以 voice_name
为主键且无删除端点；仅华东-上海一区域。"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from ..core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from ..core.net import hoshimachi_suisei
from ..core.types import (
    STATUS_FAILED,
    STATUS_READY,
    AudioResult,
    CloneTask,
    ModelInfo,
    SampleInput,
    VoiceInfo,
    VoiceRef,
)
from .base import KasumiToyama, now_ms

__all__ = ["PRESET_VOICES", "VOICE_NAME_RE", "UmiriYahata"]

REGION = "cn-east-3"
DEFAULT_ENDPOINT = f"https://sis-ext.{REGION}.myhuaweicloud.com"
DEFAULT_IAM_HOST = "iam.cn-north-4.myhuaweicloud.com"
CHAR_LIMIT = 300
VOICE_LIMIT = 1000
TOKEN_TTL = 24 * 3600.0

VOICE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")

PRESET_VOICES = {
    "chinese_huaxiaoli_common": "华小莉/标准女声",
    "chinese_huaxiaozhi_common": "华小智/男童声",
    "chinese_huaxiaotian_common": "华小天/朝气男声",
    "chinese_huaxiaoyuan_common": "华小媛/成熟女声",
    "chinese_huaxiaojing_common": "华小静/女童声",
    "chinese_huaxiaobo_common": "华小博/标准男声",
    "chinese_huaxiaorou_common": "华小柔/标准女声",
    "chinese_huaxiaoyou_common": "华小悠/嘹亮女声",
    "chinese_huaxiaoqing_common": "华小晴/青春女声",
    "chinese_huaxiaoxi_common": "华小溪/温柔女声",
    "chinese_huaxiaotong_common": "华小彤/俏皮女声",
    "chinese_huaxiaoya_common": "华小雅/标准女声",
    "chinese_huaxiaowei_common": "华小伟/成熟男声",
    "chinese_huaxiaoshuai_common": "华小帅/标准男声",
    "chinese_huaxiaojie_common": "华小杰/温柔男声",
}

SIS_CODE_KINDS: dict[str, type[TTSHubError]] = {
    "SIS.0101": AuthError,  # 验证 Token 异常（含"用了全局 token"）
    "SIS.0102": AuthError,  # 鉴权信息缺失
    "SIS.0103": AuthError,  # 实名认证缺失
    "SIS.0022": QuotaError,  # 产品不可购买
    "SIS.0023": QuotaError,
    "SIS.0024": QuotaError,
    "SIS.1202": QuotaError,  # 注册的声音数量超过限制
    "SIS.1222": ReviewRejectedError,  # 输入文本未通过风控检查
    "SIS.1201": ProviderError,  # voice_name 重复
    "SIS.1203": ProviderError,  # voice_name 未注册
    "SIS.1204": ProviderError,  # 合成文本长度不符合要求
    "SIS.1205": ProviderError,
    "SIS.1206": ProviderError,
    "SIS.1207": ProviderError,
    "SIS.1208": ProviderError,
    "SIS.1209": ProviderError,
    "SIS.1210": ProviderError,
    "SIS.1211": ProviderError,
    "SIS.1212": ProviderError,  # 音质过低
    "SIS.1213": ProviderError,  # voice_name 格式非法
    "SIS.1217": ProviderError,
    "SIS.1218": ProviderError,
    "SIS.1219": ProviderError,
    "SIS.1220": ProviderError,
    "SIS.1224": ProviderError,
    "SIS.1225": ProviderError,
    "SIS.1227": ProviderError,  # 与预置音色重名
    "SIS.0604": ProviderError,  # 合成字数超过上限
    "SIS.0012": ProviderError,
    "SIS.0031": ProviderError,
    "SIS.0032": ProviderError,
}


class UmiriYahata(KasumiToyama):
    """华为云 SIS 适配器（name = ``huawei``）。

    凭据映射：``api_key`` = IAM 用户名，``api_secret`` = 登录密码。
    另需在 providers.yaml 里给 ``domain_name``（账号名）与可选的 ``project_name``／``project_id``。
    若不想存密码，也可以用 ``x_auth_token`` 直接塞一个现成的项目级 Token（24 小时有效）。
    """

    name = "huawei"
    base_url = DEFAULT_ENDPOINT
    needs_secret = True
    default_model = "sis-vcs"
    clone_model = "sis-vcs"


    @property
    def iam_host(self) -> str:
        return str(self.setting("iam_host", DEFAULT_IAM_HOST))

    @property
    def region(self) -> str:
        return str(self.setting("region", REGION))

    def auth_headers(self) -> dict[str, str]:
        direct = self.setting("x_auth_token")
        token = str(direct) if direct else self.access_token()
        return {"X-Auth-Token": token, "Content-Type": "application/json"}

    def fetch_token(self) -> tuple[str, float]:
        """用用户名/密码换**项目级** IAM Token，并顺带记住 project id。

        两处容易踩错：① Token 在**响应头** ``X-Subject-Token`` 而不是响应体；
        ② 必须带 ``scope.project``，否则拿到的是全局 Token，调用 SIS 会报 SIS.0101。
        """
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.api_key,
                            "password": self.api_secret,
                            "domain": {"name": str(self.setting("domain_name", self.api_key))},
                        }
                    },
                },
                "scope": {
                    "project": {"name": str(self.setting("project_name", self.region))}
                },
            }
        }
        response = self.http.raw_request(
            "POST",
            f"https://{self.iam_host}/v3/auth/tokens",
            headers={"Content-Type": "application/json"},
            content=self.encode_body(body),
        )
        token = response.headers.get("X-Subject-Token")
        if not token:
            code, message = hoshimachi_suisei(response.content, response.status_code)
            raise AuthError(
                f"换取 IAM Token 失败（响应头缺少 X-Subject-Token）：{message or '响应无错误信息'}",
                vendor=self.name,
                code=code,
                status=response.status_code,
                raw=response.content[:2000],
            )
        ttl = TOKEN_TTL
        try:
            parsed = response.json()
            block = (parsed or {}).get("token") or {}
            project = block.get("project") or {}
            if project.get("id"):
                self._project_id = str(project["id"])
            expires_at = block.get("expires_at")
            if isinstance(expires_at, str):
                moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                ttl = max(60.0, (moment - datetime.now(UTC)).total_seconds())
        except (ValueError, AttributeError, TypeError):
            pass  # 解析不出就按官方口径的 24 小时算，不影响可用性
        return str(token), ttl

    def project_id(self) -> str:
        """URL 里的 ``{project_id}``；优先用配置，否则从换 Token 的响应里取。"""
        configured = self.setting("project_id")
        if configured:
            return str(configured)
        if not getattr(self, "_project_id", None):
            self.access_token()
        project = getattr(self, "_project_id", None)
        if not project:
            raise ProviderError(
                "无法确定 project_id：请在 providers.yaml 的 huawei 下显式配置 project_id",
                vendor=self.name,
            )
        return str(project)

    def base_path(self) -> str:
        return f"/v1/{self.project_id()}/vcs/voices"


    def classify(self, status: int, payload: Any, *, raw: Any = None) -> NoReturn:
        code, message = hoshimachi_suisei(payload, status)
        kind = SIS_CODE_KINDS.get(code or "")
        if kind is not None:
            raise kind(
                message or f"HTTP {status}", vendor=self.name, code=code, status=status, raw=raw
            )
        super().classify(status, payload, raw=raw)


    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="sis-vcs",
                display_name="华为云声音复刻（零样本）",
                supports_clone=True,
                supports_stream=False,  # 流式是独立 WSS 端点，纯 HTTP 不可用
                char_limit=CHAR_LIMIT,
                note="仅华东-上海一；音色以 voice_name 为主键，无 voice_id、无删除接口",
            )
        ]

    def list_voices(self) -> list[VoiceInfo]:
        payload = self.send_json("GET", self.base_path(), params={"limit": 100, "offset": 0})
        rows = ((payload.get("result") or {}).get("voices")) or []
        out = [
            VoiceInfo(
                voice_id=str(row.get("voice_name", "")),
                display_name=str(row.get("voice_name", "")),
                kind="cloned",
                model=str(row.get("language") or ""),
            )
            for row in rows
            if row.get("voice_name")
        ]
        out += [
            VoiceInfo(voice_id=name, display_name=label, kind="system", note="预置音色")
            for name, label in PRESET_VOICES.items()
        ]
        return out


    def clone(
        self,
        sample: SampleInput,
        *,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        language: str = "chinese",
        **options: Any,
    ) -> CloneTask:
        del model, transcript, preview_text
        voice_name = self.check_voice_name(name)
        body: dict[str, Any] = {
            "data": base64.b64encode(sample.read_bytes()).decode("ascii"),
            "config": {"voice_name": voice_name, "language": language},
        }
        body.update({k: v for k, v in options.items() if v is not None})
        payload = self.send_json("POST", self.base_path(), body=body)
        registered = (payload.get("result") or {}).get("voice_name")
        if not registered:
            raise ProviderError("注册响应缺少 result.voice_name", vendor=self.name, raw=payload)
        return CloneTask(
            vendor=self.name,
            status=STATUS_READY,
            voice_id=str(registered),
            task_id=str(registered),
            model=self.clone_model,
            message="注册成功（零样本、同步，无训练轮询）；音色以 voice_name 为主键",
            raw=dict(payload),
        )

    @staticmethod
    def check_voice_name(name: str | None) -> str:
        """官方两处文档对合法字符说法不一致，这里按**更严格**的一方本地先校验。

        先查预置名冲突再查格式：预置名本身有 24 个字符，长度就超了 20，
        如果先查格式，用户拿到的是"太长"而不是"你抄了预置音色的名字"。
        """
        if not name:
            raise ProviderError("华为云复刻要求提供音色名（config.voice_name）", vendor="huawei")
        if name in PRESET_VOICES:
            raise ProviderError(
                f"voice_name {name!r} 与预置音色重名，会被拒（SIS.1227）", vendor="huawei"
            )
        if not VOICE_NAME_RE.match(name):
            raise ProviderError(
                f"voice_name {name!r} 不合规：首字符须为字母、不能以数字或下划线开头、"
                "仅限字母数字下划线、长度不超过 20"
                "（官方注册页与 SIS.1213 两处说明不一致，本 adapter 取严）",
                vendor="huawei",
            )
        return name

    def clone_status(self, task_id: str) -> CloneTask:
        """没有任务态；查一次列表确认音色真的出现了。

        官方只说"秒级注册"，**没有明文承诺注册返回后立即可用**，所以这里实测一次而不是假设。
        列表按页翻查（单页 100 条、上限 VOICE_LIMIT）——只查一页的话，音色多于 100 个的
        账号会把"排在后面"误判成"没注册上"。
        """
        if self._voice_registered(task_id):
            return CloneTask(
                vendor=self.name,
                status=STATUS_READY,
                voice_id=task_id,
                task_id=task_id,
                message="音色已在注册列表中",
            )
        return CloneTask(
            vendor=self.name,
            status=STATUS_FAILED,
            task_id=task_id,
            message="注册列表中查不到该 voice_name（零样本注册是同步的，查不到即视为失败）",
        )

    def _voice_registered(self, voice_name: str) -> bool:
        """翻页查注册列表；翻到空页为止，最多 VOICE_LIMIT // 100 页。"""
        offset = 0
        page = 100
        for _ in range(max(1, VOICE_LIMIT // page)):
            payload = self.send_json(
                "GET", self.base_path(), params={"limit": page, "offset": offset}
            )
            rows = ((payload.get("result") or {}).get("voices")) or []
            if any(str(row.get("voice_name")) == voice_name for row in rows):
                return True
            if len(rows) < page:
                return False
            offset += page
        return False


    def synthesize(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        model: str | None = None,
        stream: bool = False,
        audio_format: str = "mp3",
        sample_rate: str = "24000",
        **options: Any,
    ) -> AudioResult:
        ref = VoiceRef.of(voice, vendor=self.name, model=model or self.default_model)
        if len(text) > CHAR_LIMIT:
            raise ProviderError(
                f"文本 {len(text)} 字符超过华为云 HTTPS 合成上限 {CHAR_LIMIT}", vendor=self.name
            )
        if stream:
            raise ProviderError(
                "华为云的流式合成是独立的 WSS 端点 "
                "（wss /v1/{project_id}/vcs/voices/clone，音频为裸二进制而非 base64）；"
                "本 adapter 只实现 HTTPS 非流式合成",
                vendor=self.name,
            )
        config: dict[str, Any] = {
            "audio_format": audio_format,
            "sample_rate": sample_rate,
            "voice_name": ref.raw,
        }
        config.update({k: v for k, v in options.items() if v is not None})
        started = now_ms()
        payload = self.send_json(
            "POST", f"{self.base_path()}/clone", body={"text": text, "config": config}
        )
        encoded = (payload.get("result") or {}).get("data")
        if not encoded:
            raise ProviderError("合成响应缺少 result.data", vendor=self.name, raw=payload)
        return AudioResult(
            vendor=self.name,
            model=model or self.default_model or "",
            voice_id=ref.raw,
            audio=base64.b64decode(encoded),
            format=audio_format,
            sample_rate=_sample_rate_hz(sample_rate),
            chars=len(text),
            latency_ms=int(now_ms() - started),
        )


def _sample_rate_hz(value: Any) -> int | None:
    """把官方的 ``"16kHz"`` / ``"24000"`` 形态折成整数赫兹。"""
    text = str(value or "").strip().lower().removesuffix("hz").removesuffix("k")
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number * 1000) if number < 1000 else int(number)
