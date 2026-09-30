# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""P3 三家适配器测试（离线）。"""

from __future__ import annotations

import base64
import json
import pathlib
import re

import pytest
from support import AyaMaruyama, azki, shirogane_noel
from test_polling import eve_wakamiya

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.polling import ChisatoShirasagi
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, STATUS_TRAINING, SampleInput, VoiceRef
from tts_hub.providers.baidu import MisumiUika
from tts_hub.providers.dashscope import TakiShiina
from tts_hub.providers.unisound import MutsumiWakaba

VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,255}$")


ENROLL = "/api/v1/services/audio/tts/customization"
SYNTH = "/api/v1/services/audio/tts/SpeechSynthesizer"
QWEN_SYNTH = "/api/v1/services/aigc/multimodal-generation/generation"


def aliyun(cassette: AyaMaruyama) -> TakiShiina:
    return TakiShiina("sk-dashscope-0001", http=cassette.client())


def test_aliyun_cosyvoice_clone_needs_a_public_url(tmp_path: pathlib.Path) -> None:
    adapter = aliyun(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), target_model="cosyvoice-v3-flash", name="旁白")
    assert "公网可访问" in str(excinfo.value)
    assert "qwen3-tts-vc" in str(excinfo.value)  # 要把可行路径指出来
    adapter.close()


def test_aliyun_cosyvoice_clone_registers_then_awaits_review() -> None:
    cassette = AyaMaruyama().add(
        "POST", ENROLL, json_body={"output": {"voice_id": "cosyvoice-v3-flash-myvoice-abc123"}}
    )
    adapter = aliyun(cassette)
    task = adapter.clone(
        SampleInput.from_url("http://8.8.8.8/voice.wav"),
        target_model="cosyvoice-v3-flash",
        name="my voice!",
    )
    assert task.status == STATUS_TRAINING
    assert task.task_id == "cosyvoice-v3-flash-myvoice-abc123"
    body = json.loads(cassette.calls[0]["body"])
    assert body["model"] == "voice-enrollment"
    assert body["input"]["action"] == "create_voice"
    assert body["input"]["target_model"] == "cosyvoice-v3-flash"
    assert body["input"]["prefix"] == "myvoice"  # 只留数字与字母，且 ≤10
    assert body["input"]["url"] == "http://8.8.8.8/voice.wav"
    adapter.close()


def test_aliyun_qwen_clone_embeds_local_file_as_data_url(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add(
        "POST", ENROLL, json_body={"output": {"voice": "yourVoice", "target_model": "qwen3-tts-vc-2026-01-22"}}
    )
    adapter = aliyun(cassette)
    task = adapter.clone(shirogane_noel(tmp_path, b"RIFF-local"), target_model="qwen3-tts-vc-2026-01-22")
    assert task.status == STATUS_READY and task.voice_id == "yourVoice"
    body = json.loads(cassette.calls[0]["body"])
    assert body["model"] == "qwen-voice-enrollment"
    assert body["input"]["action"] == "create"
    assert body["input"]["audio"]["data"].startswith("data:audio/wav;base64,")
    assert base64.b64decode(body["input"]["audio"]["data"].split(",", 1)[1]) == b"RIFF-local"
    adapter.close()


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [("DEPLOYING", STATUS_TRAINING), ("OK", STATUS_READY), ("UNDEPLOYED", STATUS_FAILED)],
)
def test_aliyun_review_status_maps_to_clone_task(raw_status: str, expected: str) -> None:
    cassette = AyaMaruyama().add(
        "POST", ENROLL, json_body={"output": {"status": raw_status, "target_model": "cosyvoice-v3-flash"}}
    )
    adapter = aliyun(cassette)
    task = adapter.clone_status("voice-1")
    assert task.status == expected
    assert (task.voice_id == "voice-1") is (expected == STATUS_READY)
    assert json.loads(cassette.calls[0]["body"])["input"]["action"] == "query_voice"
    adapter.close()


