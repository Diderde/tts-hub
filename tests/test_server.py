# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""出口层测试（离线，走录播传输层 + TestClient）。"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from support import AyaMaruyama, shiranui_flare

from tts_hub.server import MAX_SAMPLE_BYTES, create_app

if TYPE_CHECKING:
    from fastapi import FastAPI

    from tts_hub.hub import TTSHub

AUDIO = b"ID3-server-audio"


def client_for(tmp_path: pathlib.Path, cassette: AyaMaruyama, **overrides) -> TestClient:
    """建一个注入了录播传输层的应用客户端。

    ``base_url`` 指到回环地址：Host 头校验只放行 127.0.0.1/localhost，
    TestClient 缺省的 ``testserver`` 会被 421 拒掉。
    """
    env = overrides.pop("env", {"STEPFUN_API_KEY": "k", "MINIMAX_API_KEY": "k"})
    vendors = overrides.pop("vendors", ("minimax", "stepfun"))
    hub = shiranui_flare(tmp_path, cassette, env=env, vendors=vendors, **overrides)
    return TestClient(create_app(hub=hub), base_url="http://127.0.0.1:8000")


def hub_of(client: TestClient) -> TTSHub:
    """取回应用上的 hub。

    ``TestClient.app`` 的静态类型是 ASGI 可调用体（``Callable``），而运行时装的
    是 ``FastAPI`` 实例、hub 挂在 ``app.state`` 上——这里显式 cast 一次，
    免得每处断言都写一遍 ``type: ignore``。
    """
    app = cast("FastAPI", client.app)
    hub: TTSHub = app.state.hub
    return hub


def stepfun_tts_cassette() -> AyaMaruyama:
    return AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)


