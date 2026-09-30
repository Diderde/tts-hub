# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""科大讯飞适配器测试（离线）。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from support import AyaMaruyama, azki

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, TTSHubError
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, STATUS_TRAINING, SampleInput, VoiceRef
from tts_hub.providers.iflytek import (
    DEFAULT_TEXT_ID,
    MAX_SAMPLE_BYTES,
    WS_HOST,
    WS_PATH,
    ArareNakamachi,
)

TOKEN_PATH = "/aiauth/v1/token"
TRAIN_TEXT = "/voice_train/task/traintext"
TASK_ADD = "/voice_train/task/add"
SUBMIT_AUDIO = "/voice_train/task/submitWithAudio"
ADD_AUDIO = "/voice_train/audio/v1/add"
SUBMIT = "/voice_train/task/submit"
RESULT = "/voice_train/task/result"

APIKEY = azki("iflytek 训练鉴权 key（编造）", "iflytek-apikey-0001")
APISECRET = azki("iflytek 训练签名 secret（编造）", "iflytek-apisecret-0001")
APPID = "12345678"
_AUTH_KEY_LABEL = "api_key"


def iflytek(cassette: AyaMaruyama, **extra) -> ArareNakamachi:
    adapter = ArareNakamachi(APIKEY, api_secret=APISECRET, http=cassette.client())
    adapter.configure({"appid": APPID, **extra})
    return adapter


def token_route(cassette: AyaMaruyama, *, token: str = "ACCESSTOKEN") -> AyaMaruyama:
    return cassette.add("POST", TOKEN_PATH, json_body={"retcode": "000000", "accesstoken": token, "expiresin": 7200})


def training_text_route(cassette: AyaMaruyama, *, seg: str = "seg-1") -> AyaMaruyama:
    return cassette.add(
        "POST",
        TRAIN_TEXT,
        json_body={
            "retcode": "000000",
            "data": {
                "textId": DEFAULT_TEXT_ID,
                "textName": "通用",
                "textSegs": [{"segId": seg, "segText": "今天天气真不错，我们出去走走吧。"}],
            },
        },
    )


def test_token_exchange_uses_double_md5_and_body_timestamp() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", RESULT, json_body={"retcode": "000000", "data": {"trainStatus": -1}})
    adapter = iflytek(cassette)
    adapter.clone_status("task-1")

    token_call = cassette.calls[0]
    body = json.loads(token_call["body"])
    stamp = body["base"]["timestamp"]
    assert body["base"]["appid"] == APPID
    assert body["base"]["version"] == "v1"
    assert len(stamp) == 13  # Unix 毫秒
    expected = hashlib.md5(
        (hashlib.md5(f"{APIKEY}{stamp}".encode()).hexdigest() + token_call["body"]).encode()
    ).hexdigest()
    assert token_call["headers"]["authorization"] == expected
    adapter.close()


def test_business_headers_are_signed_and_recomputable() -> None:
    """X-Sign 必须能独立复算——这是训练侧唯一的签名，错了只会回一句 000007。"""
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", RESULT, json_body={"retcode": "000000", "data": {"trainStatus": -1}})
    adapter = iflytek(cassette)
    adapter.clone_status("task-1")

    head = cassette.calls[1]["headers"]
    assert head["x-appid"] == APPID
    assert head["x-token"] == "ACCESSTOKEN"
    assert head["x-time"].isdigit()
    body_md5 = hashlib.md5(cassette.calls[1]["body"].encode("utf-8")).hexdigest()
    expected = hashlib.md5(f"{APIKEY}{head['x-time']}{body_md5}".encode()).hexdigest()
    assert head["x-sign"] == expected
    adapter.close()


def test_token_is_exchanged_once_and_cached() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", RESULT, json_body={"retcode": "000000", "data": {"trainStatus": -1}})
    adapter = iflytek(cassette)
    for _ in range(3):
        adapter.clone_status("task-1")
    assert len([c for c in cassette.calls if c["path"] == TOKEN_PATH]) == 1
    adapter.close()


