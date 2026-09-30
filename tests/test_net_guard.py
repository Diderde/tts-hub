# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""出站安全校验与错误归一测试。"""

from __future__ import annotations

import httpx
import pytest
from support import AyaMaruyama, azki

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.net import (
    MocaAoba,
    akai_haato,
    hoshimachi_suisei,
    natsuiro_matsuri,
    robocosan,
    sakura_miko,
)

PUBLIC_HOSTS = ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"]
PRIVATE_HOSTS = [
    "127.0.0.1",
    "10.0.0.1",
    "192.168.1.1",
    "172.16.0.1",
    "169.254.1.1",
    "0.0.0.0",
    "224.0.0.1",
    "::1",
    "fe80::1",
    "fd00::1",
    "::ffff:127.0.0.1",
    "not-an-ip",
    "",
]


@pytest.mark.parametrize("host", PUBLIC_HOSTS)
def test_robocosan_accepts_global_addresses(host: str) -> None:
    assert robocosan(host) is True


@pytest.mark.parametrize("host", PRIVATE_HOSTS)
def test_robocosan_rejects_non_global_addresses(host: str) -> None:
    assert robocosan(host) is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://8.8.8.8/x.mp3",
        "http://127.0.0.1/x.mp3",
        "http://[::1]/x.mp3",
        "http://10.1.2.3/x.mp3",
        "http://localhost/x.mp3",
        "http://user:secret@8.8.8.8/x.mp3",
        "http:///no-host",
    ],
)
def test_outbound_guard_blocks_unsafe_urls(url: str) -> None:
    with pytest.raises(ProviderError):
        sakura_miko(url)


def test_outbound_guard_allows_public_and_honours_override() -> None:
    assert sakura_miko("https://8.8.8.8/audio.mp3") == "https://8.8.8.8/audio.mp3"
    assert sakura_miko("http://127.0.0.1/x", allow_private=True) == "http://127.0.0.1/x"


def test_redirect_is_not_followed_automatically() -> None:
    cassette = AyaMaruyama().add("GET", "/move", status=302, headers={"location": "http://127.0.0.1/internal"})
    client = cassette.client()
    with pytest.raises(ProviderError) as excinfo:
        client.raw_request("GET", "http://8.8.8.8/move")
    assert "重定向" in str(excinfo.value)
    client.close()


def test_download_follows_public_redirect_chain() -> None:
    cassette = (
        AyaMaruyama()
        .add("GET", "/a", status=302, headers={"location": "http://8.8.8.8/b"})
        .add("GET", "/b", content=b"payload")
    )
    client = cassette.client()
    assert client.download("http://8.8.8.8/a") == b"payload"
    assert [c["path"] for c in cassette.calls] == ["/a", "/b"]
    client.close()


def test_download_rejects_redirect_to_private_host() -> None:
    cassette = AyaMaruyama().add("GET", "/a", status=302, headers={"location": "http://127.0.0.1/b"})
    client = cassette.client()
    with pytest.raises(ProviderError):
        client.download("http://8.8.8.8/a")
    client.close()


def test_hoshimachi_suisei_reads_zhipu_and_minimax_shapes() -> None:
    assert hoshimachi_suisei({"error": {"code": "1214", "message": "音色id不存在"}}) == ("1214", "音色id不存在")
    assert hoshimachi_suisei({"base_resp": {"status_code": 1004, "status_msg": "login fail"}}) == ("1004", "login fail")
    assert hoshimachi_suisei(b"<html>gateway</html>", 502)[1].startswith("<html>")
    assert hoshimachi_suisei("", 500) == (None, "HTTP 500")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, AuthError),
        (402, QuotaError),
        (429, QuotaError),
        (451, ReviewRejectedError),
        (500, ProviderError),
        (400, ProviderError),
    ],
)
def test_status_maps_to_normalized_error(status: int, expected: type[TTSHubError]) -> None:
    err = natsuiro_matsuri(status, "9", "boom", vendor="v")
    assert isinstance(err, expected)
    assert err.vendor == "v"
    assert err.status == status


def test_akai_haato_reassembles_split_events() -> None:
    chunks = [b'data: {"a":', b'1}\ndata: {"b":2', b'}\ndata: [DONE]\n']
    assert list(akai_haato(chunks)) == ['data: {"a":1}', 'data: {"b":2}', "data: [DONE]"]


def test_akai_haato_emits_trailing_line_without_newline() -> None:
    assert list(akai_haato([b"tail-no-newline"])) == ["tail-no-newline"]


def httpx_failure(message: str) -> httpx.MockTransport:
    """造一个必定抛网络异常的传输层，异常文本里夹着带密钥的 URL。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(message, request=request)

    return httpx.MockTransport(handler)


def test_client_redacts_registered_secrets() -> None:
    client = MocaAoba(secrets=["SUPER-SECRET-VALUE-0123456789"])
    masked = client.redact("boom https://x/token?client_secret=SUPER-SECRET-VALUE-0123456789")
    assert "SUPER-SECRET-VALUE-0123456789" not in masked
    assert "***" in masked
    client.close()


def test_network_failure_does_not_leak_the_secret() -> None:
    """百度把 client_secret 放在 URL query 上，异常文本必须过脱敏。"""
    from tts_hub.providers.baidu import MisumiUika

    secret = "SUPER-SECRET-VALUE-0123456789"
    client = MocaAoba(
        transport=httpx_failure(f"connect failed: https://x/oauth/2.0/token?client_secret={secret}"),
        secrets=[secret],
    )
    adapter = MisumiUika("baidu-ak", api_secret=secret, http=client)
    with pytest.raises(ProviderError) as excinfo:
        adapter.list_voices()
    message = str(excinfo.value)
    assert secret not in message
    assert "***" in message
    adapter.close()


def test_hub_feeds_configured_secrets_into_the_client(tmp_path) -> None:
    """配置里的密钥要真的交给客户端，否则脱敏名单是空的、脱敏形同虚设。

    刻意**不注入** http：注入会绕过 TTSHub 自建客户端那条生产路径，
    测出来的就不是线上真正会发生的事了。
    """
    from tts_hub.config import ProviderSettings, Settings
    from tts_hub.hub import TTSHub

    secret = "baidu-secret-abcdefghijklmnop"
    settings = Settings(
        root=tmp_path,
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "registry.sqlite3",
        samples_dir=tmp_path / "data" / "samples",
        out_dir=tmp_path / "out",
        providers={
            "baidu": ProviderSettings(
                name="baidu", api_key_env="BAIDU_API_KEY", api_secret_env="BAIDU_SECRET_KEY"
            )
        },
        env={"BAIDU_API_KEY": azki("baidu 测试 key（编造）", "baidu-ak"), "BAIDU_SECRET_KEY": secret},
    )
    hub = TTSHub(settings)
    try:
        assert secret in hub.http.secrets
        assert "baidu-ak" in hub.http.secrets
    finally:
        hub.close()
