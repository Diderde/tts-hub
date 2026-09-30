# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""阶跃适配器测试（离线）。"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest
from support import AyaMaruyama, shirogane_noel

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.types import VoiceRef
from tts_hub.providers.stepfun import CLONE_MODELS, SYNTH_MODELS, ArisaIchigaya

AUDIO = b"ID3\x03\x00\x00\x00mp3-bytes"


def build(cassette: AyaMaruyama) -> ArisaIchigaya:
    return ArisaIchigaya("step-key-abc", http=cassette.client())


def test_synth_model_set_is_wider_than_clone_set() -> None:
    assert set(CLONE_MODELS) == {"stepaudio-2.5-tts", "step-tts-2", "step-tts-mini"}
    assert "stepaudio-3-tts" not in CLONE_MODELS
    assert "stepaudio-3-tts" in SYNTH_MODELS


def test_clone_uploads_with_storage_purpose_then_creates_voice(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-abc123", "object": "file"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-tone-FmBrMBqicC", "object": "audio.voice"})
    )
    adapter = build(cassette)
    task = adapter.clone(shirogane_noel(tmp_path), name="旁白", transcript="智能阶跃")
    assert task.done
    assert task.voice_id == "voice-tone-FmBrMBqicC"
    assert "storage" in cassette.calls[0]["body"]
    clone_body = json.loads(cassette.calls[1]["body"])
    assert clone_body["file_id"] == "file-abc123"
    assert clone_body["text"] == "智能阶跃"
    assert clone_body["model"] == "step-tts-2"
    adapter.close()


def test_clone_omits_text_when_transcript_absent(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-1"})
    )
    adapter = build(cassette)
    adapter.clone(shirogane_noel(tmp_path), name="旁白")
    assert "text" not in json.loads(cassette.calls[1]["body"])  # 不传则平台走 ASR
    adapter.close()


def test_clone_reports_duplicated_voice(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-1", "duplicated": True})
    )
    adapter = build(cassette)
    task = adapter.clone(shirogane_noel(tmp_path), name="旁白")
    assert "重复请求" in task.message
    adapter.close()


@pytest.mark.parametrize("model", ["stepaudio-3-tts", "step-tts-vivid", "unknown"])
def test_clone_rejects_models_outside_documented_whitelist(model: str, tmp_path: pathlib.Path) -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), model=model, name="旁白")
    assert "stepaudio-3-tts 仅可用于合成" in str(excinfo.value)
    adapter.close()


def test_missing_voice_id_in_clone_response_is_reported(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"object": "audio.voice"})
    )
    adapter = build(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name="旁白")
    assert "id" in str(excinfo.value)
    adapter.close()


def test_missing_file_id_in_upload_response_is_reported(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/files", json_body={"object": "file"})
    adapter = build(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name="旁白")
    assert "file_id" in str(excinfo.value)
    adapter.close()


def test_synthesize_writes_binary_and_uses_voice_param() -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)
    adapter = build(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("voice-tone-1"), model="step-tts-mini")
    assert result.audio == AUDIO
    assert result.format == "mp3"
    body = json.loads(cassette.calls[0]["body"])
    assert body["voice"] == "voice-tone-1"  # 复刻响应叫 id，合成请求叫 voice
    assert body["input"] == "你好"
    assert body["response_format"] == "mp3"
    adapter.close()


def test_synthesize_rejects_text_over_limit() -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * 1001, voice="v", model="step-tts-mini")
    assert "1000" in str(excinfo.value)
    adapter.close()


def test_synthesize_rejects_unknown_model() -> None:
    adapter = build(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("hi", voice="v", model="step-tts-vivid")
    assert "未知合成模型" in str(excinfo.value)
    adapter.close()


def test_stream_speech_decodes_base64_deltas() -> None:
    first = base64.b64encode(b"chunk-1").decode()
    second = base64.b64encode(b"chunk-2").decode()
    stream_body = (
        f'data: {{"type":"speech.audio.delta","audio":"{first}"}}\n'
        f'data: {{"type":"speech.audio.delta","audio":"{second}"}}\n'
        'data: {"type":"speech.audio.done","audio":""}\n'
        "data: [DONE]\n"
    ).encode()
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=stream_body)
    adapter = build(cassette)
    result = adapter.synthesize("hi", voice="v", model="step-tts-mini", stream=True)
    assert b"".join(result.iter_bytes()) == b"chunk-1chunk-2"
    assert json.loads(cassette.calls[0]["body"])["stream_format"] == "sse"
    adapter.close()


def test_list_voices_reads_data_array() -> None:
    cassette = AyaMaruyama().add(
        "GET",
        "/v1/audio/voices",
        json_body={
            "object": "list",
            "data": [{"id": "voice-tone-1", "file_id": "file-1", "created_at": 1742374363}],
            "has_more": False,
        },
    )
    adapter = build(cassette)
    voices = adapter.list_voices()
    assert [v.voice_id for v in voices] == ["voice-tone-1"]
    assert voices[0].kind == "cloned"
    assert cassette.calls[0]["query"] == {"limit": "100"}
    adapter.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (402, QuotaError), (429, QuotaError), (451, ReviewRejectedError), (503, ProviderError)],
)
def test_http_status_maps_to_normalized_error(status: int, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", "/v1/audio/speech", status=status, content=b"engine is currently overloaded"
    )
    adapter = build(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("hi", voice="v", model="step-tts-mini")
    assert excinfo.value.status == status
    assert "overloaded" in excinfo.value.message  # raw-text 兜底生效
    adapter.close()


def test_clone_status_echoes_sync_completion() -> None:
    adapter = build(AyaMaruyama())
    task = adapter.clone_status("voice-tone-1")
    assert task.done and task.voice_id == "voice-tone-1"
    adapter.close()
