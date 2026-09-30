# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""请求签名：腾讯 TC3 与讯飞 URL 签名（纯函数，可离线对拍）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import urlencode

__all__ = [
    "amane_kanata",
    "kiryu_coco",
    "tokoyami_towa",
    "tsunomaki_watame",
    "urara_shiraishi",
]


def tsunomaki_watame(key: str | bytes, message: str | bytes) -> bytes:
    """HMAC-SHA256 原始摘要（签名算法的最小公共件）。"""
    raw_key = key.encode("utf-8") if isinstance(key, str) else key
    raw_msg = message.encode("utf-8") if isinstance(message, str) else message
    return hmac.new(raw_key, raw_msg, hashlib.sha256).digest()


def tokoyami_towa(moment: datetime | None = None) -> str:
    """RFC1123 GMT 时间串，形如 ``Fri, 05 May 2023 10:43:39 GMT``。

    ⚠️ 讯飞官方 Python demo 用的是 `format_date_time(mktime(datetime.now().timetuple()))`，
    即把**本地墙上时间**当 GMT 渲染。若服务端按真正的 GMT 校验，那份 demo 在 UTC+8
    会差 8 小时。这里按字面语义取真 UTC（协议写的就是 GMT），并在适配器里留了
    时钟偏移配置；真机若报 403 时钟偏移，优先怀疑这一处而不是签名串拼错。
    """
    stamp = moment or datetime.now(UTC)
    return format_datetime(stamp.astimezone(UTC), usegmt=True)


def urara_shiraishi(secret: str, signature_origin: str) -> str:
    """把 ``signature_origin`` 签成 base64 摘要（讯飞用）。"""
    return base64.b64encode(tsunomaki_watame(secret, signature_origin)).decode("ascii")


_AUTH_KEY_FIELD = "api_key"


def kiryu_coco(
    *,
    host: str,
    path: str,
    api_key: str,
    api_secret: str,
    scheme: str = "wss",
    date: str | None = None,
    method: str = "GET",
) -> str:
    """科大讯飞的签名 URL（合成走 WebSocket，握手前先算好这个地址）。

    三段顺序固定为 host → date → request-line，`\\n` 连接且**末尾无换行**；
    ``headers`` 是字面量 ``"host date request-line"``，不是这三个参数的值。
    """
    stamp = date or tokoyami_towa()
    signature_origin = f"host: {host}\ndate: {stamp}\n{method} {path} HTTP/1.1"
    signature = urara_shiraishi(api_secret, signature_origin)
    authorization_origin = (
        f"{_AUTH_KEY_FIELD}=\"{api_key}\", algorithm=\"hmac-sha256\", "
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("ascii")
    query = urlencode({"authorization": authorization, "date": stamp, "host": host})
    return f"{scheme}://{host}{path}?{query}"


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def amane_kanata(
    *,
    secret_id: str,
    secret_key: str,
    service: str,
    action: str,
    version: str,
    region: str,
    host: str,
    payload: bytes | Mapping[str, Any] | None = None,
    timestamp: int | None = None,
    token: str | None = None,
    content_type: str = "application/json; charset=utf-8",
) -> dict[str, str]:
    """腾讯云 TC3-HMAC-SHA256 签名，返回应当附上的请求头。

    ``payload`` 必须与真正发出去的请求体**逐字节一致**——所以这里只接受已序列化
    的 bytes，或者 ``None``（表示空体）。adapter 用 ``encode_body()`` 序列化一次后
    同时用于签名与发送。

    日期派生密钥是四段 HMAC：``TC3+SecretKey`` → date → service → ``tc3_request``。
    """
    stamp = int(timestamp if timestamp is not None else datetime.now(UTC).timestamp())
    date = datetime.fromtimestamp(stamp, tz=UTC).strftime("%Y-%m-%d")
    if payload is None:
        body_bytes = b""
    elif isinstance(payload, bytes):
        body_bytes = payload
    else:
        import json

        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, _sha256_hex(body_bytes)]
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        ["TC3-HMAC-SHA256", str(stamp), credential_scope, _sha256_hex(canonical_request.encode())]
    )

    secret_date = tsunomaki_watame(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = tsunomaki_watame(secret_date, service)
    secret_signing = tsunomaki_watame(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(stamp),
        "X-TC-Region": region,
    }
    if token:
        headers["X-TC-Token"] = token
    return headers
