# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""厂商适配器包与工厂（能力探测而非导入成功）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from ..core.errors import AuthError, ProviderError
from ..core.net import MocaAoba
from ..core.provider import TTSProvider
from .baidu import MisumiUika
from .base import KasumiToyama
from .dashscope import TakiShiina
from .huawei import UmiriYahata
from .iflytek import ArareNakamachi
from .minimax import SaayaYamabuki
from .stepfun import ArisaIchigaya
from .tencent import SakikoTogawa
from .unisound import MutsumiWakaba
from .volcengine import NyamuYutenji
from .zhipu import RanMitake

if TYPE_CHECKING:
    from ..config import Settings

__all__ = [
    "ADAPTERS",
    "REQUIRED_METHODS",
    "ArareNakamachi",
    "ArisaIchigaya",
    "KasumiToyama",
    "MisumiUika",
    "MutsumiWakaba",
    "NyamuYutenji",
    "RanMitake",
    "SaayaYamabuki",
    "SakikoTogawa",
    "TakiShiina",
    "TomoeUdagawa",
    "UmiriYahata",
]

ADAPTERS: dict[str, type[KasumiToyama]] = {
    SaayaYamabuki.name: SaayaYamabuki,
    ArisaIchigaya.name: ArisaIchigaya,
    RanMitake.name: RanMitake,
    TakiShiina.name: TakiShiina,
    MisumiUika.name: MisumiUika,
    MutsumiWakaba.name: MutsumiWakaba,
    SakikoTogawa.name: SakikoTogawa,
    UmiriYahata.name: UmiriYahata,
    NyamuYutenji.name: NyamuYutenji,
    ArareNakamachi.name: ArareNakamachi,
}

REQUIRED_METHODS = ("list_models", "list_voices", "clone", "clone_status", "synthesize")


class TomoeUdagawa:
    """适配器工厂：按配置装配实例，注入共享的出站客户端。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http: MocaAoba | None = None,
        is_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.http = http
        self._made: dict[str, KasumiToyama] = {}
        self._is_enabled = is_enabled or (lambda name: settings.providers[name].enabled)


    def names(self) -> list[str]:
        """配置里声明过的厂商，顺序稳定。"""
        return list(self.settings.providers.keys())

    def enabled(self) -> list[str]:
        return [n for n in self.names() if self._is_enabled(n)]

    def has_key(self, vendor: str) -> bool:
        cfg = self.settings.providers.get(vendor)
        return bool(cfg and cfg.api_key(self.settings.env))

    def configured(self) -> list[str]:
        """既有密钥又启用的厂商。"""
        return [n for n in self.enabled() if self.has_key(n)]

    def probe(self, vendor: str) -> bool:
        """能力探测：类存在且协议方法齐备才算可用。"""
        cls = ADAPTERS.get(vendor)
        if cls is None:
            return False
        return all(callable(getattr(cls, method, None)) for method in REQUIRED_METHODS)


    def make(self, vendor: str, *, require_key: bool = True) -> TTSProvider:
        """构造（并缓存）某厂商的适配器实例。

        返回类型声明为协议而不是基类：各家的方法签名本来就不同（`clone` 的可选参数、
        `synthesize` 的厂商专属开关），基类里放桩函数只会逼着子类去凑一个假的统一签名。
        这里用一次显式 cast 断言"它实现了协议"，并由 `probe()` 与测试兜住这个断言。
        """
        if vendor in self._made:
            return cast("TTSProvider", self._made[vendor])
        if not self.probe(vendor):
            raise ProviderError(f"未知或能力不完整的厂商：{vendor!r}")
        cfg = self.settings.providers.get(vendor)
        if cfg is None:
            raise ProviderError(f"providers.yaml 未声明厂商：{vendor!r}")
        if not self._is_enabled(vendor):
            raise ProviderError(f"厂商 {vendor!r} 已停用")
        key = cfg.api_key(self.settings.env)
        if not key and require_key:
            raise AuthError(
                f"缺少 {vendor} 的 API 密钥（环境变量 {cfg.api_key_env}）", vendor=vendor
            )
        instance = ADAPTERS[vendor](
            key or "",
            api_secret=cfg.api_secret(self.settings.env),
            http=self.http,
            base_url=cfg.base_url,
            timeout=self.settings.timeout,
            proxy=self.settings.proxy,
            allow_private=self.settings.allow_private_urls,
            require_key=require_key,
        )
        if cfg.default_model:
            instance.default_model = cfg.default_model
        if cfg.clone_model:
            instance.clone_model = cfg.clone_model
        instance.configure(cfg.extra)
        self._made[vendor] = instance
        return cast("TTSProvider", instance)

    def close(self) -> None:
        for instance in self._made.values():
            instance.close()
        self._made.clear()
