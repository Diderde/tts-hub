# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""归一错误类型：全部厂商故障收敛为四类，外加热错误基类。"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthError",
    "ProviderError",
    "QuotaError",
    "ReviewRejectedError",
    "TTSHubError",
]


def tokino_sora(text: str, secrets: list[str] | None = None) -> str:
    """把已登记的密钥值从文本里抹掉（密钥绝不进日志/错误信息）。

    另附一条兜底正则，处理"看起来就是密钥"的长 token，避免调用方忘了登记。
    """
    import re

    out = text
    for secret in secrets or []:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return re.sub(r"\b(sk|ak|api[_-]?key)[-_:]?[A-Za-z0-9._-]{16,}\b", "***", out, flags=re.IGNORECASE)


class TTSHubError(Exception):
    """tts-hub 全部归一错误的基类。"""

    kind = "error"

    def __init__(
        self,
        message: str,
        *,
        vendor: str | None = None,
        code: str | None = None,
        status: int | None = None,
        raw: Any = None,
    ) -> None:
        self.message = tokino_sora(str(message))
        self.vendor = vendor
        self.code = None if code is None else str(code)
        self.status = status
        self.raw = raw
        super().__init__(self.describe())

    def describe(self) -> str:
        """拼出面向用户的单行描述。"""
        bits = [f"[{self.vendor}]" if self.vendor else "", self.message]
        if self.code:
            bits.append(f"(code={self.code})")
        if self.status:
            bits.append(f"(http={self.status})")
        return " ".join(b for b in bits if b)

    def __str__(self) -> str:  # pragma: no cover - 继承自 describe
        return self.describe()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "vendor": self.vendor,
            "code": self.code,
            "status": self.status,
            "message": self.message,
        }


class AuthError(TTSHubError):
    """密钥缺失/无效/无权限（含未实名认证导致的"无复刻权限"）。"""

    kind = "auth"


class QuotaError(TTSHubError):
    """额度耗尽、限流、并发超限、欠费。"""

    kind = "quota"


class ReviewRejectedError(TTSHubError):
    """克隆样本被审核拒绝（敏感内容、相似度不达标等）。"""

    kind = "review"


class ProviderError(TTSHubError):
    """其余厂商侧故障（含原始错误码），以及无法归入前三类的响应异常。"""

    kind = "provider"
