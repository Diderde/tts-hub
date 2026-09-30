# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""管理台测试（离线，走录播传输层 + TestClient）。"""

from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from support import AyaMaruyama, shiranui_flare

from tts_hub.server import ASSETS, WEB_DIR, create_app
from tts_hub.server.console import _parse_days

MINIMAX_OK = {"data": {"audio": "4869", "status": 2}, "base_resp": {"status_code": 0}}


def client_for(tmp_path: pathlib.Path, cassette: AyaMaruyama, **overrides) -> TestClient:
    """建一个注入了录播传输层的管理台客户端。

    ``base_url`` 指到回环地址：Host 头校验只放行 127.0.0.1/localhost，
    TestClient 缺省的 ``testserver`` 会被 421 拒掉。
    """
    env = overrides.pop("env", {"STEPFUN_API_KEY": "k", "MINIMAX_API_KEY": "k"})
    vendors = overrides.pop("vendors", ("minimax", "stepfun"))
    hub = shiranui_flare(tmp_path, cassette, env=env, vendors=vendors, **overrides)
    return TestClient(create_app(hub=hub), base_url="http://127.0.0.1:8000")


def two_vendor_cassette() -> AyaMaruyama:
    """两家都能合成：阶跃走普通 POST，MiniMax 回 base64 音频。"""
    return (
        AyaMaruyama()
        .add("POST", "/v1/audio/speech", content=b"STEPFUN-AUDIO")
        .add("POST", "/v1/t2a_v2", json_body=MINIMAX_OK)
    )


def clone_cassette() -> AyaMaruyama:
    """阶跃复刻：先传文件拿 id，再建音色。"""
    return (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-abc"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-cloned-1"})
    )


def console_api_paths() -> set[str]:
    """把 console.js 里 fetch 的 ``/api/...`` 路径抠出来（含模板串，参数名归一）。"""
    text = (WEB_DIR / "console.js").read_text(encoding="utf-8")
    paths: set[str] = set()
    for raw in re.findall(r"[\"'`](/api/[^\"'`]*)[\"'`]", text):
        without_query = raw.split("?")[0]
        paths.add(re.sub(r"\$\{[^}]*\}", "{param}", without_query))
    return paths