def test_missing_appid_is_reported_before_any_request() -> None:
    cassette = AyaMaruyama()
    adapter = ArareNakamachi(APIKEY, api_secret=APISECRET, http=cassette.client())
    adapter.configure({})
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone_status("t")
    assert "appid" in str(excinfo.value)
    assert cassette.calls == []
    adapter.close()


def test_token_failure_is_auth_error() -> None:
    cassette = AyaMaruyama().add(
        "POST", TOKEN_PATH, json_body={"retcode": "000007", "desc": "sign check failed"}
    )
    adapter = iflytek(cassette)
    with pytest.raises(AuthError) as excinfo:
        adapter.clone_status("t")
    assert excinfo.value.code == "000007"
    adapter.close()


def test_clone_without_text_ids_tells_you_what_to_read() -> None:
    """一句话复刻必须照官方文本录；没给 id 时要给出可执行的下一步，而不是白扣一次训练次数。"""
    cassette = token_route(AyaMaruyama())
    training_text_route(cassette, seg="seg-9")
    adapter = iflytek(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(SampleInput(data=b"RIFF", filename="s.wav"))
    message = str(excinfo.value)
    assert "seg-9" in message and "今天天气真不错" in message
    assert [c["path"] for c in cassette.calls if c["path"] != TOKEN_PATH] == [TRAIN_TEXT]
    adapter.close()


def test_clone_with_local_file_uses_multipart_submit() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", TASK_ADD, json_body={"retcode": "000000", "data": "task-42"})
    cassette.add("POST", SUBMIT_AUDIO, json_body={"retcode": "000000", "data": None})
    adapter = iflytek(cassette)
    task = adapter.clone(
        SampleInput(data=b"RIFF-iflytek", filename="s.wav"),
        text_id=DEFAULT_TEXT_ID,
        text_seg_id="seg-1",
        name="旁白",
    )
    assert task.status == STATUS_TRAINING and task.task_id == "task-42"
    body = json.loads(cassette.calls[1]["body"])
    assert body["resourceType"] == 12
    assert body["taskName"] == "旁白"
    assert "engineVersion" not in body  # 标准版不带
    upload = cassette.calls[2]["body"]
    assert "RIFF-iflytek" in upload
    assert "task-42" in upload and "seg-1" in upload
    adapter.close()


def test_clone_with_url_registers_audio_then_submits() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", TASK_ADD, json_body={"retcode": "000000", "data": "task-7"})
    cassette.add("POST", ADD_AUDIO, json_body={"retcode": "000000", "data": None})
    cassette.add("POST", SUBMIT, json_body={"retcode": "000000", "data": None})
    adapter = iflytek(cassette)
    adapter.clone(
        SampleInput.from_url("http://8.8.8.8/voice.wav"),
        text_id=DEFAULT_TEXT_ID,
        text_seg_id="seg-1",
    )
    paths = [c["path"] for c in cassette.calls]
    assert paths[-3:] == [TASK_ADD, ADD_AUDIO, SUBMIT]
    assert json.loads(cassette.calls[-2]["body"])["audioUrl"] == "http://8.8.8.8/voice.wav"
    adapter.close()


def test_multi_style_clone_adds_engine_version() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", TASK_ADD, json_body={"retcode": "000000", "data": "task-8"})
    cassette.add("POST", SUBMIT_AUDIO, json_body={"retcode": "000000", "data": None})
    adapter = iflytek(cassette)
    adapter.clone(
        SampleInput(data=b"RIFF", filename="s.wav"),
        model="x6_clone",
        text_id=DEFAULT_TEXT_ID,
        text_seg_id="seg-1",
    )
    body = json.loads(cassette.calls[1]["body"])
    assert body["engineVersion"] == "omni_v1"  # 多风格版必带
    adapter.close()


def test_clone_rejects_oversized_sample() -> None:
    adapter = iflytek(AyaMaruyama())
    big = SampleInput(data=b"\0" * (MAX_SAMPLE_BYTES + 1), filename="big.wav")
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(big, text_id=DEFAULT_TEXT_ID, text_seg_id="seg-1")
    assert "3MB" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("raw", "expected"), [(-1, STATUS_TRAINING), (2, STATUS_TRAINING), (1, STATUS_READY), (0, STATUS_FAILED)]
)
def test_clone_status_maps_train_status(raw: int, expected: str) -> None:
    """``2`` 在两处官方文档里语义不一致（草稿 / 排队中），实现按"非终态"处理，与两边都不冲突。"""
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "POST",
        RESULT,
        json_body={"retcode": "000000", "data": {"trainStatus": raw, "assetId": "res-abc", "failedDesc": ""}},
    )
    adapter = iflytek(cassette)
    task = adapter.clone_status("task-1")
    assert task.status == expected
    if expected == STATUS_READY:
        assert task.voice_id == "res-abc"  # 合成时用作 res_id
    else:
        assert task.voice_id is None
    adapter.close()


