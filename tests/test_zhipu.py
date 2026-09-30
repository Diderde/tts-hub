# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""智谱适配器测试（离线）。"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest
from support import AyaMaruyama, shirogane_noel

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.types import SampleInput, VoiceRef
from tts_hub.providers.zhipu import (
    CLONE_MODEL,
    DEFAULT_PREVIEW_TEXT,
    SYNTH_MODEL,
    SYSTEM_VOICES,
    RanMitake,
)

WAV = b"RIFF....WAVEfmt "
BASE_PATH = "/api/paas/v4"


def build(cassette: AyaMaruyama) -> RanMitake:
    return RanMitake("zhipu-key.abc.def", http=cassette.client())


def test_voice_enum_matches_api_reference_not_the_guide_sample() -> None:
    assert "female" not in SYSTEM_VOICES
    assert set(SYSTEM_VOICES) == {"tongtong", "chuichui", "xiaochen", "jam", "kazi", "douji", "luodo"}


def test_clone_uploads_then_clones_with_two_distinct_texts(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", f"{BASE_PATH}/files", json_body={"id": "file_abc123", "object": "file"})
        .add("POST", f"{BASE_PATH}/voice/clone", json_body={"voice": "voice_clone_2026_001"})
    )
    adapter = build(cassette)
    task = adapter.clone(
        shirogane_noel(tmp_path),
        name="my_custom_voice_001",
        transcript="您好，这是一段示例音频的文本内容。",
        preview_text="欢迎使用我们的音色复刻服务。",
    )
    assert task.voice_id == "voice_clone_2026_001"
    assert "voice-clone-input" in cassette.calls[0]["body"]
    assert cassette.calls[0]["headers"]["authorization"] == "Bearer zhipu-key.abc.def"

    body = json.loads(cassette.calls[1]["body"])
    assert body["model"] == CLONE_MODEL
    assert body["voice_name"] == "my_custom_voice_001"
    assert body["input"] == "欢迎使用我们的音色复刻服务。"
    assert body["text"] == "您好，这是一段示例音频的文本内容。"
    assert body["file_id"] == "file_abc123"
    adapter.close()


def test_clone_uses_default_preview_text_when_absent(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", f"{BASE_PATH}/files", json_body={"id": "file_1"})
        .add("POST", f"{BASE_PATH}/voice/clone", json_body={"voice": "voice_1"})
    )
    adapter = build(cassette)
    adapter.clone(shirogane_noel(tmp_path), name="v1")
    assert json.loads(cassette.calls[1]["body"])["input"] == DEFAULT_PREVIEW_TEXT
    adapter.close()


def test_clone_requires_a_voice_name(tmp_path: pathlib.Path) -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path))
    assert "voice_name" in str(excinfo.value)
    adapter.close()


def test_clone_rejects_other_models(tmp_path: pathlib.Path) -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError):
        adapter.clone(shirogane_noel(tmp_path), model="glm-tts", name="v1")
    adapter.close()


def test_missing_voice_field_is_reported(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", f"{BASE_PATH}/files", json_body={"id": "file_1"})
        .add("POST", f"{BASE_PATH}/voice/clone", json_body={"file_id": "file_2"})
    )
    adapter = build(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name="v1")
    assert "voice" in str(excinfo.value)
    adapter.close()


def test_synthesize_returns_binary_wav_and_forces_wav_format() -> None:
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/audio/speech", content=WAV)
    adapter = build(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("tongtong"), model=SYNTH_MODEL)
    assert result.audio == WAV
    assert result.format == "wav"
    body = json.loads(cassette.calls[0]["body"])
    assert body == {"model": "glm-tts", "input": "你好", "voice": "tongtong", "response_format": "wav"}
    adapter.close()


def test_synthesize_accepts_cloned_voice_without_prefix() -> None:
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/audio/speech", content=WAV)
    adapter = build(cassette)
    adapter.synthesize("你好", voice=VoiceRef("voice_clone_2026_001"), model=SYNTH_MODEL)
    assert json.loads(cassette.calls[0]["body"])["voice"] == "voice_clone_2026_001"
    adapter.close()


def test_synthesize_rejects_text_over_1024() -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * 1025, voice="tongtong", model=SYNTH_MODEL)
    assert "1024" in str(excinfo.value)
    adapter.close()


