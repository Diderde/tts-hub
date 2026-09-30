# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""MiniMax 适配器测试（离线）。"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from support import AyaMaruyama, shirogane_noel

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.types import SampleInput, VoiceRef
from tts_hub.providers.minimax import SaayaYamabuki, aki_rosenthal

BASE = "https://api.minimax.cn"
OK = {"base_resp": {"status_code": 0, "status_msg": "success"}}
VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,255}$")


def build(cassette: AyaMaruyama) -> SaayaYamabuki:
    return SaayaYamabuki("sk-test-key-0001", http=cassette.client())


def test_voice_id_respects_minimax_rules() -> None:
    for name in ("旁白", "narrator", "1st-voice", "   ", "很长的中文音色名称用于测试"):
        vid = aki_rosenthal(name)
        assert VOICE_ID_RE.match(vid), vid
        assert not vid.endswith(("-", "_"))
    assert aki_rosenthal("旁白") == aki_rosenthal("旁白")  # 可复现
    assert aki_rosenthal("旁白") != aki_rosenthal("旁白", salt="other.wav")


def test_clone_uploads_then_clones(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 123456789}, **OK})
        .add(
            "POST",
            "/v1/voice_clone",
            json_body={
                "input_sensitive": False,
                "input_sensitive_type": 0,
                "demo_audio": "",
                **OK,
            },
        )
    )
    adapter = build(cassette)
    task = adapter.clone(shirogane_noel(tmp_path), name="旁白")

    assert task.done and task.voice_id is not None
    assert VOICE_ID_RE.match(task.voice_id)
    upload = cassette.calls[0]
    assert upload["path"] == "/v1/files/upload"
    assert "voice_clone" in upload["body"]
    assert upload["headers"]["authorization"] == "Bearer sk-test-key-0001"
    clone_body = json.loads(cassette.calls[1]["body"])
    assert clone_body["file_id"] == 123456789  # int64 按数字发，不是字符串
    assert VOICE_ID_RE.match(clone_body["voice_id"])
    assert task.task_id == task.voice_id
    adapter.close()


def test_clone_preview_text_goes_to_text_field(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 7}, **OK})
        .add("POST", "/v1/voice_clone", json_body=OK)
    )
    adapter = build(cassette)
    adapter.clone(shirogane_noel(tmp_path), name="旁白", preview_text="试听一句话")
    body = json.loads(cassette.calls[1]["body"])
    assert body["text"] == "试听一句话"
    assert body["model"] == "speech-2.8-hd"
    adapter.close()


def test_clone_requires_prompt_text_when_clone_prompt_given(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 7}, **OK})
    )
    adapter = build(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name="旁白", clone_prompt=shirogane_noel(tmp_path, name="p.wav"))
    assert "prompt_text" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("flag", "expected_type"),
    [({"input_sensitive": False, "input_sensitive_type": 0}, 0),
     ({"input_sensitive": {"type": 0}}, 0),
     ({"input_sensitive": False, "input_sensitive_type": 3}, 3),
     ({"input_sensitive": {"type": 2}}, 2)],
)
def test_safety_flag_handles_both_documented_shapes(flag: dict, expected_type: int) -> None:
    adapter = SaayaYamabuki("k", http=AyaMaruyama().client())
    assert adapter.safety_flag(flag)[0] == expected_type
    adapter.close()


def test_flagged_sample_raises_review_rejected(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 7}, **OK})
        .add("POST", "/v1/voice_clone", json_body={"input_sensitive": {"type": 1}, **OK})
    )
    adapter = build(cassette)
    with pytest.raises(ReviewRejectedError):
        adapter.clone(shirogane_noel(tmp_path), name="旁白")
    adapter.close()


def test_synthesize_decodes_hex_audio() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/v1/t2a_v2",
        json_body={
            "data": {"audio": "48656c6c6f", "status": 2},
            "extra_info": {
                "audio_sample_rate": 32000,
                "audio_format": "mp3",
                "usage_characters": 26,
            },
            **OK,
        },
    )
    adapter = build(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("ttshub-narrator-1"), model="speech-2.8-turbo")
    assert result.audio == b"Hello"
    assert result.chars == 26
    assert result.sample_rate == 32000
    body = json.loads(cassette.calls[0]["body"])
    assert body["voice_setting"]["voice_id"] == "ttshub-narrator-1"
    assert body["stream"] is False
    adapter.close()


