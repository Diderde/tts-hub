# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""配置装载：providers.yaml 与 .env；密钥只从环境读取，不入库、不进日志。"""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = ["DEFAULT_PROVIDERS", "ProviderSettings", "RimiUshigome", "Settings"]

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "minimax": {
        "enabled": True,
        "api_key_env": "MINIMAX_API_KEY",
        "base_url": "https://api.minimax.cn",
        "default_model": "speech-2.8-turbo",
        "clone_model": "speech-2.8-hd",
    },
    "stepfun": {
        "enabled": True,
        "api_key_env": "STEPFUN_API_KEY",
        "base_url": "https://api.stepfun.com/v1",
        "default_model": "stepaudio-2.5-tts",
        "clone_model": "step-tts-2",
    },
    "zhipu": {
        "enabled": True,
        "api_key_env": "ZHIPU_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-tts",
        "clone_model": "glm-tts-clone",
    },
    "aliyun": {
        "enabled": True,
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com",
        "default_model": "cosyvoice-v3-flash",
        "clone_model": "cosyvoice-v3-flash",
    },
    "baidu": {
        "enabled": True,
        "api_key_env": "BAIDU_API_KEY",
        "api_secret_env": "BAIDU_SECRET_KEY",
    },
    "unisound": {
        "enabled": True,
        "api_key_env": "UNISOUND_API_KEY",
        "base_url": "https://maas-api.unisound.com",
    },
    "tencent": {
        "enabled": True,
        "api_key_env": "TENCENT_SECRET_ID",
        "api_secret_env": "TENCENT_SECRET_KEY",
    },
    "huawei": {
        "enabled": True,
        "api_key_env": "HUAWEI_USERNAME",
        "api_secret_env": "HUAWEI_PASSWORD",
    },
    "volcengine": {
        "enabled": True,
        "api_key_env": "VOLCENGINE_API_KEY",
        "base_url": "https://openspeech.bytedance.com",
        "billing": "prepaid",
        "ack_first_charge": False,
    },
    "iflytek": {
        "enabled": True,
        "api_key_env": "IFLYTEK_API_KEY",
        "api_secret_env": "IFLYTEK_API_SECRET",
    },
}

_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """单个厂商的开关与默认值。"""

    name: str
    enabled: bool = True
    api_key_env: str = ""
    api_secret_env: str = ""
    base_url: str | None = None
    default_model: str | None = None
    clone_model: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def _read(self, env: Mapping[str, str], name: str) -> str | None:
        if not name:
            return None
        value = env.get(name) or os.environ.get(name)
        return value.strip() if value and value.strip() else None

    def api_key(self, env: Mapping[str, str]) -> str | None:
        return self._read(env, self.api_key_env)

    def api_secret(self, env: Mapping[str, str]) -> str | None:
        """第二把密钥（如百度的 Secret Key）；没有就是 ``None``。"""
        return self._read(env, self.api_secret_env)


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行的全部配置。"""

    root: pathlib.Path
    data_dir: pathlib.Path
    db_path: pathlib.Path
    samples_dir: pathlib.Path
    out_dir: pathlib.Path
    default_vendor: str = "minimax"
    fallback: tuple[str, ...] = ()
    proxy: str | None = None
    allow_private_urls: bool = False
    timeout: float = 60.0
    host: str = "127.0.0.1"
    port: int = 8000
    providers: Mapping[str, ProviderSettings] = field(default_factory=dict)
    pricing: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)

    def provider(self, name: str) -> ProviderSettings:
        return self.providers[name]

    def enabled_vendors(self) -> list[str]:
        return [n for n, cfg in self.providers.items() if cfg.enabled]

    @property
    def exposes_lan(self) -> bool:
        """监听地址是否超出本机（``0.0.0.0`` / 局域网 IP 都算）。

        §9 要求默认只听 127.0.0.1；一旦超出，服务启动时必须给出显式告警，
        而不是让用户以为它还是本机的。
        """
        host = (self.host or "").strip().lower()
        return host not in ("127.0.0.1", "localhost", "::1", "[::1]")

    def secrets(self) -> list[str]:
        """所有已知密钥值（含第二把密钥），供日志脱敏使用。"""
        out = []
        for cfg in self.providers.values():
            for value in (cfg.api_key(self.env), cfg.api_secret(self.env)):
                if value:
                    out.append(value)
        return out


def yozora_mel(path: str | pathlib.Path) -> dict[str, str]:
    """解析 ``.env``（只支持 ``KEY=VALUE``、``export`` 前缀、引号与 ``#`` 注释）。"""
    target = pathlib.Path(path)
    values: dict[str, str] = {}
    if not target.is_file():
        return values
    for raw in target.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def hitomi_chris(path: str | pathlib.Path) -> dict[str, Any]:
    """读 ``providers.yaml``；缺失或为空返回空字典（交由调用方回退默认）。"""
    target = pathlib.Path(path)
    if not target.is_file():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 声明
        raise RuntimeError("读取 providers.yaml 需要 PyYAML（pip install PyYAML）") from exc
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{target} 顶层必须是映射")
    return loaded