def test_console_page_is_served_at_root(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    for marker in ("调音台", "音色库", "成本看板", "调用日志", "/assets/console.js"):
        assert marker in body


def test_console_page_is_not_in_the_api_schema(tmp_path: pathlib.Path) -> None:
    """页面不是 API：不该出现在 /openapi.json 里（否则调用方会以为它是契约）。"""
    client = client_for(tmp_path, AyaMaruyama())
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths
    assert "/assets/{name}" not in paths


def test_console_assets_are_whitelisted(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    js = client.get("/assets/console.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert "function el(" in js.text

    css = client.get("/assets/console.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    assert client.get("/assets/index.html").status_code == 404
    assert client.get("/assets/console.py").status_code == 404
    assert client.get("/assets/%2e%2e%2fpyproject.toml").status_code == 404


def test_web_assets_are_present_and_declared() -> None:
    """页面资源必须真的在包里：wheel 少带一个文件，管理台就是白屏。"""
    for name in ("index.html", "console.js", "console.css"):
        assert (WEB_DIR / name).is_file(), f"缺少管理台资源：{name}"
    declared = {filename for filename, _ in ASSETS.values()}
    on_disk = {p.name for p in WEB_DIR.iterdir() if p.is_file()}
    assert on_disk - {"index.html"} == declared


def test_console_only_calls_endpoints_that_actually_exist(tmp_path: pathlib.Path) -> None:
    """前端 fetch 的每个 /api 路径都必须真的在 OpenAPI 里。

    这是**防前后端漂移**的闸门：页面里写错一个路径，浏览器只会安静地 404，
    单测不盯着就没人会发现。参数名不同不算错（``{id}`` 与 ``{voice_id}`` 归一后比）。
    """
    known = set(client_for(tmp_path, AyaMaruyama()).get("/openapi.json").json()["paths"])

    def shape(path: str) -> str:
        return re.sub(r"\{[^}]*\}", "{}", path)

    known_shapes = {shape(path) for path in known}
    missing = [path for path in console_api_paths() if shape(path) not in known_shapes]
    assert missing == [], f"管理台调了不存在的接口：{missing}"


def test_console_page_only_loads_declared_assets() -> None:
    """页面里引用的 /assets/<名> 必须在白名单里，否则页面自己就会 404。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r"/assets/([A-Za-z0-9._-]+)", html))
    assert referenced == set(ASSETS), f"页面引用与白名单不一致：{referenced} vs {set(ASSETS)}"


def test_every_element_id_the_script_looks_up_exists() -> None:
    """JS 里 ``$("x")`` 的每个 id 都必须在 HTML 里。

    浏览器不会因为 id 打错而报错，只会在取 `null.textContent` 时炸在某个回调里——
    表现出来就是"点了没反应"。这类漂移只能靠扫一遍来防。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    script = (WEB_DIR / "console.js").read_text(encoding="utf-8")
    declared = set(re.findall(r'id="([^"]+)"', html))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', script))
    assert wanted - declared == set(), f"脚本找了不存在的元素 id：{sorted(wanted - declared)}"

    for panel in re.findall(r'data-panel="([^"]+)"', html):
        assert f'id="panel-{panel}"' in html, f"页签 {panel} 没有对应的面板"


def test_missing_asset_reports_actionable_error(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """资源缺失时给出能照着修的消息，而不是一句 500 Internal Server Error。"""
    import tts_hub.server.console as console

    monkeypatch.setattr(console, "WEB_DIR", tmp_path / "nowhere")
    client = client_for(tmp_path / "hub", AyaMaruyama())
    response = client.get("/")
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "console_missing"
    assert "web" in error["message"]


def test_voice_detail_lists_bindings_samples_and_default(tmp_path: pathlib.Path) -> None:
    cassette = clone_cassette()
    client = client_for(tmp_path, cassette)
    created = client.post(
        "/api/clone",
        data={"name": "旁白", "vendor": "stepfun"},
        files={"file": ("sample.wav", b"RIFF-wave", "audio/wav")},
    ).json()
    voice_id = created["voice_id"]

    detail = client.get(f"/api/voices/{voice_id}").json()
    assert detail["name"] == "旁白"
    assert detail["preferred_vendor"] is None
    assert [b["vendor"] for b in detail["bindings"]] == ["stepfun"]
    assert len(detail["samples"]) == 1
    assert detail["samples"][0]["sample_hash"]
    assert detail["samples"][0]["bindings"][0]["vendor"] == "stepfun"


def test_voice_detail_404_for_unknown_id(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    response = client.get("/api/voices/voice_nope")
    assert response.status_code == 404


def test_voice_can_be_renamed_and_tagged(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    voice_id = client.post("/api/voices", json={"name": "草稿", "tags": "test"}).json()["id"]
    updated = client.patch(f"/api/voices/{voice_id}", json={"name": "旁白", "tags": "正式,女声"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "旁白"
    assert updated.json()["tags"] == "正式,女声"

    assert client.patch(f"/api/voices/{voice_id}", json={}).status_code == 400


def test_binding_can_be_added_and_removed(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    voice_id = client.post("/api/voices", json={"name": "旁白"}).json()["id"]

    created = client.post(
        f"/api/voices/{voice_id}/bindings",
        json={"vendor": "stepfun", "vendor_voice_id": "voice-x", "model": "step-tts-mini"},
    )
    assert created.status_code == 201
    binding_id = created.json()["binding_id"]

    detail = client.get(f"/api/voices/{voice_id}").json()
    assert detail["bindings"][0]["vendor_voice_id"] == "voice-x"

    other = client.post("/api/voices", json={"name": "别的"}).json()["id"]
    assert client.delete(f"/api/voices/{other}/bindings/{binding_id}").status_code == 404
    assert client.delete(f"/api/voices/{voice_id}/bindings/{binding_id}").status_code == 200
    assert client.get(f"/api/voices/{voice_id}").json()["bindings"] == []


def test_default_requires_an_existing_binding(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    voice_id = client.post("/api/voices", json={"name": "旁白"}).json()["id"]
    response = client.post(f"/api/voices/{voice_id}/default", json={"vendor": "stepfun"})
    assert response.status_code == 404
    assert "无法设为默认" in response.json()["error"]["message"]


def test_sample_download_rejects_paths_outside_samples_dir(tmp_path: pathlib.Path) -> None:
    """样本路径来自注册表，但"能不能读"不该建立在"上游一定没写坏"的假设上。"""
    client = client_for(tmp_path, AyaMaruyama())
    voice_id = client.post("/api/voices", json={"name": "旁白"}).json()["id"]
    outsider = tmp_path / "outside.wav"
    outsider.write_bytes(b"NOT-A-SAMPLE")

    from support import shiranui_flare

    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"STEPFUN_API_KEY": "k"})
    binding_id = hub.registry.bind(
        voice_id, "stepfun", "voice-x", sample_path=str(outsider), sample_hash="deadbeef"
    )
    client2 = client_for(tmp_path, AyaMaruyama())
    response = client2.get(f"/api/voices/{voice_id}/bindings/{binding_id}/sample")
    assert response.status_code == 404
    assert "样本" in response.json()["error"]["message"]


def test_sample_download_serves_archived_bytes(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, clone_cassette())
    created = client.post(
        "/api/clone",
        data={"name": "旁白", "vendor": "stepfun"},
        files={"file": ("sample.wav", b"RIFF-original", "audio/wav")},
    ).json()
    binding_id = created["binding_id"]
    response = client.get(f"/api/voices/{created['voice_id']}/bindings/{binding_id}/sample")
    assert response.status_code == 200
    assert response.content == b"RIFF-original"


def test_closed_loop_clone_listen_compare_and_set_default(tmp_path: pathlib.Path) -> None:
    """P5 的验收动作：本地不连厂商也能把这个闭环走完。"""
    cassette = clone_cassette().add("POST", "/v1/audio/speech", content=b"STEPFUN-AUDIO").add(
        "POST", "/v1/t2a_v2", json_body=MINIMAX_OK
    )
    client = client_for(tmp_path, cassette)

    created = client.post(
        "/api/clone",
        data={"name": "旁白", "vendor": "stepfun", "transcript": "智能阶跃"},
        files={"file": ("sample.wav", b"RIFF-wave", "audio/wav")},
    ).json()
    voice_id = created["voice_id"]

    client.post(
        f"/api/voices/{voice_id}/bindings",
        json={"vendor": "minimax", "vendor_voice_id": "minimax-voice-x", "model": "speech-2.8-turbo"},
    )

    stepfun = client.post("/api/tts", json={"text": "你好", "voice": voice_id, "vendor": "stepfun"})
    minimax = client.post("/api/tts", json={"text": "你好", "voice": voice_id, "vendor": "minimax"})
    assert stepfun.status_code == minimax.status_code == 200
    assert stepfun.content == b"STEPFUN-AUDIO"
    assert minimax.content == b"Hi"
    assert {stepfun.headers["x-tts-vendor"], minimax.headers["x-tts-vendor"]} == {"stepfun", "minimax"}

    assert (
        client.post(f"/api/voices/{voice_id}/default", json={"vendor": "minimax"}).status_code == 200
    )
    detail = client.get(f"/api/voices/{voice_id}").json()
    assert detail["preferred_vendor"] == "minimax"
    assert next(b["vendor"] for b in detail["bindings"]) == "minimax"  # 首选排在前面

    implicit = client.post("/api/tts", json={"text": "你好", "voice": voice_id})
    assert implicit.status_code == 200
    assert implicit.headers["x-tts-vendor"] == "minimax"
    assert implicit.content == b"Hi"

    client.post(f"/api/voices/{voice_id}/default", json={"vendor": "stepfun"})
    again = client.post("/api/tts", json={"text": "你好", "voice": voice_id})
    assert again.headers["x-tts-vendor"] == "stepfun"


def test_explicit_vendor_beats_the_default(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, two_vendor_cassette())
    voice_id = client.post("/api/voices", json={"name": "旁白"}).json()["id"]
    client.post(f"/api/voices/{voice_id}/bindings", json={"vendor": "stepfun", "vendor_voice_id": "v1"})
    client.post(f"/api/voices/{voice_id}/bindings", json={"vendor": "minimax", "vendor_voice_id": "v2"})
    client.post(f"/api/voices/{voice_id}/default", json={"vendor": "minimax"})

    explicit = client.post("/api/tts", json={"text": "hi", "voice": voice_id, "vendor": "stepfun"})
    assert explicit.headers["x-tts-vendor"] == "stepfun"


def test_default_is_kept_when_rebinding_the_same_vendor(tmp_path: pathlib.Path) -> None:
    """重新绑定同一家（比如复刻重跑）不该把"默认"标记弄丢。"""
    client = client_for(tmp_path, AyaMaruyama())
    voice_id = client.post("/api/voices", json={"name": "旁白"}).json()["id"]
    client.post(f"/api/voices/{voice_id}/bindings", json={"vendor": "stepfun", "vendor_voice_id": "v1"})
    client.post(f"/api/voices/{voice_id}/default", json={"vendor": "stepfun"})
    client.post(f"/api/voices/{voice_id}/bindings", json={"vendor": "stepfun", "vendor_voice_id": "v2"})

    detail = client.get(f"/api/voices/{voice_id}").json()
    assert detail["preferred_vendor"] == "stepfun"
    assert detail["bindings"][0]["vendor_voice_id"] == "v2"


def test_default_does_not_leak_across_voices(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    first = client.post("/api/voices", json={"name": "甲"}).json()["id"]
    second = client.post("/api/voices", json={"name": "乙"}).json()["id"]
    for voice_id in (first, second):
        client.post(f"/api/voices/{voice_id}/bindings", json={"vendor": "stepfun", "vendor_voice_id": "v"})
    client.post(f"/api/voices/{first}/default", json={"vendor": "stepfun"})

    assert client.get(f"/api/voices/{first}").json()["preferred_vendor"] == "stepfun"
    assert client.get(f"/api/voices/{second}").json()["preferred_vendor"] is None


def test_vendor_models_expose_char_limits(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    data = client.get("/api/vendors/stepfun/models").json()
    assert data["vendor"] == "stepfun"
    assert data["models"]
    assert all("char_limit" in model for model in data["models"])

    missing = client.get("/api/vendors/nope/models")
    assert missing.status_code == 404
    assert missing.json()["error"]["kind"] == "provider"


def test_expiring_endpoint_lists_ttl_bindings(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, AyaMaruyama())
    assert client.get("/api/expiring").json()["bindings"] == []

    from support import shiranui_flare

    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"MINIMAX_API_KEY": "k"})
    voice_id = hub.registry.create_voice("会过期的")
    hub.registry.bind(voice_id, "minimax", "mv-1", ttl_hours=-1)  # 已经过期
    rows = client.get("/api/expiring").json()["bindings"]
    assert [r["vendor"] for r in rows] == ["minimax"]


def test_calls_endpoint_filters_by_vendor_status_and_keyword(tmp_path: pathlib.Path) -> None:
    cassette = two_vendor_cassette().add(
        "POST", "/v1/audio/speech", status=500, content=b"boom", once=False
    )
    client = client_for(tmp_path, cassette)
    ok = client.post("/api/tts", json={"text": "hi", "voice": "v", "vendor": "minimax"})
    assert ok.status_code == 200

    all_calls = client.get("/api/calls").json()
    assert all_calls["count"] == 1
    assert all_calls["calls"][0]["vendor"] == "minimax"
    assert all_calls["calls"][0]["status"] == "ok"

    assert client.get("/api/calls?vendor=stepfun").json()["count"] == 0
    assert client.get("/api/calls?status=error").json()["count"] == 0
    assert client.get("/api/calls?q=speech-2").json()["count"] == 1
    assert client.get("/api/calls?q=不存在的关键字").json()["count"] == 0


def test_cost_endpoint_supports_all_three_dimensions(tmp_path: pathlib.Path) -> None:
    client = client_for(tmp_path, two_vendor_cassette())
    client.post("/api/tts", json={"text": "你好", "voice": "v", "vendor": "minimax"})
    for by in ("vendor", "model", "day"):
        data = client.get(f"/api/cost?range=7d&by={by}").json()
        assert data["by"] == by
        assert data["rows"]
        assert data["currency"] == "CNY"
    assert client.get("/api/cost?by=nope").status_code == 400


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7d", 7), ("30", 30), ("0", 1), ("-3", 1), ("abc", 30), ("99999d", 3650)],
)
def test_range_parser_clamps_to_sane_bounds(raw: str, expected: int) -> None:
    assert _parse_days(raw) == expected


def test_preferred_column_is_added_to_an_old_database(tmp_path: pathlib.Path) -> None:
    """老库（没有 preferred 列）打开后自动补列，且历史绑定默认"非首选"。"""
    db_path = tmp_path / "old" / "data" / "registry.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript(
        """
        CREATE TABLE voices (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                             tags TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE voice_bindings (
            id TEXT PRIMARY KEY, voice_id TEXT NOT NULL, vendor TEXT NOT NULL,
            vendor_voice_id TEXT NOT NULL, model TEXT, status TEXT NOT NULL DEFAULT 'ready',
            sample_hash TEXT, sample_path TEXT, created_at TEXT NOT NULL,
            last_used_at TEXT, expires_at TEXT, task_id TEXT, UNIQUE (voice_id, vendor));
        INSERT INTO voices VALUES ('voice_old', '老音色', '', '2026-01-01T00:00:00+00:00');
        INSERT INTO voice_bindings (id, voice_id, vendor, vendor_voice_id, status, created_at)
            VALUES ('bind_old', 'voice_old', 'minimax', 'mv-old', 'ready', '2026-01-01T00:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    hub = shiranui_flare(tmp_path / "old", AyaMaruyama(), env={"MINIMAX_API_KEY": "k"})
    row = hub.registry.bindings(voice_id="voice_old")[0]
    assert row["vendor_voice_id"] == "mv-old"  # 数据没丢
    assert row["preferred"] == 0
    assert hub.registry.preferred_vendor("voice_old") is None
    assert hub.registry.set_preferred_binding("voice_old", "minimax") is True
    assert hub.registry.preferred_vendor("voice_old") == "minimax"
