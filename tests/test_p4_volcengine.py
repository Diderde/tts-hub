# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""火山引擎适配器测试（离线）。"""

from __future__ import annotations

import base64
import json

import pytest
from support import AyaMaruyama

from tts_hub.core.errors import ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, STATUS_TRAINING, SampleInput, VoiceRef
from tts_hub.providers.volcengine import (
    CODE_OK,
    GET_VOICE_PATH,
    MAX_SAMPLE_BYTES,
    RESOURCE_IDS,
    VOICE_CLONE_PATH,
    NyamuYutenji,
)

SYNTH_PATH = "/api/v3/tts/unidirectional/sse"


def volc(cassette: AyaMaruyama, **extra) -> NyamuYutenji:
    adapter = NyamuYutenji("volc-key", http=cassette.client())
    adapter.configure(extra)
    return adapter


def sse(*payloads: dict) -> bytes:
    """拼一段 SSE：每块一个 event + 一个 data 行。"""
    out = []
    for index, payload in enumerate(payloads):
        out.append(f"event: {300 + index}")
        out.append("data: " + json.dumps(payload, ensure_ascii=False))
        out.append("")
    return ("\n".join(out) + "\n").encode("utf-8")


def audio_block(data: bytes) -> dict:
    return {"code": 0, "message": "", "data": base64.b64encode(data).decode()}


def end_block() -> dict:
    return {"code": CODE_OK, "message": "OK", "data": None, "usage": {"text_words": 4}}


def clone_body(**kw) -> dict:
    base = {"speaker_id": "S_demo1234", "audio": {"data": base64.b64encode(b"RIFF").decode(), "format": "wav"}}
    base.update(kw)
    return base


def test_synthesize_collects_sse_chunks_into_one_audio() -> None:
    cassette = AyaMaruyama().add(
        "POST", SYNTH_PATH, content=sse(audio_block(b"AAA"), audio_block(b"BBB"), end_block())
    )
    adapter = volc(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("S_demo1234"))
    assert result.audio == b"AAABBB"
    assert result.format == "mp3"
    body = json.loads(cassette.calls[0]["body"])
    assert body["req_params"]["text"] == "你好"
    assert body["req_params"]["speaker"] == "S_demo1234"
    assert body["user"]["uid"] == "tts-hub"
    adapter.close()


def test_stream_yields_chunks_lazily() -> None:
    cassette = AyaMaruyama().add(
        "POST", SYNTH_PATH, content=sse(audio_block(b"one"), audio_block(b"two"), end_block())
    )
    adapter = volc(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("S_demo1234"), stream=True)
    assert result.stream is True
    assert list(result.iter_bytes()) == [b"one", b"two"]
    adapter.close()


def test_synthesis_headers_carry_resource_id() -> None:
    cassette = AyaMaruyama().add("POST", SYNTH_PATH, content=sse(end_block()))
    adapter = volc(cassette, resource_id="seed-icl-2.0")
    adapter.synthesize("你好", voice=VoiceRef("S_demo1234"))
    head = cassette.calls[0]["headers"]
    assert head["x-api-key"] == "volc-key"
    assert head["x-api-resource-id"] == "seed-icl-2.0"
    assert head["x-api-request-id"]  # 每次都要带一个新的请求 id
    adapter.close()


def test_additions_is_sent_as_a_json_string() -> None:
    """官方明文：additions 是 jsonstring，不是对象。"""
    cassette = AyaMaruyama().add("POST", SYNTH_PATH, content=sse(end_block()))
    adapter = volc(cassette)
    adapter.synthesize(
        "你好", voice=VoiceRef("S_demo1234"), additions={"disable_markdown_filter": True}
    )
    body = json.loads(cassette.calls[0]["body"])
    additions = body["req_params"]["additions"]
    assert isinstance(additions, str)
    assert json.loads(additions) == {"disable_markdown_filter": True}
    adapter.close()


def test_synthesis_error_block_raises() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        SYNTH_PATH,
        content=sse({"code": 45000000, "message": "quota exceeded for types: concurrency", "data": None}),
    )
    adapter = volc(cassette)
    with pytest.raises(QuotaError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("S_demo1234"))
    assert excinfo.value.code == "45000000"
    assert "concurrency" in str(excinfo.value)
    adapter.close()


def test_clone_headers_do_not_carry_resource_id() -> None:
    """复刻接口发 Resource-Id 会出问题——这是官方 2534847 的明文区分。"""
    cassette = AyaMaruyama().add(
        "POST", VOICE_CLONE_PATH, json_body={"speaker_id": "S_demo1234", "status": 1}
    )
    adapter = volc(cassette)
    adapter.clone(SampleInput(data=b"RIFF", filename="s.wav"), speaker_id="S_demo1234")
    head = cassette.calls[0]["headers"]
    assert head["x-api-key"] == "volc-key"
    assert "x-api-resource-id" not in head
    adapter.close()