def test_aliyun_synthesize_downloads_the_returned_url() -> None:
    synth_reply = {
        "output": {"audio": {"url": "http://8.8.8.8/out.mp3", "expires_at": 1}},
        "usage": {"characters": 7},
    }
    cassette = (
        AyaMaruyama()
        .add("POST", SYNTH, json_body=synth_reply)
        .add("GET", "/out.mp3", content=b"MP3-BYTES")
    )
    adapter = aliyun(cassette)
    result = adapter.synthesize("你好世界", voice=VoiceRef("cosyvoice-v3-flash-myvoice-abc"))
    assert result.audio == b"MP3-BYTES"  # 音频是二次下载来的
    assert result.chars == 7
    assert json.loads(cassette.calls[0]["body"])["input"]["format"] == "mp3"
    adapter.close()


def test_aliyun_synthesize_without_audio_url_explains_the_likely_cause() -> None:
    cassette = AyaMaruyama().add("POST", SYNTH, json_body={"output": {"audio": {"data": ""}}})
    adapter = aliyun(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("v"), model="cosyvoice-v3-flash")
    assert "target_model" in str(excinfo.value)
    adapter.close()


def test_aliyun_qwen_synthesis_uses_the_multimodal_endpoint() -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", QWEN_SYNTH, json_body={"output": {"audio": {"url": "http://8.8.8.8/q.mp3"}}})
        .add("GET", "/q.mp3", content=b"QWEN-MP3")
    )
    adapter = aliyun(cassette)
    result = adapter.synthesize("hi", voice=VoiceRef("yourVoice"), model="qwen3-tts-vc-2026-01-22")
    assert result.audio == b"QWEN-MP3"
    body = json.loads(cassette.calls[0]["body"])
    assert body == {"model": "qwen3-tts-vc-2026-01-22", "input": {"text": "hi", "voice": "yourVoice"}}
    adapter.close()


def test_aliyun_qwen_char_limit_enforced() -> None:
    adapter = aliyun(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * 601, voice=VoiceRef("v"), model="qwen3-tts-vc-2026-01-22")
    assert "600" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("Throttling.RateQuota", QuotaError),
        ("Arrearage", QuotaError),
        ("InvalidApiKey", AuthError),
        ("NOT AUTHORIZED", AuthError),
        ("DataInspectionFailed", ReviewRejectedError),
        ("AuditFailed", ProviderError),
    ],
)
def test_aliyun_error_codes_map(code: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", SYNTH, status=400, json_body={"code": code, "message": "boom", "request_id": "r1"}
    )
    adapter = aliyun(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("hi", voice=VoiceRef("v"))
    assert excinfo.value.code == code
    adapter.close()


def test_aliyun_list_voices_reads_voice_list() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        ENROLL,
        json_body={"output": {"voice_list": [{"voice_id": "v1", "status": "OK", "gmt_create": "2026-01-01"}]}},
    )
    adapter = aliyun(cassette)
    voices = adapter.list_voices()
    assert [v.voice_id for v in voices] == ["v1"]
    assert voices[0].note == "OK"
    adapter.close()


BAIDU_VOICE = "/rest/2.0/speech/publiccloudspeech/v1/voice/clone"
BAIDU_TOKEN = "/oauth/2.0/token"


def baidu(cassette: AyaMaruyama, **kw) -> MisumiUika:
    return MisumiUika(
        azki("baidu 测试 key（编造）", "baidu-ak"),
        api_secret=azki("baidu 测试 secret（编造）", "baidu-sk"),
        http=cassette.client(),
        **kw,
    )


def token_route(
    cassette: AyaMaruyama, token: str = "tok-1", ttl: int = 2592000, *, once: bool = False
) -> AyaMaruyama:
    return cassette.add(
        "POST", BAIDU_TOKEN, json_body={"access_token": token, "expires_in": ttl}, once=once
    )


def test_baidu_requires_both_keys() -> None:
    with pytest.raises(AuthError) as excinfo:
        MisumiUika("only-ak", http=AyaMaruyama().client())
    assert "Secret Key" in str(excinfo.value)


