# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""适配器基类：鉴权头、请求封装、业务码分类、耗时统计。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, ClassVar, NoReturn

import httpx

from ..core.errors import ProviderError, TTSHubError
from ..core.net import MocaAoba, hoshimachi_suisei, natsuiro_matsuri

__all__ = ["KasumiToyama"]


class KasumiToyama:
    """厂商适配器基类。"""

    name: str = ""
    base_url: str = ""
    CODE_KINDS: ClassVar[Mapping[str, type[TTSHubError]]] = {}
    default_model: str | None = None
    clone_model: str | None = None
    clone_requires_url: bool = False
    timeout: float = 60.0
    needs_secret: bool = False
    TOKEN_SKEW: float = 60.0

    def __init__(
        self,
        api_key: str,
        *,
        api_secret: str | None = None,
        http: MocaAoba | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
        allow_private: bool = False,
        owned_http: bool | None = None,
        require_key: bool = True,
    ) -> None:
        from ..core.errors import AuthError

        if require_key and (not api_key or not api_key.strip()):
            raise AuthError(f"{self.name or type(self).__name__} 缺少 API 密钥", vendor=self.name)
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip() or None
        if require_key and self.needs_secret and not self.api_secret:
            raise AuthError(
                f"{self.name or type(self).__name__} 需要 API Key 与 Secret Key 两把密钥",
                vendor=self.name,
            )
        self._token: str | None = None
        self._token_deadline = 0.0
        self.extra: dict[str, Any] = {}
        if base_url:
            self.base_url = base_url
        if timeout:
            self.timeout = timeout
        self._owns_http = owned_http if owned_http is not None else http is None
        self.http = http or MocaAoba(
            timeout=self.timeout,
            proxy=proxy,
            allow_private=allow_private,
            secrets=[s for s in (self.api_key, self.api_secret) if s],
        )


    def configure(self, extra: Mapping[str, Any] | None) -> KasumiToyama:
        """接收 providers.yaml 里的厂商专属配置。

        华为要 ``project_id``、火山要 ``app_id``/``resource_id``、讯飞要 ``app_id``——
        这些不是密钥也不通用，统一走这里而不是往构造函数上加一串具名参数。
        """
        self.extra = {str(k): v for k, v in (extra or {}).items()}
        return self

    def setting(self, name: str, default: Any = None) -> Any:
        """读一条厂商专属配置。"""
        return self.extra.get(name, default)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> KasumiToyama:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def headers(self, *, json_body: bool = True) -> dict[str, str]:
        head = self.auth_headers()
        if json_body:
            head["Content-Type"] = "application/json"
        return head

    def auth_headers(self) -> dict[str, str]:
        """鉴权头。默认就是 ``Bearer <api_key>``；需要换 token 的厂商覆写本方法。"""
        return {"Authorization": f"Bearer {self.api_key}"}

    def cached_access_token(self, fetch: Callable[[], tuple[str, float]]) -> str:
        """带过期时间的 token 缓存。

        ``fetch`` 返回 ``(token, 有效秒数)``。命中缓存就不发请求——百度这类
        "先换 token 再调用"的厂商 token 通常有几十天有效期，每次都换既慢又容易撞限流。
        缓存只活在进程内：进程重启后重换一次，比把密钥派生出的凭据落盘安全。
        """
        now = time.monotonic()
        if self._token and now < self._token_deadline:
            return self._token
        token, ttl = fetch()
        if not token:
            raise ProviderError("换取 access_token 失败：响应里没有 token", vendor=self.name)
        self._token = token
        self._token_deadline = now + max(0.0, float(ttl) - self.TOKEN_SKEW)
        return token

    def access_token(self) -> str:
        """取当前可用的 token（带缓存）。

        百度与华为都需要"换一次、缓存、过期再换"，逻辑完全一样，因此放在基类；
        子类只需实现 :meth:`fetch_token`。
        """
        return self.cached_access_token(self.fetch_token)

    def fetch_token(self) -> tuple[str, float]:
        """换取 token 并返回 ``(token, 有效秒数)``；不需要 token 的厂商不会走到这里。"""
        raise ProviderError(f"{self.name or type(self).__name__} 未实现 token 换取", vendor=self.name)

    def business_error(self, payload: Mapping[str, Any]) -> tuple[str | None, str] | None:
        """HTTP 200 但体内含业务错误时返回 ``(code, message)``；默认无此类字段。"""
        return None

    def classify(self, status: int, payload: Any, *, raw: Any = None) -> NoReturn:
        """把厂商错误响应映射成四类归一错误，并抛出。

        先查厂商业务码表，查不到再按 HTTP 状态码兜底分类。两条路都不会静默吞错。
        """
        code, message = hoshimachi_suisei(payload, status)
        kind = self.CODE_KINDS.get(code or "")
        if kind is None:
            raise natsuiro_matsuri(status, code, message, vendor=self.name, raw=raw)
        raise kind(message or f"HTTP {status}", vendor=self.name, code=code, status=status, raw=raw)

    def encode_body(self, body: Any) -> bytes | None:
        """把请求体序列化成**将要发出去的那串字节**。

        签名类厂商（腾讯 TC3、华为 AK/SK）要对**请求体的精确字节**做哈希，
        所以序列化只做一次、哈希与发送共用同一份，不能一边 json= 一边另算一遍。
        """
        if body is None:
            return None
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def signed_headers(self, method: str, url: str, payload: bytes | None) -> dict[str, str]:
        """需要请求签名的厂商覆写本方法，返回要附加（或覆盖）的请求头。

        默认不签名——P1~P3 六家都是 Bearer 或 query token。
        """
        return {}

    def send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        expect: Iterable[int] = (200, 201),
    ) -> httpx.Response:
        """发一次请求并做状态/业务双层错误检查。"""
        expect_set = set(expect)
        url = self.url(path)
        payload = self.encode_body(body) if (files is None and data is None) else None
        headers = self.headers(json_body=payload is not None)
        headers.update(self.signed_headers(method, url, payload))
        try:
            response = self.http.raw_request(
                method,
                url,
                headers=headers,
                content=payload,
                params=params,
                data=data,
                files=files,
            )
        except TTSHubError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(self.http.redact(f"网络请求失败：{exc}"), vendor=self.name) from exc
        if response.status_code not in expect_set:
            self.classify(response.status_code, response.content, raw=response.content[:2000])
        return response

    def send_json(self, method: str, path: str, **kw: Any) -> Mapping[str, Any]:
        """发请求并返回 JSON 对象；HTTP 200 的体内业务错误也在此拦下。"""
        response = self.send(method, path, **kw)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("厂商返回的不是合法 JSON", vendor=self.name, status=response.status_code) from exc
        if not isinstance(payload, dict):
            raise ProviderError("厂商返回的 JSON 顶层不是对象", vendor=self.name, raw=payload)
        biz = self.business_error(payload)
        if biz is not None:
            code, message = biz
            wrapped = {"base_resp": {"status_code": code, "status_msg": message}}
            self.classify(response.status_code, wrapped, raw=payload)
        return payload

    def send_bytes(self, method: str, path: str, **kw: Any) -> bytes:
        return self.send(method, path, **kw).content


    def sample_bytes(self, sample: Any) -> tuple[str, str, bytes]:
        """把 ``SampleInput`` 归一成 ``(文件名, MIME, 字节)``。

        URL 形式在此经出站安全校验后下载（§9：用户提交的音频 URL 必须先校验）。
        """
        if sample.url:
            raw = self.http.download(sample.url)
            return sample.display_name, sample.mime or "application/octet-stream", raw
        raw = sample.read_bytes()
        name = sample.display_name
        mime = sample.mime or shirakami_fubuki(name)
        return name, mime, raw


def shirakami_fubuki(filename: str) -> str:
    """按扩展名猜 MIME（只覆盖厂商接受的音频格式，其余退回八位字节流）。"""
    tail = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
        "pcm": "audio/L16",
    }.get(tail, "application/octet-stream")


def now_ms() -> float:
    """高精度时间戳（毫秒），用于耗时统计。"""
    return time.perf_counter() * 1000.0
