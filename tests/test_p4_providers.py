# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""P4 适配器测试：腾讯 TC3 签名对拍与讯飞签名 URL。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
from support import AyaMaruyama, azki, shirogane_noel

from tts_hub.core.errors import AuthError, ProviderError, QuotaError, ReviewRejectedError, TTSHubError
from tts_hub.core.signing import (
    amane_kanata,
    kiryu_coco,
    tokoyami_towa,
    tsunomaki_watame,
)
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, STATUS_TRAINING, VoiceRef
from tts_hub.providers.tencent import FAST_VOICE_TYPE, SakikoTogawa

SECRET_ID = azki("tencent 文档示例 SecretId", "AKIDEXAMPLEEXAMPLE00")
SECRET_KEY = azki("tencent 文档示例 SecretKey", "SecretKeyEXAMPLEEXAMPLE000000")


def tc3(**kw: Any) -> dict[str, str]:
    base: dict[str, Any] = {
        "secret_id": SECRET_ID,
        "secret_key": SECRET_KEY,
        "service": "vrs",
        "action": "CreateVRSTask",
        "version": "2020-08-24",
        "region": "ap-guangzhou",
        "host": "vrs.tencentcloudapi.com",
        "payload": b'{"a":1}',
        "timestamp": 1551113065,
    }
    base.update(kw)
    return amane_kanata(**base)


def test_tc3_authorization_shape_matches_the_documented_format() -> None:
    headers = tc3()
    auth = headers["Authorization"]
    assert auth.startswith("TC3-HMAC-SHA256 Credential=")
    assert f"Credential={SECRET_ID}/2019-02-25/vrs/tc3_request, " in auth
    assert "SignedHeaders=content-type;host, " in auth
    signature = auth.split("Signature=")[1]
    assert len(signature) == 64
    assert all(ch in "0123456789abcdef" for ch in signature)


def test_tc3_credential_date_uses_utc_not_local_time() -> None:
    """官方警告：日期若按本地时区算，凌晨调用必失败。

    取一个 UTC 上属于 2 月 26 日、而东八区已是 2 月 26 日凌晨的时间戳反着验：
    这里直接断言派生日期等于按 UTC 换算的结果。
    """
    stamp = int(datetime(2026, 2, 28, 16, 30, tzinfo=UTC).timestamp())
    headers = tc3(timestamp=stamp)
    assert "/2026-02-28/vrs/tc3_request" in headers["Authorization"]
    assert headers["X-TC-Timestamp"] == str(stamp)


def test_tc3_is_deterministic_and_body_sensitive() -> None:
    assert tc3() == tc3()
    assert tc3()["Authorization"] != tc3(payload=b'{"a":2}')["Authorization"]
    assert tc3()["Authorization"] != tc3(service="tts")["Authorization"]


def test_tc3_signature_is_recomputable_from_the_documented_steps() -> None:
    """按文档公式独立复算一遍，确认实现照的是文档而不是"能跑就行"。"""
    stamp, date = 1551113065, "2019-02-25"
    payload = b'{"a":1}'
    canonical_headers = "content-type:application/json; charset=utf-8\nhost:vrs.tencentcloudapi.com\n"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, "content-type;host", hashlib.sha256(payload).hexdigest()]
    )
    scope = f"{date}/vrs/tc3_request"
    string_to_sign = "\n".join(
        ["TC3-HMAC-SHA256", str(stamp), scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )

    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_signing = sign(sign(sign(("TC3" + SECRET_KEY).encode(), date), "vrs"), "tc3_request")
    expected = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    assert tc3()["Authorization"].endswith(expected)


def test_tc3_headers_cover_required_公共参数() -> None:
    headers = tc3()
    for name in ("X-TC-Action", "X-TC-Version", "X-TC-Timestamp", "X-TC-Region", "Content-Type", "Host"):
        assert name in headers
    assert headers["X-TC-Action"] == "CreateVRSTask"
    assert headers["Content-Type"] == "application/json; charset=utf-8"


def test_tc3_optional_token_header() -> None:
    assert "X-TC-Token" not in tc3()
    assert tc3(token="tmp-token")["X-TC-Token"] == "tmp-token"


def test_tc3_accepts_dict_payload_identically_to_bytes() -> None:
    assert tc3(payload={"a": 1})["Authorization"] == tc3(payload=b'{"a":1}')["Authorization"]


def test_iflytek_signed_url_structure() -> None:
    sample_key = azki("讯飞签名对拍 key（编造）", "my-key")
    sample_secret = azki("讯飞签名对拍 secret（编造）", "my-secret")
    url = kiryu_coco(
        host="cn-huabei-1.xf-yun.com",
        path="/v1/private/voice_clone",
        api_key=sample_key,
        api_secret=sample_secret,
        date="Fri, 05 May 2023 10:43:39 GMT",
    )
    assert url.startswith("wss://cn-huabei-1.xf-yun.com/v1/private/voice_clone?")
    assert "authorization=" in url and "date=" in url and "host=" in url

    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(url).query)
    assert query["host"] == ["cn-huabei-1.xf-yun.com"]
    assert query["date"] == ["Fri, 05 May 2023 10:43:39 GMT"]
    origin = base64.b64decode(query["authorization"][0]).decode("utf-8")
    assert origin.startswith('api_key="my-key", algorithm="hmac-sha256", ')
    assert 'headers="host date request-line"' in origin
    assert origin.endswith('"')