def test_baidu_token_is_exchanged_once_and_cached(tmp_path: pathlib.Path) -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", f"{BAIDU_VOICE}/create", json_body={"status": 0, "data": {"voice_id": 1063622}})
    adapter = baidu(cassette)
    for _ in range(3):
        adapter.clone(shirogane_noel(tmp_path, b"RIFF"), name="旁白")
    token_calls = [c for c in cassette.calls if c["path"] == BAIDU_TOKEN]
    assert len(token_calls) == 1  # 换一次就够，不该每次都换
    assert token_calls[0]["query"]["client_id"] == "baidu-ak"
    assert token_calls[0]["query"]["client_secret"] == "baidu-sk"
    assert token_calls[0]["query"]["grant_type"] == "client_credentials"
    create = next(c for c in cassette.calls if c["path"].endswith("/create"))
    assert create["query"]["access_token"] == "tok-1"
    assert "authorization" not in create["headers"]
    adapter.close()


def test_baidu_token_exchange_failure_is_auth_error() -> None:
    cassette = AyaMaruyama().add(
        "POST", BAIDU_TOKEN, json_body={"error": "invalid_client", "error_description": "unknown client id"}
    )
    adapter = baidu(cassette)
    with pytest.raises(AuthError) as excinfo:
        adapter.list_voices()
    assert "unknown client id" in str(excinfo.value)
    adapter.close()


def test_baidu_expired_token_is_refreshed_and_retried_once() -> None:
    cassette = token_route(AyaMaruyama(), token="old-token", once=True)
    cassette.add(
        "POST",
        f"{BAIDU_VOICE}/list",
        status=401,
        json_body={"status": 111, "message": "Access token expired"},
        once=True,  # 用完即弃，否则第二次请求又会命中这条 401
    )
    cassette.add(
        "POST",
        BAIDU_TOKEN,
        json_body={"access_token": azki("轮换后的 token（编造）", "new-token"), "expires_in": 2592000},
        once=True,
    )
    cassette.add("POST", f"{BAIDU_VOICE}/list", json_body={"status": 0, "data": {"items": []}})
    adapter = baidu(cassette)
    assert adapter.list_voices() == []
    tokens = [c["query"].get("access_token") for c in cassette.calls if c["path"].endswith("/list")]
    assert tokens == ["old-token", "new-token"]
    adapter.close()


def test_baidu_api_key_auth_mode_skips_the_token_dance() -> None:
    cassette = AyaMaruyama().add("POST", f"{BAIDU_VOICE}/list", json_body={"status": 0, "data": {"items": []}})
    adapter = baidu(cassette)
    adapter.auth_mode = "api_key"
    assert adapter.list_voices() == []
    assert cassette.calls[0]["headers"]["authorization"] == "baidu-ak"
    assert "access_token" not in cassette.calls[0]["query"]
    adapter.close()


def test_baidu_clone_embeds_local_file_as_base64(tmp_path: pathlib.Path) -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", f"{BAIDU_VOICE}/create", json_body={"status": 0, "data": {"voice_id": 1063622}})
    adapter = baidu(cassette)
    task = adapter.clone(shirogane_noel(tmp_path, b"RIFF-baidu"), name="旁白")
    assert task.status == STATUS_READY and task.voice_id == "1063622"
    body = json.loads(cassette.calls[-1]["body"])
    assert body["voice_name"] == "旁白"
    assert base64.b64decode(body["audio_file"]) == b"RIFF-baidu"
    assert "audio_url" not in body
    adapter.close()


def test_baidu_clone_uses_url_when_given() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", f"{BAIDU_VOICE}/create", json_body={"status": 0, "data": {"voice_id": 7}})
    adapter = baidu(cassette)
    adapter.clone(SampleInput.from_url("http://8.8.8.8/s.wav"), name="旁白")
    body = json.loads(cassette.calls[-1]["body"])
    assert body["audio_url"] == "http://8.8.8.8/s.wav" and "audio_file" not in body
    adapter.close()


def test_baidu_clone_requires_unique_voice_name(tmp_path: pathlib.Path) -> None:
    adapter = baidu(token_route(AyaMaruyama()))
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name=None)
    assert "voice_name" in str(excinfo.value)
    adapter.close()