def ookami_mio(root: pathlib.Path, raw: Any) -> pathlib.Path:
    """把配置里的目录值解析成绝对路径（相对值以项目根为基准）。"""
    path = pathlib.Path(str(raw))
    return path if path.is_absolute() else (root / path)


class RimiUshigome:
    """配置装载器。"""

    def __init__(
        self,
        root: str | pathlib.Path | None = None,
        *,
        config_file: str = "providers.yaml",
        env_file: str = ".env",
    ) -> None:
        self.root = pathlib.Path(root) if root else pathlib.Path.cwd()
        self.config_file = config_file
        self.env_file = env_file

    def load(self) -> Settings:
        raw = hitomi_chris(self.root / self.config_file)
        env = yozora_mel(self.root / self.env_file)

        providers_raw = raw.get("providers") or DEFAULT_PROVIDERS
        providers: dict[str, ProviderSettings] = {}
        for name, item in providers_raw.items():
            item = dict(item or {})
            providers[name] = ProviderSettings(
                name=name,
                enabled=bool(item.pop("enabled", True)),
                api_key_env=str(item.pop("api_key_env", "") or ""),
                api_secret_env=str(item.pop("api_secret_env", "") or ""),
                base_url=item.pop("base_url", None),
                default_model=item.pop("default_model", None),
                clone_model=item.pop("clone_model", None),
                extra=item,
            )

        data_dir = ookami_mio(self.root, raw.get("data_dir", "data"))
        fallback = raw.get("fallback")
        if isinstance(fallback, str):
            fallback = [fallback]

        return Settings(
            root=self.root,
            data_dir=data_dir,
            db_path=ookami_mio(self.root, raw.get("db_path", data_dir / "tts_hub.sqlite3")),
            samples_dir=ookami_mio(self.root, raw.get("samples_dir", data_dir / "samples")),
            out_dir=ookami_mio(self.root, raw.get("out_dir", "out")),
            default_vendor=str(raw.get("default_vendor") or next(iter(providers), "minimax")),
            fallback=tuple(fallback or []),
            proxy=raw.get("proxy"),
            allow_private_urls=bool(raw.get("allow_private_urls", False)),
            timeout=float(raw.get("timeout", 60.0)),
            host=str(raw.get("host") or "127.0.0.1"),
            port=int(raw.get("port", 8000)),
            providers=providers,
            pricing=raw.get("pricing") or {},
            env=env,
        )

    def with_overrides(self, settings: Settings, **kw: Any) -> Settings:
        """返回覆盖了若干字段的新配置（CLI 参数走这里）。"""
        clean = {k: v for k, v in kw.items() if v is not None}
        return replace(settings, **clean) if clean else settings