def test_iflytek_signature_origin_is_three_lines_in_order() -> None:
    """签名原文三段顺序固定，且末尾没有多余换行。"""
    url = kiryu_coco(
        host="h.example.com",
        path="/p",
        api_key="k",
        api_secret="s",
        date="D",
    )
    from urllib.parse import parse_qs, urlsplit

    origin_b64 = parse_qs(urlsplit(url).query)["authorization"][0]
    origin = base64.b64decode(origin_b64).decode("utf-8")
    signature = origin.split('signature="')[1].rstrip('"')
    expected = base64.b64encode(
        tsunomaki_watame("s", "host: h.example.com\ndate: D\nGET /p HTTP/1.1")
    ).decode()
    assert signature == expected


def test_rfc1123_timestamp_shape() -> None:
    stamp = tokoyami_towa(datetime(2023, 5, 5, 10, 43, 39, tzinfo=UTC))
    assert stamp == "Fri, 05 May 2023 10:43:39 GMT"
    assert len(tsunomaki_watame("k", "m")) == 32  # SHA256 原始摘要


VRS = "/"
TRAINING_TEXT = {"TrainingTextList": [{"TextId": "00001", "Text": "在很久很久以前 鸟群中有一只小鸟"}]}


def tencent(cassette: AyaMaruyama) -> SakikoTogawa:
    return SakikoTogawa(
        "AKIDEXAMPLEEXAMPLE00",
        api_secret=azki("tencent 文档示例 SecretKey（变体）", "SecretKeyEXAMPLE0000"),
        http=cassette.client(),
    )


def envelope(action_payload: dict) -> dict:
    return {"Response": {"RequestId": "req-1", **action_payload}}


def route(cassette: AyaMaruyama, action: str, payload: dict[str, Any], **kw: Any) -> AyaMaruyama:
    """按 X-TC-Action 路由：三个产品域共用同一个 host:port，只能靠请求头区分。"""

    def matches(entry: dict[str, Any]) -> bool:
        headers: dict[str, str] = entry["headers"]
        return headers.get("x-tc-action") == action

    return cassette.add("POST", "/", json_body=envelope(payload), predicate=matches, **kw)


def test_clone_requires_text_id_and_tells_you_what_to_read(tmp_path: pathlib.Path) -> None:
    """一句话复刻必须照官方文本录；没给 text_id 时要给出可执行的下一步，而不是白扣一次钱。"""
    cassette = route(AyaMaruyama(), "GetTrainingText", {"Data": TRAINING_TEXT})
    adapter = tencent(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path, b"RIFF"), name="旁白")
    message = str(excinfo.value)
    assert "00001" in message and "鸟群中有一只小鸟" in message
    assert [c["headers"].get("x-tc-action") for c in cassette.calls] == ["GetTrainingText"]
    adapter.close()


def test_clone_with_text_id_runs_detect_then_create(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add(
            "POST",
            "/",
            json_body=envelope({"Data": {"AudioId": "audio-1", "DetectionCode": 0}}),
            predicate=lambda e: e["headers"].get("x-tc-action") == "DetectEnvAndSoundQuality",
        )
        .add(
            "POST",
            "/",
            json_body=envelope({"Data": {"TaskId": "task-9"}}),
            predicate=lambda e: e["headers"].get("x-tc-action") == "CreateVRSTask",
        )
    )
    adapter = tencent(cassette)
    task = adapter.clone(shirogane_noel(tmp_path, b"RIFF-tx"), name="旁白", text_id="00001")
    assert task.status == STATUS_TRAINING and task.task_id == "task-9"
    detect = json.loads(cassette.calls[0]["body"])
    assert detect["AudioData"] == base64.b64encode(b"RIFF-tx").decode()
    assert detect["TypeId"] == 2 and detect["TaskType"] == 5
    create = json.loads(cassette.calls[1]["body"])
    assert create["AudioIdList"] == ["audio-1"]
    assert create["TaskType"] == 5 and create["VoiceLanguage"] == 1
    assert create["SessionId"]
    for call in cassette.calls:
        assert call["headers"]["authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDEXAMPLE")
        assert "/vrs/tc3_request" in call["headers"]["authorization"]
    adapter.close()