def test_health_reports_loopback_only_by_default(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["host"] == "127.0.0.1"
    assert body["exposes_lan"] is False
    assert set(body["vendors"]) == {"minimax", "stepfun"}


def test_tts_returns_audio_with_metadata_headers(tmp_path: pathlib.Path) -> None:
    cassette = stepfun_tts_cassette()
    client = client_for(tmp_path, cassette)
    response = client.post(
        "/api/tts", json={"text": "你好", "voice": "voice-tone-1", "vendor": "stepfun"}
    )
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["x-tts-vendor"] == "stepfun"
    assert response.headers["x-tts-voice"] == "voice-tone-1"
    assert json.loads(cassette.calls[0]["body"])["voice"] == "voice-tone-1"


def test_tts_defaults_to_configured_vendor(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add(
        "POST", "/v1/t2a_v2",
        json_body={"data": {"audio": "4869", "status": 2}, "base_resp": {"status_code": 0}},
    )
    client = client_for(tmp_path, cassette, default_vendor="minimax")
    response = client.post("/api/tts", json={"text": "hi", "voice": "tts-1"})
    assert response.status_code == 200
    assert response.content == b"Hi"
    assert response.headers["x-tts-vendor"] == "minimax"


def test_tts_stream_returns_chunked_audio(tmp_path: pathlib.Path) -> None:
    stream_body = b'data: {"data":{"audio":"414243","status":2},"base_resp":{"status_code":0}}\n'
    cassette = AyaMaruyama().add("POST", "/v1/t2a_v2", content=stream_body)
    client = client_for(tmp_path, cassette, default_vendor="minimax")
    response = client.post("/api/tts/stream", json={"text": "hi", "voice": "tts-1"})
    assert response.status_code == 200
    assert response.content == b"ABC"


@pytest.mark.parametrize(
    ("http_status", "payload", "expected_status", "expected_kind"),
    [
        (200, {"base_resp": {"status_code": 1004, "status_msg": "bad key"}}, 401, "auth"),
        (200, {"base_resp": {"status_code": 2038, "status_msg": "no cloning permission"}}, 401, "auth"),
        (200, {"base_resp": {"status_code": 1008, "status_msg": "no balance"}}, 429, "quota"),
        (200, {"base_resp": {"status_code": 1043, "status_msg": "asr failed"}}, 422, "review"),
        (200, {"base_resp": {"status_code": 1001, "status_msg": "timeout"}}, 502, "provider"),
        (200, {"base_resp": {"status_code": 2013, "status_msg": "invalid params"}}, 502, "provider"),
        (401, {"message": "unauthorized"}, 401, "auth"),
        (429, {"message": "slow down"}, 429, "quota"),
        (503, {"message": "overloaded"}, 502, "provider"),
    ],
)
def test_tts_maps_normalized_errors_to_http_status(
    tmp_path: pathlib.Path,
    http_status: int,
    payload: dict,
    expected_status: int,
    expected_kind: str,
) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/t2a_v2", status=http_status, json_body=payload)
    client = client_for(tmp_path, cassette, default_vendor="minimax")
    response = client.post("/api/tts", json={"text": "hi", "voice": "tts-1"})
    assert response.status_code == expected_status
    error = response.json()["error"]
    assert error["kind"] == expected_kind
    assert error["vendor"] == "minimax"
    assert "message" in error


def test_tts_unknown_vendor_is_provider_error(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post("/api/tts", json={"text": "hi", "voice": "v", "vendor": "nope"})
    assert response.status_code == 502
    assert "nope" in response.json()["error"]["message"]


def test_disabled_vendor_is_refused_then_restored(tmp_path: pathlib.Path) -> None:
    cassette = stepfun_tts_cassette()
    client = client_for(tmp_path, cassette)

    assert client.post("/api/vendors/stepfun/disable").json()["enabled"] is False
    blocked = client.post("/api/tts", json={"text": "hi", "voice": "v", "vendor": "stepfun"})
    assert blocked.status_code == 502
    assert "已停用" in blocked.json()["error"]["message"]

    assert client.post("/api/vendors/stepfun/enable").json()["enabled"] is True
    assert client.post("/api/tts", json={"text": "hi", "voice": "v", "vendor": "stepfun"}).status_code == 200


def test_vendor_toggle_persists_across_hub_instances(tmp_path: pathlib.Path) -> None:
    """启停是运行期状态，落在注册表里；重建 hub 后仍然生效。"""
    client = client_for(tmp_path, stepfun_tts_cassette())
    client.post("/api/vendors/stepfun/disable")
    hub = hub_of(client)
    vendor_state = hub.registry.vendor_states()
    assert vendor_state == {"stepfun": False}

    from support import shiranui_flare as build_hub  # 同一个工厂，新建实例

    rebuilt = build_hub(tmp_path, AyaMaruyama(), env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))
    assert rebuilt.is_vendor_enabled("stepfun") is False
    assert [v["vendor"] for v in rebuilt.vendors() if v["enabled"]] == []
    rebuilt.close()


def test_unknown_vendor_toggle_is_404(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    assert client.post("/api/vendors/nope/disable").status_code == 404


def test_fallback_switches_vendor_on_quota_but_not_on_auth(tmp_path: pathlib.Path) -> None:
    quota = AyaMaruyama().add(
        "POST", "/v1/t2a_v2", json_body={"base_resp": {"status_code": 1008, "status_msg": "no balance"}}
    ).add("POST", "/v1/audio/speech", content=AUDIO)
    client = client_for(tmp_path / "q", quota, fallback=("minimax", "stepfun"), default_vendor="minimax")
    ok = client.post("/api/tts", json={"text": "hi", "voice": "cixingnansheng", "fallback": True})
    assert ok.status_code == 200
    assert ok.headers["x-tts-vendor"] == "stepfun"

    auth = AyaMaruyama().add(
        "POST", "/v1/t2a_v2", json_body={"base_resp": {"status_code": 1004, "status_msg": "bad key"}}
    )
    client2 = client_for(tmp_path / "a", auth, fallback=("minimax", "stepfun"), default_vendor="minimax")
    denied = client2.post("/api/tts", json={"text": "hi", "voice": "v", "fallback": True})
    assert denied.status_code == 401
    assert [c["path"] for c in auth.calls] == ["/v1/t2a_v2"]  # 没有去打扰第二家


def test_tts_rejects_invalid_body_with_uniform_error_shape(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post("/api/tts", json={"voice": "v"})  # 缺 text
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation"
    assert "text" in error["message"]


def test_tts_rejects_unknown_fields(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post("/api/tts", json={"text": "hi", "voice": "v", "unexpected": 1})
    assert response.status_code == 422


def test_clone_with_file_upload(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-abc"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-tone-9"})
    )
    client = client_for(tmp_path, cassette)
    response = client.post(
        "/api/clone",
        data={"name": "旁白", "vendor": "stepfun", "transcript": "智能阶跃"},
        files={"file": ("sample.wav", b"RIFF-wave", "audio/wav")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["voice_id"] == "voice-tone-9"
    assert body["name"] == "旁白"
    assert body["binding_id"]
    binding = hub_of(client).registry.bindings(voice_id=body["voice_id"])[0]
    assert pathlib.Path(binding["sample_path"]).read_bytes() == b"RIFF-wave"


def test_clone_with_url_downloads_through_guard(tmp_path: pathlib.Path) -> None:
    cassette = (
        AyaMaruyama()
        .add("GET", "/s.wav", content=b"REMOTE-WAV")
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-1"})
    )
    client = client_for(tmp_path, cassette)
    response = client.post(
        "/api/clone", data={"name": "远程", "vendor": "stepfun", "url": "http://8.8.8.8/s.wav"}
    )
    assert response.status_code == 200
    assert response.json()["task"]["voice_id"] == "voice-1"


def test_clone_refuses_private_url(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama()
    client = client_for(tmp_path, cassette)
    response = client.post(
        "/api/clone", data={"name": "内网", "vendor": "stepfun", "url": "http://127.0.0.1/secret.wav"}
    )
    assert response.status_code == 502
    assert cassette.calls == []  # 一个请求都不该发出去


def test_clone_without_sample_is_400(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post("/api/clone", data={"name": "空", "vendor": "stepfun"})
    assert response.status_code == 400
    assert "file 或 url" in response.json()["error"]["message"]


def test_clone_with_empty_file_is_400(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post(
        "/api/clone",
        data={"name": "空文件", "vendor": "stepfun"},
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    assert response.status_code == 400
    assert "为空" in response.json()["error"]["message"]


def test_clone_oversized_file_is_413(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post(
        "/api/clone",
        data={"name": "大文件", "vendor": "stepfun"},
        files={"file": ("big.wav", b"\0" * (MAX_SAMPLE_BYTES + 1), "audio/wav")},
    )
    assert response.status_code == 413


def test_clone_status_endpoint(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    body = client.get("/api/clone/voice-tone-1", params={"vendor": "stepfun"}).json()
    assert body["status"] == "ready"
    assert body["voice_id"] == "voice-tone-1"


def test_voice_crud_roundtrip(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())

    created = client.post("/api/voices", json={"name": "旁白", "tags": "narration"})
    assert created.status_code == 201
    voice_id = created.json()["id"]

    listed = client.get("/api/voices").json()["voices"]
    assert [v["name"] for v in listed] == ["旁白"]

    bound = client.post(
        "/api/voices",
        json={"name": "旁白", "vendor": "stepfun", "vendor_voice_id": "voice-tone-1"},
    )
    assert bound.json()["id"] == voice_id  # 同名复用，不新建
    detail = client.get("/api/voices").json()["voices"][0]
    assert detail["bindings"][0]["vendor_voice_id"] == "voice-tone-1"

    assert client.delete(f"/api/voices/{voice_id}").json()["deleted"] is True
    assert client.get("/api/voices").json()["voices"] == []
    assert client.delete(f"/api/voices/{voice_id}").status_code == 404


def test_voice_binding_requires_vendor_voice_id(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.post("/api/voices", json={"name": "x", "vendor": "stepfun"})
    assert response.status_code == 400
    assert "vendor_voice_id" in response.json()["error"]["message"]


def test_voice_remote_listing(tmp_path: pathlib.Path) -> None:
    cassette = AyaMaruyama().add(
        "GET", "/v1/audio/voices", json_body={"object": "list", "data": [{"id": "voice-tone-2"}]}
    )
    client = client_for(tmp_path, cassette)
    body = client.get("/api/voices", params={"remote": "stepfun"}).json()
    assert body["vendor"] == "stepfun"
    assert body["voices"][0]["voice_id"] == "voice-tone-2"


def test_logical_voice_used_through_api(tmp_path: pathlib.Path) -> None:
    """端到端：建逻辑音色 + 绑定，然后只用逻辑名合成（P2 的验收方向）。"""
    cassette = stepfun_tts_cassette()
    client = client_for(tmp_path, cassette)
    client.post(
        "/api/voices",
        json={"name": "旁白", "vendor": "stepfun", "vendor_voice_id": "voice-tone-1"},
    )
    response = client.post("/api/tts", json={"text": "你好", "voice": "旁白", "vendor": "stepfun"})
    assert response.status_code == 200
    assert json.loads(cassette.calls[0]["body"])["voice"] == "voice-tone-1"


def test_vendors_listing_exposes_key_state_and_price(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    rows = {r["vendor"]: r for r in client.get("/api/vendors").json()["vendors"]}
    assert rows["stepfun"]["has_key"] is True
    assert rows["minimax"]["has_key"] is True
    assert "元/万字符" in rows["stepfun"]["pricing"]


def test_cost_reflects_recorded_calls(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, stepfun_tts_cassette())
    client.post("/api/tts", json={"text": "你好世界", "voice": "v", "vendor": "stepfun"})
    body = client.get("/api/cost", params={"range": "7d", "by": "vendor"}).json()
    assert body["days"] == 7
    assert body["rows"][0]["bucket"] == "stepfun"
    assert body["total"] > 0


@pytest.mark.parametrize(("raw", "expected"), [("30d", 30), ("1", 1), ("junk", 7), ("0d", 1), ("99999d", 3650)])
def test_cost_range_parsing(tmp_path: pathlib.Path, raw: str, expected: int) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    assert client.get("/api/cost", params={"range": raw}).json()["days"] == expected


def test_cost_rejects_unknown_grouping(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    assert client.get("/api/cost", params={"by": "voice"}).status_code == 400


def test_openapi_schema_names_are_reader_friendly(tmp_path: pathlib.Path) -> None:
    """内部命名不该出现在接口文档里。"""
    client = client_for(tmp_path, AyaMaruyama())
    schema = client.get("/openapi.json").json()
    names = set(schema["components"]["schemas"])
    assert {"TtsRequest", "VoiceCreateRequest"} <= names
    for name in names:
        assert not name.startswith(("Chisato", "Maya", "Kasumi", "Tae", "Aya", "Hina")), name


def test_documented_paths_are_all_present(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {
        "/api/tts",
        "/api/tts/stream",
        "/api/clone",
        "/api/clone/{task_id}",
        "/api/voices",
        "/api/voices/{voice_id}",
        "/api/vendors",
        "/api/vendors/{vendor}/enable",
        "/api/vendors/{vendor}/disable",
        "/api/cost",
    } <= paths