def test_baidu_synthesize_returns_binary_when_content_type_is_audio() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", f"{BAIDU_VOICE}/tts", content=b"ID3-baidu", headers={"content-type": "audio/mp3"})
    adapter = baidu(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("1063622"))
    assert result.audio == b"ID3-baidu"
    body = json.loads(cassette.calls[-1]["body"])
    assert body["voice_id"] == 1063622  # 官方标注 int
    assert body["media_type"] == "mp3" and body["speed"] == 5
    adapter.close()


def test_baidu_synthesize_detects_json_error_even_on_http_200() -> None:
    """成败看 Content-Type：失败时百度回 JSON，状态码可能仍是 200。"""
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "POST",
        f"{BAIDU_VOICE}/tts",
        status=200,
        json_body={"status": 11011, "message": "voice_id not exists"},
    )
    adapter = baidu(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("999"))
    assert excinfo.value.code == "11011"
    adapter.close()


def test_baidu_stream_is_degraded_with_a_reason() -> None:
    adapter = baidu(token_route(AyaMaruyama()))
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("1"), stream=True)
    assert "WebSocket" in str(excinfo.value)
    adapter.close()


def test_baidu_text_limit() -> None:
    adapter = baidu(token_route(AyaMaruyama()))
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * 501, voice=VoiceRef("1"))
    assert "500" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [(110, AuthError), (111, AuthError), (217, AuthError), (15, QuotaError), (17, QuotaError),
     (10027, ReviewRejectedError), (11015, ReviewRejectedError), (12001, ProviderError)],
)
def test_baidu_error_codes_map(code: int, expected: type[TTSHubError]) -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", f"{BAIDU_VOICE}/tts", status=400, json_body={"status": code, "message": "boom"})
    adapter = baidu(cassette)
    with pytest.raises(expected):
        adapter.synthesize("你好", voice=VoiceRef("1"))
    adapter.close()


UNI_VOICE = "/v1/audio/voices/clone"
UNI_TASKS = "/v1/audio/speech/tasks"


def unisound(cassette: AyaMaruyama) -> MutsumiWakaba:
    adapter = MutsumiWakaba("uni-key", http=cassette.client())
    clock, sleep, _ = eve_wakamiya()
    adapter.poller = ChisatoShirasagi(interval=2.0, timeout=60.0, sleep=sleep, clock=clock)
    return adapter


def test_unisound_clone_uploads_then_clones_synchronously(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 871009177767936}})
        .add("POST", UNI_VOICE, json_body={"input_sensitive": False, "demo_audio": "http://8.8.8.8/d.wav"})
    )
    adapter = unisound(cassette)
    task = adapter.clone(shirogane_noel(tmp_path), name="旁白")
    assert task.status == STATUS_READY
    assert VOICE_ID_RE.match(task.voice_id or "")
    assert "voice_clone" in cassette.calls[0]["body"]
    body = json.loads(cassette.calls[1]["body"])
    assert body["file_id"] == 871009177767936
    assert body["model"] == "u2-tts-clone"
    assert body["voice_id"] == task.voice_id  # voice_id 由调用方生成
    adapter.close()


def test_unisound_flagged_sample_is_rejected(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files/upload", json_body={"file": {"file_id": 1}})
        .add("POST", UNI_VOICE, json_body={"input_sensitive": True, "input_sensitive_type": 1})
    )
    adapter = unisound(cassette)
    with pytest.raises(ReviewRejectedError):
        adapter.clone(shirogane_noel(tmp_path), name="旁白")
    adapter.close()


def test_unisound_synthesize_polls_the_async_task_then_downloads() -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", UNI_TASKS, json_body={"task_id": "t-1", "file_id": 5, "usage_characters": 11})
        .add("GET", UNI_TASKS, json_body={"task_id": "t-1", "status": "Waiting", "file_id": 5}, once=True)
        .add("GET", UNI_TASKS, json_body={"task_id": "t-1", "status": "Processing", "file_id": 5}, once=True)
        .add("GET", UNI_TASKS, json_body={"task_id": "t-1", "status": "Success", "file_id": 5}, once=True)
        .add("GET", "/v1/files/retrieve_content", content=b"UNI-MP3")
    )
    adapter = unisound(cassette)
    result = adapter.synthesize("水泊梁山的故事家喻户晓", voice=VoiceRef("my-voice-001"))
    assert result.audio == b"UNI-MP3"
    assert result.chars == 11
    polls = [c for c in cassette.calls if c["method"] == "GET" and c["path"] == UNI_TASKS]
    assert len(polls) == 3  # Waiting -> Processing -> Success
    assert polls[0]["query"]["task_id"] == "t-1"
    adapter.close()