def test_detect_quality_failure_is_review_rejected(tmp_path: pathlib.Path) -> None:
    cassette = route(
        AyaMaruyama(),
        "DetectEnvAndSoundQuality",
        {"Data": {"DetectionCode": -3, "DetectionMsg": "噪声较大不通过"}},
    )
    adapter = tencent(cassette)
    with pytest.raises(ReviewRejectedError) as excinfo:
        adapter.clone(shirogane_noel(tmp_path), name="旁白", text_id="00001")
    assert "噪声" in str(excinfo.value)
    adapter.close()


def test_detect_quality_without_audio_id_explains_type_id(tmp_path: pathlib.Path) -> None:
    """环境检测（TypeId=1）不返回 AudioId，报错要指出这一点。"""
    cassette = route(AyaMaruyama(), "DetectEnvAndSoundQuality", {"Data": {"DetectionCode": 0}})
    adapter = tencent(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.detect_quality(shirogane_noel(tmp_path, b"x"), text_id="t")
    assert "TypeId" in str(excinfo.value)
    adapter.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0, STATUS_TRAINING), (1, STATUS_TRAINING), (2, STATUS_READY), (3, STATUS_FAILED)],
)
def test_clone_status_maps_integer_status(status: int, expected: str) -> None:
    cassette = route(
        AyaMaruyama(),
        "DescribeVRSTaskStatus",
        {"Data": {"Status": status, "StatusStr": "doing", "VoiceType": FAST_VOICE_TYPE,
                  "FastVoiceType": "WCHN-353xxxx0f3eace0c1", "ErrorMsg": ""}},
    )
    adapter = tencent(cassette)
    task = adapter.clone_status("task-9")
    assert task.status == expected
    if expected == STATUS_READY:
        assert task.voice_id == "WCHN-353xxxx0f3eace0c1"  # 一句话版真正的音色 ID
    else:
        assert task.voice_id is None
    adapter.close()


def test_synthesize_passes_both_fields_for_a_fast_voice() -> None:
    cassette = route(
        AyaMaruyama(),
        "TextToVoice",
        {"Audio": base64.b64encode(b"TX-MP3").decode()},
    )
    adapter = tencent(cassette)
    result = adapter.synthesize("你好", voice=VoiceRef("WCHN-353xxxx0f3eace0c1"))
    assert result.audio == b"TX-MP3"
    body = json.loads(cassette.calls[0]["body"])
    assert body["VoiceType"] == FAST_VOICE_TYPE
    assert body["FastVoiceType"] == "WCHN-353xxxx0f3eace0c1"
    assert "/tts/tc3_request" in cassette.calls[0]["headers"]["authorization"]
    assert cassette.calls[0]["headers"]["x-tc-action"] == "TextToVoice"
    adapter.close()


def test_synthesize_uses_numeric_voice_type_for_classic_voices() -> None:
    cassette = route(AyaMaruyama(), "TextToVoice", {"Audio": base64.b64encode(b"X").decode()})
    adapter = tencent(cassette)
    adapter.synthesize("hi", voice=VoiceRef("101001"))
    body = json.loads(cassette.calls[0]["body"])
    assert body["VoiceType"] == 101001
    assert "FastVoiceType" not in body
    adapter.close()


@pytest.mark.parametrize(
    ("text", "ok"),
    [("字" * 150, True), ("字" * 151, False), ("a" * 500, True), ("a" * 501, False)],
)
def test_synthesize_char_limits_split_by_script(text: str, ok: bool) -> None:
    cassette = route(AyaMaruyama(), "TextToVoice", {"Audio": base64.b64encode(b"X").decode()})
    adapter = tencent(cassette)
    if ok:
        assert adapter.synthesize(text, voice=VoiceRef("101001")).audio == b"X"
    else:
        with pytest.raises(ProviderError) as excinfo:
            adapter.synthesize(text, voice=VoiceRef("101001"))
        assert "上限" in str(excinfo.value)
    adapter.close()


def test_synthesize_stream_without_sdk_app_id_is_actionable() -> None:
    """流式要 TRTC 的 SdkAppId；缺了要说清楚缺什么、以及怎么绕开。"""
    adapter = tencent(AyaMaruyama())
    with pytest.raises(ProviderError) as excinfo:
        adapter.synthesize("你好", voice=VoiceRef("1"), stream=True)
    message = str(excinfo.value)
    assert "SdkAppId" in message and "stream=True" in message
    adapter.close()


def flowtts_cassette(*events: dict) -> AyaMaruyama:
    body = b"".join(f"data: {json.dumps(event)}\n".encode() for event in events)
    return AyaMaruyama().add("POST", "/", content=body)


