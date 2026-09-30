# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""华为云 SIS 适配器测试（离线）。"""

from __future__ import annotations

import base64
import json

import pytest
from support import AyaMaruyama, azki

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, SampleInput, VoiceRef
from tts_hub.providers.huawei import (
    CHAR_LIMIT,
    PRESET_VOICES,
    UmiriYahata,
    _sample_rate_hz,
)

TOKEN_PATH = "/v3/auth/tokens"
PROJECT = "proj-abc123"
VOICES_PATH = f"/v1/{PROJECT}/vcs/voices"


def huawei(cassette: AyaMaruyama, *, project_id: str | None = None) -> UmiriYahata:
    adapter = UmiriYahata(
        "hw-user", api_secret="hw-pass", http=cassette.client()
    )
    adapter.configure({"domain_name": "hw-domain", **({"project_id": project_id} if project_id else {})})
    return adapter


def token_route(cassette: AyaMaruyama, *, token: str = "IAM-TOKEN", project_id: str = PROJECT) -> AyaMaruyama:
    return cassette.add(
        "POST",
        TOKEN_PATH,
        headers={"X-Subject-Token": token},
        json_body={"token": {"expires_at": "2099-01-01T00:00:00.000000Z", "project": {"id": project_id}}},
    )


def _sample() -> SampleInput:
    """内联样本：华为的音频走 base64 内联，不需要落盘，因此不借道 shirogane_noel。"""
    return SampleInput(data=b"RIFF-hw", filename="s.wav")


def test_token_is_read_from_response_header_and_scoped_to_project() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("GET", VOICES_PATH, json_body={"result": {"voices": []}})
    adapter = huawei(cassette)
    assert adapter.list_voices() != []  # 预置音色会补上
    body = json.loads(cassette.calls[0]["body"])
    assert body["auth"]["identity"]["methods"] == ["password"]
    assert body["auth"]["identity"]["password"]["user"]["name"] == "hw-user"
    assert body["auth"]["identity"]["password"]["user"]["domain"]["name"] == "hw-domain"
    assert body["auth"]["scope"]["project"]["name"] == "cn-east-3"
    assert cassette.calls[1]["headers"]["x-auth-token"] == "IAM-TOKEN"
    adapter.close()


def test_project_id_is_extracted_from_the_token_response() -> None:
    cassette = token_route(AyaMaruyama(), project_id="proj-from-token")
    cassette.add("GET", "/v1/proj-from-token/vcs/voices", json_body={"result": {"voices": []}})
    adapter = huawei(cassette)
    adapter.list_voices()
    assert adapter.project_id() == "proj-from-token"
    adapter.close()


def test_configured_project_id_overrides_the_token_response() -> None:
    """配了 project_id 就用配置的，不去猜 token 响应里的那个。

    注意：配了 project_id **不等于**可以不换 token —— 鉴权本身仍需要它。
    这里要验的是"URL 用的是哪个 id"，不是"要不要换 token"。
    """
    cassette = token_route(AyaMaruyama(), project_id="ignored-proj")
    cassette.add("GET", "/v1/configured-proj/vcs/voices", json_body={"result": {"voices": []}})
    adapter = huawei(cassette, project_id="configured-proj")
    adapter.list_voices()
    assert [c["path"] for c in cassette.calls] == [
        TOKEN_PATH,
        "/v1/configured-proj/vcs/voices",
    ]
    adapter.close()


def test_token_is_exchanged_once_and_cached() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("GET", VOICES_PATH, json_body={"result": {"voices": []}})
    adapter = huawei(cassette)
    for _ in range(3):
        adapter.list_voices()
    assert len([c for c in cassette.calls if c["path"] == TOKEN_PATH]) == 1
    adapter.close()


def test_direct_x_auth_token_short_circuits_the_exchange() -> None:
    """不想存密码时可以只给一个现成的项目级 Token。"""
    cassette = AyaMaruyama()
    cassette.add("GET", VOICES_PATH, json_body={"result": {"voices": []}})
    adapter = huawei(cassette, project_id=PROJECT)
    adapter.configure(
        {"project_id": PROJECT, "x_auth_token": azki("华为现成项目级 Token（编造）", "PASTED-TOKEN")}
    )
    adapter.list_voices()
    assert not [c for c in cassette.calls if c["path"] == TOKEN_PATH]
    assert cassette.calls[0]["headers"]["x-auth-token"] == "PASTED-TOKEN"
    adapter.close()


def test_missing_subject_token_is_auth_error() -> None:
    cassette = AyaMaruyama().add(
        "POST", TOKEN_PATH, status=401, json_body={"error": {"code": "APIGW.0301", "message": "bad credentials"}}
    )
    adapter = huawei(cassette)
    with pytest.raises(AuthError) as excinfo:
        adapter.list_voices()
    assert "X-Subject-Token" in str(excinfo.value)
    adapter.close()