def test_stream_decodes_base64_from_choices_delta() -> None:
    piece_a = base64.b64encode(b"AAA").decode()
    piece_b = base64.b64encode(b"BBB").decode()
    stream_body = (
        f'data: {{"choices":[{{"index":0,"delta":{{"role":"assistant","content":"{piece_a}"}}}}]}}\n'
        f'data: {{"choices":[{{"index":1,"delta":{{"content":"{piece_b}"}}}}]}}\n'
        'data: {"choices":[{"finish_reason":"stop","index":2}]}\n'
        "data: [DONE]\n"
    ).encode()
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/audio/speech", content=stream_body)
    adapter = build(cassette)
    result = adapter.synthesize("hi", voice="tongtong", model=SYNTH_MODEL, stream=True)
    assert b"".join(result.iter_bytes()) == b"AAABBB"
    assert json.loads(cassette.calls[0]["body"])["stream"] is True
    adapter.close()


def test_stream_surfaces_error_code_payload() -> None:
    stream_body = b'data: {"error":{"code":"1214","message":"voice id not found"}}\n'
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/audio/speech", content=stream_body)
    adapter = build(cassette)
    result = adapter.synthesize("hi", voice="bad", model=SYNTH_MODEL, stream=True)
    with pytest.raises(ProviderError) as excinfo:
        list(result.iter_bytes())
    assert excinfo.value.code == "1214"
    adapter.close()


def test_list_voices_merges_cloned_and_system() -> None:
    cassette = AyaMaruyama().add(
        "GET",
        f"{BASE_PATH}/voice/list",
        json_body={
            "voice_list": [
                {
                    "voice": "voice_clone_1",
                    "voice_name": "我的音色",
                    "voice_type": "PRIVATE",
                    "download_url": "https://example.invalid/a.mp3",
                    "create_time": "2026-03-15 14:30:52",
                }
            ]
        },
    )
    adapter = build(cassette)
    voices = adapter.list_voices()
    cloned = [v for v in voices if v.kind == "cloned"]
    system = [v for v in voices if v.kind == "system"]
    assert [v.voice_id for v in cloned] == ["voice_clone_1"]
    assert len(system) == len(SYSTEM_VOICES)
    adapter.close()


def test_delete_voice_uses_post_not_delete() -> None:
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/voice/delete", json_body={"voice": "v", "update_time": "t"})
    adapter = build(cassette)
    adapter.delete_voice("voice_clone_1")
    assert cassette.calls[0]["method"] == "POST"
    assert json.loads(cassette.calls[0]["body"]) == {"voice": "voice_clone_1"}
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1000", AuthError),
        ("1001", AuthError),
        ("1003", AuthError),
        ("1113", QuotaError),
        ("1302", QuotaError),
        ("1305", QuotaError),
        ("1301", ReviewRejectedError),
        ("1214", ProviderError),
        ("1261", ProviderError),
        ("1234", ProviderError),
    ],
)
def test_error_codes_map_to_normalized_errors(code: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", f"{BASE_PATH}/audio/speech", status=400, json_body={"error": {"code": code, "message": "boom"}}
    )
    adapter = build(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("hi", voice="tongtong", model=SYNTH_MODEL)
    assert excinfo.value.code == code  # code 是字符串，原样保留
    assert excinfo.value.vendor == "zhipu"
    adapter.close()


def test_unauthorized_status_is_auth_error() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        f"{BASE_PATH}/audio/speech",
        status=401,
        json_body={"error": {"code": "1001", "message": "Header 中未收到 Authentication 参数"}},
    )
    adapter = build(cassette)
    with pytest.raises(AuthError):
        adapter.synthesize("hi", voice="tongtong", model=SYNTH_MODEL)
    adapter.close()


def test_url_sample_is_downloaded_through_guarded_client(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("GET", "/sample.wav", content=b"REMOTE-SAMPLE")
        .add("POST", f"{BASE_PATH}/files", json_body={"id": "file_remote"})
        .add("POST", f"{BASE_PATH}/voice/clone", json_body={"voice": "voice_remote"})
    )
    adapter = build(cassette)
    sample = SampleInput.from_url("http://8.8.8.8/sample.wav")
    task = adapter.clone(sample, name="v-remote")
    assert task.voice_id == "voice_remote"
    assert cassette.calls[0]["path"] == "/sample.wav"
    assert "REMOTE-SAMPLE" in cassette.calls[1]["body"]
    adapter.close()


def test_private_url_sample_is_refused_before_any_upload() -> None:
    cassette = AyaMaruyama().add("POST", f"{BASE_PATH}/files", json_body={"id": "file"})
    adapter = build(cassette)
    with pytest.raises(ProviderError):
        adapter.clone(SampleInput.from_url("http://127.0.0.1/sample.wav"), name="v")
    assert cassette.calls == []  # 一个请求都不该发出去
    adapter.close()