def test_flowtts_sse_streams_chunks_from_the_ai_domain() -> None:
    """SSE 必须用 trtc.ai.tencentcloudapi.com——官方逐字："否则会调用接口失败"。"""
    cassette = flowtts_cassette(
        {"Type": "chunk", "Audio": base64.b64encode(b"A").decode(), "Seq": 0, "IsEnd": False},
        {"Type": "chunk", "Audio": base64.b64encode(b"B").decode(), "Seq": 1, "IsEnd": True},
    )
    adapter = tencent(cassette)
    adapter.configure({"sdk_app_id": 160001234})
    result = adapter.synthesize("你好", voice=VoiceRef("v-female-x"), stream=True)
    assert result.stream is True
    assert list(result.iter_bytes()) == [b"A", b"B"]
    call = cassette.calls[0]
    assert call["headers"]["x-tc-action"] == "TextToSpeechSSE"
    assert call["headers"]["host"] == "trtc.ai.tencentcloudapi.com"
    assert "/trtc/tc3_request" in call["headers"]["authorization"]  # service 是 trtc
    body = json.loads(call["body"])
    assert body["SdkAppId"] == 160001234
    assert body["Voice"]["VoiceId"] == "v-female-x"
    adapter.close()


def test_flowtts_sse_error_frame_raises_on_iteration() -> None:
    cassette = flowtts_cassette(
        {"Type": "error", "Error": {"Code": "InvalidParameter.VoiceId", "Message": "voice not found"}}
    )
    adapter = tencent(cassette)
    adapter.configure({"sdk_app_id": 1})
    result = adapter.synthesize("你好", voice=VoiceRef("bad"), stream=True)
    with pytest.raises(ProviderError) as excinfo:
        list(result.iter_bytes())
    assert excinfo.value.code == "InvalidParameter.VoiceId"
    assert "voice not found" in str(excinfo.value)
    adapter.close()


def test_flowtts_stream_uses_its_own_char_limit() -> None:
    """流式上限是 20000，不是 TextToVoice 的 150/500——两者不能混用。"""
    cassette = flowtts_cassette({"Type": "chunk", "Audio": "", "IsEnd": True})
    adapter = tencent(cassette)
    adapter.configure({"sdk_app_id": 1})
    assert adapter.synthesize("字" * 600, voice=VoiceRef("v"), stream=True).stream is True
    adapter.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AuthFailure.SignatureFailure", AuthError),
        ("AuthFailure.SecretIdNotFound", AuthError),
        ("RequestLimitExceeded", QuotaError),
        ("LimitExceeded.ConcurrencyLimit", QuotaError),
        ("UnsupportedOperation.VRSQuotaExhausted", QuotaError),
        ("FailedOperation.VoiceNotQualified", ReviewRejectedError),
        ("FailedOperation.NoSuchTask", ProviderError),
        ("SomethingBrand.New", ProviderError),
    ],
)
def test_error_codes_map_to_normalized_errors(code: str, expected: type[TTSHubError]) -> None:
    cassette = AyaMaruyama().add(
        "POST",
        "/",
        json_body={"Response": {"Error": {"Code": code, "Message": "boom"}, "RequestId": "r"}},
    )
    adapter = tencent(cassette)
    with pytest.raises(expected) as excinfo:
        adapter.clone_status("task-9")
    assert excinfo.value.code == code
    assert excinfo.value.vendor == "tencent"
    adapter.close()


def test_missing_response_envelope_is_reported() -> None:
    cassette = AyaMaruyama().add("POST", "/", json_body={"unexpected": True})
    adapter = tencent(cassette)
    with pytest.raises(ProviderError) as excinfo:
        adapter.clone_status("task-9")
    assert "Response" in str(excinfo.value)
    adapter.close()


def test_list_voices_merges_both_task_types() -> None:
    cassette = (
        AyaMaruyama()
        .add(
            "POST",
            "/",
            json_body=envelope({"Data": {"VoiceTypeList": [
                {"VoiceType": 101001, "VoiceName": "基础版音色", "DateCreated": "2026-01-01"}
            ]}}),
            predicate=lambda e: json.loads(e["body"]).get("TaskType") == 0,
        )
        .add(
            "POST",
            "/",
            json_body=envelope({"Data": {"VoiceTypeList": [
                {"VoiceType": FAST_VOICE_TYPE, "FastVoiceType": "WCHN-abc", "VoiceName": "一句话音色"}
            ]}}),
            predicate=lambda e: json.loads(e["body"]).get("TaskType") == 5,
        )
    )
    adapter = tencent(cassette)
    voices = {v.voice_id: v for v in adapter.list_voices()}
    assert set(voices) == {"101001", "WCHN-abc"}
    assert voices["WCHN-abc"].note == "一句话版"
    adapter.close()