def test_missing_project_id_is_reported_actionably() -> None:
    cassette = AyaMaruyama().add(
        "POST", TOKEN_PATH, headers={"X-Subject-Token": "T"}, json_body={"token": {}}
    )
    adapter = huawei(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.list_voices()
    assert "project_id" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    "bad",
    ["1abc", "_abc", "has-dash", "has space", "x" * 21, "中文名", ""],
)
def test_voice_name_is_validated_before_any_request(bad: str) -> None:
    """非法 voice_name 要在本地就被拦住，别浪费一次带凭据的往返。"""
    cassette = AyaMaruyama()
    adapter = huawei(cassette)
    with pytest.raises(ProviderError):
        adapter.clone(_sample(), name=bad)
    assert cassette.calls == []
    adapter.close()


def test_preset_voice_name_clash_is_refused_locally() -> None:
    cassette = AyaMaruyama()
    adapter = huawei(cassette)
    preset = next(iter(PRESET_VOICES))
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(_sample(), name=preset)
    assert "SIS.1227" in str(excinfo.value)
    adapter.close()


def test_clone_registers_synchronously_with_base64_audio() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", VOICES_PATH, json_body={"trace_id": "t-1", "result": {"voice_name": "my_voice"}})
    adapter = huawei(cassette)
    task = adapter.clone(_sample(), name="my_voice")
    assert task.status == STATUS_READY
    assert task.voice_id == "my_voice"  # 没有 voice_id，voice_name 就是主键
    body = json.loads(cassette.calls[-1]["body"])
    assert base64.b64decode(body["data"]) == b"RIFF-hw"
    assert body["config"] == {"voice_name": "my_voice", "language": "chinese"}
    adapter.close()


def test_clone_status_reports_ready_only_when_listed() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("GET", VOICES_PATH, json_body={"result": {"voices": [{"voice_name": "my_voice"}]}})
    adapter = huawei(cassette)
    assert adapter.clone_status("my_voice").status == STATUS_READY
    cassette.add("GET", VOICES_PATH, json_body={"result": {"voices": []}}, once=True)
    assert adapter.clone_status("ghost").status == STATUS_FAILED
    adapter.close()


def test_synthesize_decodes_base64_and_parses_sample_rate() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "POST", f"{VOICES_PATH}/clone", json_body={"result": {"data": base64.b64encode(b"HW-WAV").decode()}}
    )
    adapter = huawei(cassette)
    result = adapter.synthesize("欢迎使用语音云服务。", voice=VoiceRef("my_voice"), sample_rate="16kHz")
    assert result.audio == b"HW-WAV"
    assert result.sample_rate == 16000
    body = json.loads(cassette.calls[-1]["body"])
    assert body["text"] == "欢迎使用语音云服务。"
    assert body["config"]["voice_name"] == "my_voice"
    assert body["config"]["sample_rate"] == "16kHz"
    adapter.close()


@pytest.mark.parametrize(
    ("raw", "expected"), [("8kHz", 8000), ("16kHz", 16000), ("24kHz", 24000), ("24000", 24000), ("junk", None)]
)
def test_sample_rate_parsing(raw: str, expected: int | None) -> None:
    assert _sample_rate_hz(raw) == expected


def test_synthesize_enforces_char_limit() -> None:
    adapter = huawei(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * (CHAR_LIMIT + 1), voice=VoiceRef("v"))
    assert str(CHAR_LIMIT) in str(excinfo.value)
    adapter.close()


def test_stream_is_degraded_with_the_real_reason() -> None:
    adapter = huawei(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("v"), stream=True)
    message = str(excinfo.value)
    assert "WSS" in message and "裸二进制" in message
    adapter.close()


def test_list_voices_merges_cloned_and_presets() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "GET", VOICES_PATH, json_body={"result": {"voices": [{"voice_name": "my_voice", "language": "chinese"}]}}
    )
    adapter = huawei(cassette)
    voices = {v.voice_id: v for v in adapter.list_voices()}
    assert "my_voice" in voices and voices["my_voice"].kind == "cloned"
    assert len([v for v in voices.values() if v.kind == "system"]) == len(PRESET_VOICES)
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("SIS.0101", AuthError),
        ("SIS.0102", AuthError),
        ("SIS.0103", AuthError),
        ("SIS.1202", QuotaError),
        ("SIS.0022", QuotaError),
        ("SIS.1222", ReviewRejectedError),
        ("SIS.1203", ProviderError),
        ("SIS.1204", ProviderError),
        ("SIS.1213", ProviderError),
        ("SIS.9999", ProviderError),
    ],
)
def test_sis_error_codes_map_to_normalized_errors(code: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", TOKEN_PATH, headers={"X-Subject-Token": "T"}, json_body={"token": {"project": {"id": PROJECT}}}
    )
    cassette.add("POST", f"{VOICES_PATH}/clone", status=400, json_body={"error_code": code, "error_msg": "boom"})
    adapter = huawei(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("v"))
    assert excinfo.value.code == code
    assert excinfo.value.vendor == "huawei"
    adapter.close()