def test_unisound_prefers_download_url_over_the_content_api() -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", UNI_TASKS, json_body={"task_id": "t-2", "file_id": 9})
        .add(
            "GET",
            UNI_TASKS,
            json_body={"status": "Success", "file_id": 9, "download_url": "http://8.8.8.8/signed.mp3"},
        )
        .add("GET", "/signed.mp3", content=b"SIGNED")
    )
    adapter = unisound(cassette)
    assert adapter.synthesize("hi", voice=VoiceRef("v")).audio == b"SIGNED"
    assert [c["path"] for c in cassette.calls] == [UNI_TASKS, UNI_TASKS, "/signed.mp3"]
    adapter.close()


def test_unisound_failed_task_raises() -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", UNI_TASKS, json_body={"task_id": "t-3", "file_id": 1})
        .add(
            "GET",
            UNI_TASKS,
            json_body={"status": "Failed", "base_resp": {"status_code": 0, "status_msg": "合成失败"}},
        )
    )
    adapter = unisound(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("hi", voice=VoiceRef("v"))
    assert "合成失败" in str(excinfo.value)
    adapter.close()


def test_unisound_stream_is_degraded_with_the_real_reason() -> None:
    adapter = unisound(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("hi", voice=VoiceRef("v"), stream=True)
    assert "WebSocket" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(("model", "limit"), [("u2-tts", 50_000), ("u2-tts-clone", 20_000)])
def test_unisound_char_limit_depends_on_model(model: str, limit: int) -> None:
    adapter = unisound(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("字" * (limit + 1), voice=VoiceRef("v"), model=model)
    assert str(limit) in str(excinfo.value)
    adapter.close()


def test_unisound_content_api_falls_back_across_purposes() -> None:
    """文档写 t2a_async、官方 SDK 用 t2a_async_output，两个 purpose 都要能兜住。"""
    cassette = (
        AyaMaruyama()
        .add("POST", UNI_TASKS, json_body={"task_id": "t-4", "file_id": 3})
        .add("GET", UNI_TASKS, json_body={"status": "Success", "file_id": 3})
        .add(
            "GET",
            "/v1/files/retrieve_content",
            status=400,
            json_body={"base_resp": {"status_code": 100001, "status_msg": "purpose mismatch"}},
            once=True,
        )
        .add("GET", "/v1/files/retrieve_content", content=b"SECOND-TRY")
    )
    adapter = unisound(cassette)
    assert adapter.synthesize("hi", voice=VoiceRef("v")).audio == b"SECOND-TRY"
    purposes = [
        c["query"].get("purpose") for c in cassette.calls if c["path"].endswith("retrieve_content")
    ]
    assert purposes == ["t2a_async_output", "t2a_async"]
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [("100101", AuthError), ("100103", AuthError), ("100501", QuotaError), ("220109", QuotaError),
     ("100003", ProviderError), ("100004", ReviewRejectedError)],
)
def test_unisound_error_codes_map(code: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST", UNI_TASKS, status=401, json_body={"base_resp": {"status_code": int(code), "status_msg": "boom"}}
    )
    adapter = unisound(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.synthesize("hi", voice=VoiceRef("v"))
    assert excinfo.value.code == code
    adapter.close()


def test_unisound_list_voices_flattens_buckets() -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/v1/audio/voices/query",
        json_body={
            "system_voice": [{"voice_id": "cn_male_chenyu", "voice_name": "陈宇"}],
            "voice_cloning": [{"voice_id": "my-voice-001", "created_time": "2026-01-01"}],
        },
    )
    adapter = unisound(cassette)
    voices = {v.voice_id: v for v in adapter.list_voices()}
    assert set(voices) == {"cn_male_chenyu", "my-voice-001"}
    assert voices["my-voice-001"].kind == "cloned"
    adapter.close()