def test_prepaid_clone_requires_speaker_id() -> None:
    adapter = volc(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(SampleInput(data=b"x", filename="s.wav"))
    assert "speaker_id" in str(excinfo.value)
    assert "S_" in str(excinfo.value)
    adapter.close()


def test_postpaid_clone_sends_the_fixed_pair() -> None:
    cassette = AyaMaruyama().add(
        "POST", VOICE_CLONE_PATH, json_body={"speaker_id": "my-custom-voice", "status": 1}
    )
    adapter = volc(cassette, billing="postpaid")
    task = adapter.clone(
        SampleInput(data=b"RIFF", filename="s.wav"), custom_speaker_id="my-custom-voice"
    )
    body = json.loads(cassette.calls[0]["body"])
    assert body["speaker_id"] == "custom_speaker_id"  # 官方固定值
    assert body["custom_speaker_id"] == "my-custom-voice"
    assert task.status == STATUS_TRAINING
    adapter.close()


def test_postpaid_clone_requires_custom_speaker_id() -> None:
    adapter = volc(AyaMaruyama(), billing="postpaid")
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(SampleInput(data=b"x", filename="s.wav"))
    assert "custom_speaker_id" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(1, STATUS_TRAINING), (2, STATUS_READY), (4, STATUS_READY), (3, STATUS_FAILED)],
)
def test_clone_status_values_map(status: int, expected: str) -> None:
    cassette = AyaMaruyama().add(
        "POST", VOICE_CLONE_PATH, json_body={"speaker_id": "S_demo1234", "status": status}
    )
    adapter = volc(cassette)
    task = adapter.clone(SampleInput(data=b"RIFF", filename="s.wav"), speaker_id="S_demo1234")
    assert task.status == expected
    assert (task.voice_id == "S_demo1234") is (expected == STATUS_READY)
    adapter.close()


def test_clone_message_warns_about_the_irreversible_charge() -> None:
    cassette = AyaMaruyama().add(
        "POST", VOICE_CLONE_PATH, json_body={"speaker_id": "S_demo1234", "status": 1}
    )
    adapter = volc(cassette)
    task = adapter.clone(SampleInput(data=b"RIFF", filename="s.wav"), speaker_id="S_demo1234")
    assert "138" in task.message and "转正" in task.message and "7 天" in task.message
    adapter.close()


def test_clone_rejects_oversized_sample() -> None:
    adapter = volc(AyaMaruyama())
    big = SampleInput(data=b"\0" * (MAX_SAMPLE_BYTES + 1), filename="big.wav")
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(big, speaker_id="S_demo1234")
    assert "10MB" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, STATUS_FAILED), (1, STATUS_TRAINING), (2, STATUS_READY), (3, STATUS_FAILED), (4, STATUS_READY)],
)
def test_clone_status_polls_get_voice(raw: int, expected: str) -> None:
    cassette = AyaMaruyama().add("POST", GET_VOICE_PATH, json_body={"speaker_id": "S_demo1234", "status": raw})
    adapter = volc(cassette)
    task = adapter.clone_status("S_demo1234")
    assert task.status == expected
    assert "2 与 4 均可合成" in task.message
    adapter.close()


def test_postpaid_synthesis_is_blocked_until_explicitly_acknowledged() -> None:
    """默认必须拦住：138 元且不可逆，不该扣完再解释。"""
    cassette = AyaMaruyama()
    adapter = volc(cassette, billing="postpaid")
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("my-voice"))
    message = str(excinfo.value)
    assert "138" in message and "ack_charge=True" in message
    assert cassette.calls == []  # 一个请求都没发出去
    adapter.close()


def test_postpaid_synthesis_proceeds_with_per_call_ack() -> None:
    cassette = AyaMaruyama().add("POST", SYNTH_PATH, content=sse(audio_block(b"OK"), end_block()))
    adapter = volc(cassette, billing="postpaid")
    assert adapter.synthesize("你好", voice=VoiceRef("my-voice"), ack_charge=True).audio == b"OK"
    adapter.close()


def test_postpaid_synthesis_proceeds_with_config_ack() -> None:
    cassette = AyaMaruyama().add("POST", SYNTH_PATH, content=sse(audio_block(b"OK"), end_block()))
    adapter = volc(cassette, billing="postpaid", ack_first_charge=True)
    assert adapter.synthesize("你好", voice=VoiceRef("my-voice")).audio == b"OK"
    adapter.close()


def test_prepaid_synthesis_needs_no_ack() -> None:
    cassette = AyaMaruyama().add("POST", SYNTH_PATH, content=sse(audio_block(b"OK"), end_block()))
    adapter = volc(cassette)
    assert adapter.synthesize("你好", voice=VoiceRef("S_demo1234")).audio == b"OK"
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (45001104, ReviewRejectedError),
        (45001127, ReviewRejectedError),
        (55001310, ReviewRejectedError),
        (45000000, QuotaError),
        (45001123, QuotaError),
        (40402003, ProviderError),
        (99999999, ProviderError),
    ],
)
def test_error_codes_map_to_normalized_errors(code: int, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", SYNTH_PATH, content=sse({"code": code, "message": "boom", "data": None})
    )
    adapter = volc(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("S_demo1234"))
    assert excinfo.value.code == str(code)
    adapter.close()


def test_http_level_error_is_classified_too() -> None:
    cassette = AyaMaruyama().add(
        "POST", VOICE_CLONE_PATH, status=403, json_body={"code": 45000001, "message": "speaker not found"}
    )
    adapter = volc(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(SampleInput(data=b"RIFF", filename="s.wav"), speaker_id="S_x")
    assert "speaker not found" in str(excinfo.value)
    adapter.close()


def test_list_models_exposes_all_six_resource_ids() -> None:
    adapter = volc(AyaMaruyama())
    models = {m.id: m for m in adapter.list_models()}
    assert set(models) == set(RESOURCE_IDS)
    assert models["seed-icl-2.0"].supports_clone is True
    assert models["seed-tts-2.0"].supports_clone is False
    assert all(m.supports_stream for m in models.values())  # SSE，纯 HTTP 可流式
    adapter.close()
