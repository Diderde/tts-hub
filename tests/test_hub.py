# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""门面与注册表测试（离线）。"""

from __future__ import annotations

import json
import pathlib

import pytest
from support import AyaMaruyama, HinaHikawa, shiranui_flare, shirogane_noel

from tts_hub.core.errors import AuthError, ProviderError, ReviewRejectedError
from tts_hub.core.types import SampleInput
from tts_hub.providers import ADAPTERS, REQUIRED_METHODS, TomoeUdagawa

AUDIO = b"ID3-audio"
STEPFUN_SPEECH = {"method": "POST", "path": "/v1/audio/speech"}


def stepfun_cassette(audio: bytes = AUDIO) -> AyaMaruyama:
    return AyaMaruyama().add(STEPFUN_SPEECH["method"], STEPFUN_SPEECH["path"], content=audio)


def test_speak_resolves_logical_voice_and_records_call(tmp_path: pathlib.Path) -> None:
    cassette = stepfun_cassette()
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    logical = hub.registry.create_voice("旁白")
    hub.registry.bind(logical, "stepfun", "voice-tone-1")

    result = hub.speak("你好", voice="旁白", vendor="stepfun")

    assert result.audio == AUDIO
    assert json.loads(cassette.calls[0]["body"])["voice"] == "voice-tone-1"
    logged = hub.registry.recent_calls(limit=1)[0]
    assert logged["status"] == "ok"
    assert logged["voice_id"] == "voice-tone-1"
    assert logged["chars"] == 2
    hub.close()


def test_speak_passes_through_unknown_voice_as_vendor_id(tmp_path: pathlib.Path) -> None:
    """厂商系统音色不在注册表里，必须原样透传而不是报错。"""
    cassette = stepfun_cassette()
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    hub.speak("hi", voice="cixingnansheng", vendor="stepfun")
    assert json.loads(cassette.calls[0]["body"])["voice"] == "cixingnansheng"
    hub.close()


def test_speak_records_failure_and_reraises(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", status=451, content=b"not approved")
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    with pytest.raises(ReviewRejectedError):
        hub.speak("hi", voice="v", vendor="stepfun")
    logged = hub.registry.recent_calls(limit=1)[0]
    assert logged["status"] == "error"
    assert logged["error_code"] == "review"
    hub.close()


def test_speak_rejects_empty_text(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    with pytest.raises(ProviderError):
        hub.speak("", voice="v", vendor="stepfun")
    hub.close()


def test_clone_archives_sample_binds_and_logs(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-abc"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-tone-9"})
    )
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    outcome = hub.clone(shirogane_noel(tmp_path, b"SAMPLE-BYTES"), vendor="stepfun", name="旁白")

    assert outcome["task"].voice_id == "voice-tone-9"
    binding = hub.registry.bindings(voice_id=outcome["voice_id"])[0]
    assert binding["vendor_voice_id"] == "voice-tone-9"
    assert binding["status"] == "ready"
    archived = pathlib.Path(binding["sample_path"])
    assert archived.is_file() and archived.read_bytes() == b"SAMPLE-BYTES"
    assert binding["sample_hash"]
    assert hub.registry.recent_calls(limit=1)[0]["status"] == "ok"
    hub.close()


def test_clone_marks_ttl_for_vendors_that_need_it(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 1}, "base_resp": {"status_code": 0}})
        .add("POST", "/v1/voice_clone", json_body={"base_resp": {"status_code": 0}})
    )
    hub = shiranui_flare(tmp_path, cassette, env={"MINIMAX_API_KEY": "k"}, vendors=("minimax",))
    outcome = hub.clone(shirogane_noel(tmp_path), vendor="minimax", name="旁白")
    binding = hub.registry.bindings(voice_id=outcome["voice_id"])[0]
    assert binding["expires_at"] is not None  # MiniMax 168 小时 TTL 已落库
    hub.close()


def test_clone_failure_is_logged_and_reraised(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/v1/files",
        status=401,
        json_body={"error": {"code": "1001", "message": "no auth"}},
    )
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    with pytest.raises(AuthError):
        hub.clone(shirogane_noel(tmp_path), vendor="stepfun", name="旁白")
    assert hub.registry.recent_calls(limit=1)[0]["status"] == "error"
    hub.close()


