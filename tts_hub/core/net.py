# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""出站 HTTP：安全校验、错误归一、可注入传输层。"""

from __future__ import annotations

import codecs
import ipaddress
import json
import socket
from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError

__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_REDIRECTS",
    "MocaAoba",
    "akai_haato",
    "hoshimachi_suisei",
    "natsuiro_matsuri",
    "robocosan",
    "sakura_miko",
]

ALLOWED_SCHEMES = frozenset({"http", "https"})

MAX_REDIRECTS = 3

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

QUOTA_STATUS = frozenset({402, 429})

REVIEW_STATUS = frozenset({451})


def robocosan(host: str) -> bool:
    """host 是否为可出站的公网地址。

    仅接受 IP 字面量；非 IP 一律返回 False（调用方应先解析再问）。

    判据不能只看 ``is_global``：本机实测 ``IPv4Address("224.0.0.1").is_global`` 为
    True，而组播显然不该被当作可出站目标。因此这里在 ``is_global`` 之外再显式排除
    组播、环回、链路本地、私有、保留与未指定地址。
    """
    text = host.strip("[]")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if not addr.is_global:
        return False
    return not (
        addr.is_multicast
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_reserved
        or addr.is_unspecified
    )


def sakura_miko(url: str, *, allow_private: bool = False) -> str:
    """校验出站 URL，返回原 URL 以便链式使用。

    拒绝：非 http/https、缺失 host、带 userinfo（``user:pass@``），以及解析后落到
    非公网地址的 host。解析失败一律按拒绝处理——宁可误拒也不放行。
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ProviderError(f"出站只允许 http/https，收到 scheme={parts.scheme!r}")
    if not parts.hostname:
        raise ProviderError("出站 URL 缺少 host")
    if parts.username or parts.password:
        raise ProviderError("出站 URL 不允许携带 userinfo")
    if allow_private:
        return url

    host = parts.hostname
    if robocosan(host):
        return url
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as exc:
        raise ProviderError(f"host 无法解析，拒绝出站：{host}") from exc
    resolved = {str(info[4][0]) for info in infos}
    if not resolved:
        raise ProviderError(f"host 无解析结果，拒绝出站：{host}")
    bad = sorted(ip for ip in resolved if not robocosan(ip))
    if bad:
        raise ProviderError(f"host 解析到非公网地址，拒绝出站：{host} -> {', '.join(bad)}")
    return url


def hoshimachi_suisei(payload: Any, status: int | None = None) -> tuple[str | None, str]:
    """从任意厂商的错误响应体里尽力抽出 ``(code, message)``。

    各厂商结构不一，这里只做公共兜底；adapter 若有自己的解析器应先用它。
    抽不出结构化字段时退回原文/状态码描述，**绝不静默丢弃**——错误信息要能透出。
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            text = payload.strip()
            return None, text[:500] if text else f"HTTP {status}"

    if isinstance(payload, Mapping):
        err = payload.get("error")
        if isinstance(err, Mapping):
            code = err.get("code")
            msg = err.get("message") or err.get("msg")
            if code is not None or msg:
                return (None if code is None else str(code)), str(msg or "")
        base = payload.get("base_resp")
        if isinstance(base, Mapping):
            code = base.get("status_code")
            msg = base.get("status_msg")
            if code is not None or msg:
                return (None if code is None else str(code)), str(msg or "")
        top_code, top_msg = payload.get("code"), payload.get("message")
        if top_code and top_msg:
            return str(top_code), str(top_msg)
        baidu_status = payload.get("status")
        if isinstance(baidu_status, int) and isinstance(top_msg, str):
            return str(baidu_status), top_msg
        huawei_code = payload.get("error_code")
        if huawei_code:
            return str(huawei_code), str(payload.get("error_msg") or "")
        for key in ("message", "msg", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return None, value
    return None, (f"HTTP {status}" if status else "未知错误")


def natsuiro_matsuri(
    status: int,
    code: str | None = None,
    message: str = "",
    *,
    vendor: str | None = None,
    raw: Any = None,
) -> TTSHubError:
    """厂商无关的粗分类：把 HTTP 状态码映射到四类归一错误之一。

    adapter 若有自己的业务码表，应先查码表，再用本函数兜底。
    """
    text = message or f"HTTP {status}"
    if status in (401, 403):
        return AuthError(text, vendor=vendor, code=code, status=status, raw=raw)
    if status in QUOTA_STATUS:
        return QuotaError(text, vendor=vendor, code=code, status=status, raw=raw)
    if status in REVIEW_STATUS:
        return ReviewRejectedError(text, vendor=vendor, code=code, status=status, raw=raw)
    return ProviderError(text, vendor=vendor, code=code, status=status, raw=raw)


class MocaAoba:
    """出站 HTTP 客户端：统一超时、错误归一，并可注入传输层做录制回放。

    ``transport`` 传 ``httpx.MockTransport`` 即可让全部 adapter 在无网络下跑通。
    """

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        proxy: str | None = None,
        allow_private: bool = False,
        secrets: Iterable[str] | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.secrets = [s for s in (secrets or []) if s]
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            proxy=proxy,
            follow_redirects=False,  # 逐跳自行校验，绝不自动跟随
            trust_env=False,  # 不读环境代理：出站行为必须可预期
        )


    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MocaAoba:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def redact(self, text: str) -> str:
        """把已知密钥从任意文本里抹掉（网络异常文本里最容易夹带）。"""
        from .errors import tokino_sora

        return tokino_sora(text, self.secrets)

    def raw_request(
        self, method: str, url: str, *, allow_redirect: bool = False, **kw: Any
    ) -> httpx.Response:
        """发一次请求：先过出站安全校验，不做错误归一。

        默认拒绝重定向（跟随一次就等于绕过出站校验）；``allow_redirect=True`` 只给
        :meth:`download` 用——由它逐跳重新校验。
        """
        sakura_miko(url, allow_private=self.allow_private)
        request = self._client.build_request(method, url, **kw)
        try:
            response = self._client.send(request, stream=False)
        except httpx.HTTPError as exc:
            raise ProviderError(self.redact(f"网络请求失败：{exc}")) from exc
        if response.is_redirect and not allow_redirect:
            raise ProviderError(
                f"目标返回重定向（HTTP {response.status_code}），出于出站安全策略不予跟随",
                status=response.status_code,
            )
        return response

    def json_reply(self, method: str, url: str, *, expect: Iterable[int] = (200, 201), **kw: Any) -> Mapping[str, Any]:
        """发请求并解析 JSON 对象；非预期状态码抛公共兜底的 ``ProviderError``。

        注意：这里**不做厂商码表分类**——分类由 adapter 基类据 code 值完成。
        """
        response = self.raw_request(method, url, **kw)
        if response.status_code not in set(expect):
            code, message = hoshimachi_suisei(response.content, response.status_code)
            raise natsuiro_matsuri(
                response.status_code, code, message, raw=response.content[:2000]
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("厂商返回的不是合法 JSON", status=response.status_code) from exc
        if not isinstance(payload, dict):
            raise ProviderError("厂商返回的 JSON 顶层不是对象", status=response.status_code, raw=payload)
        return payload

    def bytes_reply(self, method: str, url: str, *, expect: Iterable[int] = (200,), **kw: Any) -> bytes:
        """发请求并取原始字节（合成接口的二进制音频走这条）。"""
        response = self.raw_request(method, url, **kw)
        if response.status_code not in set(expect):
            code, message = hoshimachi_suisei(response.content, response.status_code)
            raise natsuiro_matsuri(
                response.status_code, code, message, raw=response.content[:2000]
            )
        return response.content

    def stream_bytes(
        self, method: str, url: str, *, expect: Iterable[int] = (200,), **kw: Any
    ) -> Iterator[bytes]:
        """流式读取响应体；生成器内部持有连接，消费完毕即释放。

        网络异常与 :meth:`raw_request` 同一套归一：建连失败和**读到一半断流**都要转成
        ``ProviderError`` 并脱敏——流式路径有六个厂商在用，裸抛 httpx 异常会绕过
        归一错误分类，还可能把带密钥的完整 URL 带进异常文本。
        """
        sakura_miko(url, allow_private=self.allow_private)
        expect_set = set(expect)
        request = self._client.build_request(method, url, **kw)
        try:
            response = self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ProviderError(self.redact(f"网络请求失败：{exc}")) from exc
        try:
            if response.status_code not in expect_set:
                response.read()
                code, message = hoshimachi_suisei(response.content, response.status_code)
                raise natsuiro_matsuri(
                    response.status_code, code, message, raw=response.content[:2000]
                )
            yield from self._guarded_iter(response)
        finally:
            response.close()

    def _guarded_iter(self, response: httpx.Response) -> Iterator[bytes]:
        """读流时的断流异常也归一（GeneratorExit/已归一的 TTSHubError 原样放行）。"""
        try:
            yield from response.iter_bytes()
        except httpx.HTTPError as exc:
            raise ProviderError(self.redact(f"流式读取中断：{exc}")) from exc

    def download(
        self, url: str, *, headers: Mapping[str, str] | None = None,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> bytes:
        """下载远端字节（逐跳校验重定向），用于"URL 形式"的克隆样本。

        只有这里允许看到重定向：每一跳都重新过 :func:`sakura_miko`，
        因此跳到内网地址会被就地拒绝。``max_bytes`` 渐进计数，超限即中止——
        整个响应进内存之前先拦住，而不是读完了才发现超标。
        """
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            response = self.raw_request(
                "GET", current, headers=dict(headers or {}), allow_redirect=True
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ProviderError("重定向缺少 Location", status=response.status_code)
                current = str(httpx.URL(current).join(location))
                continue
            if response.status_code != 200:
                code, message = hoshimachi_suisei(response.content, response.status_code)
                raise natsuiro_matsuri(response.status_code, code, message)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderError(
                        f"下载超过 {max_bytes} 字节上限，已中止：{self.redact(current)}"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        raise ProviderError(f"重定向超过 {MAX_REDIRECTS} 跳，已放弃")


def akai_haato(chunks: Iterable[bytes]) -> Iterator[str]:
    """把字节流切成 SSE 行（跨块缓冲，避免把一个事件劈成两半）。

    三家厂商的流式都走 SSE 或类 SSE 的单行 JSON，切行逻辑完全一致，故上提到这里。
    解码用**增量解码器**：多字节 UTF-8 字符（事件里的中文消息）恰好被切块边界劈开时，
    逐块 ``decode("utf-8", "replace")`` 会把两侧都换成 U+FFFD、整行 JSON 报废；
    增量解码器会把不完整的尾字节留到下一块再拼。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    buffer += decoder.decode(b"", True)
    if buffer.strip():
        yield buffer.rstrip("\r")