def test_synthesize_rejects_too_long_text() -> None:
    adapter = SaayaYamabuki("k", http=AyaMaruyama().client())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * 10000, voice="v", model="speech-2.8-turbo")
    assert "上限" in str(excinfo.value)
    adapter.close()


def test_synthesize_rejects_unknown_model() -> None:
    adapter = SaayaYamabuki("k", http=AyaMaruyama().client())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("hi", voice="v", model="speech-9.9")
    assert "未知合成模型" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (1004, "login fail", AuthError),
        (2049, "invalid API Key", AuthError),
        (2038, "no cloning permission", AuthError),
        (1008, "insufficient balance", QuotaError),
        (1002, "rate limit", QuotaError),
        (1043, "The asr similarity check failed", ReviewRejectedError),
        (1044, "clone prompt similarity check failed", ReviewRejectedError),
        (1026, "input sensitive", ReviewRejectedError),
        (2013, "invalid params", ProviderError),
        (2037, "voice duration too short", ProviderError),
        (2039, "voice clone voice id duplicate", ProviderError),
    ],
)
def test_business_codes_map_to_normalized_errors(code: int, message: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/v1/t2a_v2",
        status=200,
        json_body={"base_resp": {"status_code": code, "status_msg": message}},
    )
    adapter = build(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("hi", voice="v", model="speech-2.8-turbo")
    assert excinfo.value.code == str(code)
    assert excinfo.value.vendor == "minimax"
    adapter.close()


def test_missing_data_object_is_reported() -> None:
    cassette = AyaMaruyama().add("POST", "/v1/t2a_v2", json_body={"data": None, **OK})
    adapter = build(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("hi", voice="v", model="speech-2.8-turbo")
    assert "data.audio" in str(excinfo.value)
    adapter.close()


def test_empty_api_key_is_rejected_before_any_request() -> None:
    with pytest.raises(AuthError):
        SaayaYamabuki("", http=AyaMaruyama().client())


def test_list_voices_flattens_all_three_buckets() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/v1/get_voice",
        json_body={
            "system_voice": [{"voice_id": "sys-1", "voice_name": "系统"}],
            "voice_cloning": [{"voice_id": "cl-1", "voice_name": "复刻", "created_time": "2026-01-01"}],
            "voice_generation": [{"voice_id": "gen-1", "voice_name": "文生"}],
            **OK,
        },
    )
    adapter = build(cassette)
    voices = {v.voice_id: v for v in adapter.list_voices()}
    assert set(voices) == {"sys-1", "cl-1", "gen-1"}
    assert voices["cl-1"].kind == "cloned" and voices["sys-1"].kind == "system"
    adapter.close()


def test_stream_decodes_hex_chunks() -> None:
    stream_body = (
        b'data: {"data":{"audio":"48656c","status":1},"base_resp":{"status_code":0,"status_msg":""}}\n'
        b'data: {"data":{"audio":"6c6f","status":2},"base_resp":{"status_code":0,"status_msg":""}}\n'
    )
    cassette = AyaMaruyama().add("POST", "/v1/t2a_v2", content=stream_body)
    adapter = build(cassette)
    result = adapter.synthesize("hi", voice="v", model="speech-2.8-turbo", stream=True)
    assert b"".join(result.iter_bytes()) == b"Hello"
    adapter.close()


def test_upload_sample_reads_local_path(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/files/upload", json_body={"file": {"file_id": 5}, **OK})
    adapter = build(cassette)
    wav_path = shirogane_noel(tmp_path).path
    assert wav_path is not None
    sample = SampleInput.from_path(wav_path)
    assert adapter.upload_sample(sample, purpose="prompt_audio") == 5
    assert "prompt_audio" in cassette.calls[0]["body"]
    adapter.close()