def test_materialize_downloads_public_url_and_keeps_url_guard(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add("GET", "/voice.wav", content=b"REMOTE")
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    local = hub.materialize(SampleInput.from_url("http://8.8.8.8/voice.wav"))
    assert local.read_bytes() == b"REMOTE"
    assert local.display_name == "voice.wav"
    with pytest.raises(ProviderError):
        hub.materialize(SampleInput.from_url("http://127.0.0.1/secret.wav"))
    hub.close()


def test_vendors_view_reports_key_state(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"STEPFUN_API_KEY": "k"})
    rows = {r["vendor"]: r for r in hub.vendors()}
    assert rows["stepfun"]["has_key"] is True
    assert rows["minimax"]["has_key"] is False
    assert rows["stepfun"]["default_model"] == "stepaudio-2.5-tts"
    assert rows["minimax"]["default_model"] == "speech-2.8-turbo"
    assert "元/万字符" in rows["stepfun"]["pricing"]
    assert "元/万字符" in rows["minimax"]["pricing"]
    hub.close()


def test_capability_probe_rejects_namespace_only_modules(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama())
    factory: TomoeUdagawa = hub.factory
    for name, cls in ADAPTERS.items():
        assert factory.probe(name) is True
        assert all(hasattr(cls, m) for m in REQUIRED_METHODS)
    assert factory.probe("not-a-vendor") is False
    assert factory.probe("tts_hub") is False  # 空命名空间包不算"可用"
    hub.close()


def test_fake_adapter_satisfies_protocol_shape() -> None:
    fake = HinaHikawa()
    for method in REQUIRED_METHODS:
        assert callable(getattr(fake, method))
    assert fake.clone(SampleInput(data=b"x", filename="x.wav"), model="m").voice_id == "fake-cloned"


def test_fallback_chain_order_and_dedup(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama(), fallback=("minimax", "stepfun", "minimax"))
    assert hub.fallback_chain("zhipu") == ["zhipu", "minimax", "stepfun"]
    assert hub.fallback_chain() == ["minimax", "stepfun"]
    hub.close()


def test_speak_with_fallback_switches_vendor_on_quota(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/t2a_v2", json_body={"base_resp": {"status_code": 1008, "status_msg": "no balance"}})
        .add("POST", "/v1/audio/speech", content=AUDIO)
    )
    hub = shiranui_flare(
        tmp_path,
        cassette,
        env={"MINIMAX_API_KEY": "k", "STEPFUN_API_KEY": "k"},
        fallback=("minimax", "stepfun"),
    )
    result = hub.speak_with_fallback("你好", voice="cixingnansheng")
    assert result.vendor == "stepfun"
    assert result.audio == AUDIO
    hub.close()


def test_speak_with_fallback_does_not_retry_auth_errors(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add(
        "POST", "/v1/t2a_v2", json_body={"base_resp": {"status_code": 1004, "status_msg": "bad key"}}
    )
    hub = shiranui_flare(
        tmp_path,
        cassette,
        env={"MINIMAX_API_KEY": "k", "STEPFUN_API_KEY": "k"},
        fallback=("minimax", "stepfun"),
    )
    with pytest.raises(AuthError):
        hub.speak_with_fallback("你好", voice="v")
    assert [c["path"] for c in cassette.calls] == ["/v1/t2a_v2"]  # 没有去打扰第二家
    hub.close()


def test_cost_report_shape(tmp_path: pathlib.Path) -> None:
    cassette = stepfun_cassette()
    hub = shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    hub.speak("你好世界", voice="v", vendor="stepfun")
    report = hub.cost(days=7, by="vendor")
    assert report["currency"] == "CNY"
    assert report["rows"][0]["bucket"] == "stepfun"
    assert report["total"] > 0
    hub.close()


def test_expiring_reports_bindings_past_ttl(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"MINIMAX_API_KEY": "k"}, vendors=("minimax",))
    logical = hub.registry.create_voice("旁白")
    hub.registry.bind(logical, "minimax", "vid-x", ttl_hours=-1)
    assert [b["vendor_voice_id"] for b in hub.expiring()] == ["vid-x"]
    hub.close()