def test_clone_status_surfaces_failure_reason() -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "POST", RESULT, json_body={"retcode": "000000", "data": {"trainStatus": 0, "failedDesc": "音频质量不达标"}}
    )
    adapter = iflytek(cassette)
    task = adapter.clone_status("task-1")
    assert task.failed and "音频质量不达标" in task.message
    adapter.close()


def test_synthesize_is_degraded_with_a_usable_alternative() -> None:
    """不假装成功，而且要把"那你能怎么办"说清楚。"""
    adapter = iflytek(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("res-abc"))
    message = str(excinfo.value)
    assert "没有 HTTP 合成接口" in message
    assert WS_HOST in message and WS_PATH in message
    assert "x4_*" in message  # 说明长文本那条路为什么不走
    assert "signed_ws_url" in message  # 给出可执行的替代
    adapter.close()


def test_signed_ws_url_is_ready_to_use() -> None:
    """签名 URL 是纯计算，可以离线验；接入方拿去就能连 WS。"""
    from urllib.parse import parse_qs, urlsplit

    adapter = iflytek(AyaMaruyama())
    url = adapter.signed_ws_url(date="Fri, 05 May 2023 10:43:39 GMT")
    parts = urlsplit(url)
    assert parts.scheme == "wss" and parts.netloc == WS_HOST and parts.path == WS_PATH
    query = parse_qs(parts.query)
    assert query["host"] == [WS_HOST]
    origin = base64.b64decode(query["authorization"][0]).decode("utf-8")
    assert origin.startswith(f'{_AUTH_KEY_LABEL}="{APIKEY}"')
    adapter.close()


def test_list_models_marks_streaming_as_unavailable() -> None:
    adapter = iflytek(AyaMaruyama())
    models = {m.id: m for m in adapter.list_models()}
    assert set(models) == {"x5_clone", "x6_clone"}
    assert all(m.supports_clone for m in models.values())
    assert not any(m.supports_stream for m in models.values())
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("10000", AuthError),
        ("10020", AuthError),
        ("10018", QuotaError),
        ("10021", QuotaError),
        ("11201", QuotaError),
        ("20001", ProviderError),
        ("60000", ProviderError),
        ("99999", ProviderError),
    ],
)
def test_training_error_codes_map(code: str, expected: type[TTSHubError]) -> None:
    cassette = token_route(AyaMaruyama())
    cassette.add("POST", RESULT, json_body={"retcode": code, "desc": "boom"})
    adapter = iflytek(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.clone_status("task-1")
    assert excinfo.value.code == code
    adapter.close()


def test_review_rejection_wording_is_possible_via_failed_desc() -> None:
    """审核拒绝没有专用码，表现为 trainStatus=0 + failedDesc，这条要能透出来。"""
    cassette = token_route(AyaMaruyama())
    cassette.add(
        "POST", RESULT, json_body={"retcode": "000000", "data": {"trainStatus": 0, "failedDesc": "疑似敏感人物"}}
    )
    adapter = iflytek(cassette)
    task = adapter.clone_status("task-1")
    assert task.failed and "敏感人物" in task.message
    adapter.close()
