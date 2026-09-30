# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""回归测试（离线）。"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient
from support import AyaMaruyama, shiranui_flare
from test_p4_huawei import PROJECT, VOICES_PATH, huawei, token_route

from tts_hub.cli import _disp_width, _pad, murasaki_shion
from tts_hub.core.errors import ProviderError
from tts_hub.core.net import MocaAoba, akai_haato
from tts_hub.core.types import STATUS_READY, SampleInput
from tts_hub.providers import ArisaIchigaya, NyamuYutenji
from tts_hub.registry import TaeHanazono
from tts_hub.registry.store import SCHEMA
from tts_hub.server import create_app

if TYPE_CHECKING:
    from typing import Any


def aliyun_hub(tmp_path: pathlib.Path, cassette: AyaMaruyama):
    return shiranui_flare(
        tmp_path, cassette, vendors=("aliyun",), env={"ALIYUN_API_KEY": "k"}
    )


ENROLL_PATH = "/api/v1/services/audio/tts/customization"


def test_hub_clone_passes_public_url_through_for_url_only_vendors(
    tmp_path: pathlib.Path, cassette: AyaMaruyama
) -> None:
    """归档照做（本地留审计副本），但上送的仍是原始 URL——物化成字节等于堵死
    CosyVoice 唯一可行的复刻通道（修复前 hub 恒传 data，克隆 100% 失败）。"""
    cassette.add("GET", "/a.wav", content=b"RIFF-url-sample")
    cassette.add(
        "POST", ENROLL_PATH, json_body={"output": {"voice_id": "cosy-test", "status": "OK"}}
    )
    hub = aliyun_hub(tmp_path, cassette)
    try:
        outcome = hub.clone(
            SampleInput.from_url("https://8.8.8.8/a.wav"), vendor="aliyun", name="urlpass"
        )
        assert outcome["task"].status in (STATUS_READY, "training")
        enroll = json.loads(cassette.calls[-1]["body"])
        assert enroll["input"]["url"] == "https://8.8.8.8/a.wav"
        assert enroll["input"]["action"] == "create_voice"
        binding = hub.registry.bindings(voice_id=outcome["voice_id"])[0]
        assert binding["sample_hash"]
        assert pathlib.Path(binding["sample_path"]).is_file()
    finally:
        hub.close()


def test_hub_clone_local_file_for_url_only_vendor_still_explains_the_alternative(
    tmp_path: pathlib.Path, cassette: AyaMaruyama
) -> None:
    hub = aliyun_hub(tmp_path, cassette)
    sample = tmp_path / "local.wav"
    sample.write_bytes(b"RIFF-local")
    try:
        with pytest.raises(ProviderError) as excinfo:
            hub.clone(SampleInput.from_path(sample), vendor="aliyun", name="localpass")
        assert "qwen3-tts-vc" in str(excinfo.value)
    finally:
        hub.close()


def test_hub_clone_rejects_missing_or_ambiguous_samples(tmp_path: pathlib.Path) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama(), env={"MINIMAX_API_KEY": "k"})
    try:
        with pytest.raises(ProviderError, match="恰好提供"):
            hub.clone(SampleInput(), vendor="minimax", name="empty")
        with pytest.raises(ProviderError, match="恰好提供"):
            hub.clone(SampleInput(path=tmp_path / "x.wav", data=b"x"), vendor="minimax", name="dup")
    finally:
        hub.close()


def _failing_transport(message: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(message, request=request)

    return httpx.MockTransport(handler)


def test_stream_bytes_normalizes_network_failures_and_redacts_secrets() -> None:
    secret = "STREAM-SECRET-VALUE-0123456789"
    client = MocaAoba(
        transport=_failing_transport(f"connect failed: https://x/sse?key={secret}"),
        secrets=[secret],
    )
    with pytest.raises(ProviderError) as excinfo:
        list(client.stream_bytes("POST", "https://8.8.8.8/sse"))
    message = str(excinfo.value)
    assert secret not in message
    assert "***" in message
    client.close()


def test_download_aborts_before_reading_past_the_cap(cassette: AyaMaruyama) -> None:
    payload = b"x" * 4096
    cassette.add("GET", "/big", content=payload)
    client = cassette.client()
    with pytest.raises(ProviderError, match="上限"):
        client.download("http://8.8.8.8/big", max_bytes=1024)
    assert client.download("http://8.8.8.8/big", max_bytes=8192) == payload
    client.close()


def test_host_guard_blocks_foreign_hosts_but_allows_loopback(
    tmp_path: pathlib.Path, cassette: AyaMaruyama
) -> None:
    hub = shiranui_flare(tmp_path, cassette)
    client = TestClient(create_app(hub=hub), base_url="http://127.0.0.1:8000")
    try:
        assert client.get("/api/health").status_code == 200  # base_url 即回环
        foreign = client.get("/api/health", headers={"Host": "evil.example.com"})
        assert foreign.status_code == 421
        assert foreign.json()["error"]["code"] == "host_not_allowed"
        assert client.get("/api/health", headers={"Host": "localhost:8000"}).status_code == 200
    finally:
        hub.close()


def test_host_guard_steps_aside_when_listening_beyond_loopback(tmp_path: pathlib.Path) -> None:
    """显式改听局域网说明换了保护模型，此闸不再拦（README 有对应告警）。"""
    hub = shiranui_flare(tmp_path, AyaMaruyama(), host="0.0.0.0")
    client = TestClient(create_app(hub=hub), base_url="http://127.0.0.1:8000")
    try:
        assert client.get("/api/health", headers={"Host": "my-lan.example"}).status_code == 200
    finally:
        hub.close()


def test_stepfun_clone_rejects_preview_text_instead_of_ignoring_it() -> None:
    adapter = ArisaIchigaya("k", http=AyaMaruyama().client())
    try:
        with pytest.raises(ProviderError) as excinfo:
            adapter.clone(SampleInput(data=b"x", filename="a.mp3"), preview_text="试试")
        assert "voices-preview" in str(excinfo.value)
    finally:
        adapter.close()


def _volcengine_clone_body(cassette: AyaMaruyama, **kw: Any) -> dict[str, Any]:
    cassette.add(
        "POST", "/api/v3/tts/voice_clone", json_body={"status": 2, "speaker_id": "S_test"}
    )
    adapter = NyamuYutenji("volc-key", http=cassette.client())
    try:
        adapter.clone(SampleInput(data=b"RIFF", filename="a.wav"), speaker_id="S_test", **kw)
        body: dict[str, Any] = json.loads(cassette.calls[0]["body"])
        return body
    finally:
        adapter.close()


def test_volcengine_demo_text_prefers_preview_then_transcript(cassette: AyaMaruyama) -> None:
    body = _volcengine_clone_body(cassette, name="旁白", preview_text="试听这句话")
    assert body["extra_params"]["demo_text"] == "试听这句话"
    assert body["extra_params"]["demo_text"] != "旁白"


def test_volcengine_demo_text_falls_back_to_transcript_not_the_name(
    cassette: AyaMaruyama,
) -> None:
    body = _volcengine_clone_body(cassette, name="旁白", transcript="样本里念的内容")
    assert body["extra_params"]["demo_text"] == "样本里念的内容"


def test_volcengine_omits_demo_text_when_nothing_sensible_is_available(
    cassette: AyaMaruyama,
) -> None:
    body = _volcengine_clone_body(cassette, name="旁白")
    assert "extra_params" not in body


MINIMAX_OK = {"data": {"audio": "4869"}, "base_resp": {"status_code": 0}}


def test_speak_logs_the_same_cost_formula_as_the_failure_branch(
    tmp_path: pathlib.Path, cassette: AyaMaruyama
) -> None:
    cassette.add("POST", "/v1/t2a_v2", json_body=MINIMAX_OK)
    hub = shiranui_flare(tmp_path, cassette, env={"MINIMAX_API_KEY": "k"})
    try:
        hub.speak("你好世界", voice="mm-voice", vendor="minimax", model="speech-2.8-turbo")
        row = hub.registry.recent_calls()[0]
        expected = hub.pricing.synth_cost("minimax", "speech-2.8-turbo", "你好世界")
        assert row["cost_estimate"] == pytest.approx(expected)
        assert row["status"] == "ok"
    finally:
        hub.close()


def test_unique_voice_name_migration_renames_duplicates_and_keeps_bindings(
    tmp_path: pathlib.Path,
) -> None:
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO voices (id, name, tags, created_at) VALUES ('voice_aaa', '旁白', '', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO voices (id, name, tags, created_at) VALUES ('voice_bbb', '旁白', '', '2026-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO voice_bindings (id, voice_id, vendor, vendor_voice_id, status, created_at)"
        " VALUES ('bind_bbb', 'voice_bbb', 'minimax', 'mm1', 'ready', '2026-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = TaeHanazono(db)
    try:
        assert store.find_voice("旁白") == "voice_aaa"
        assert store.find_voice("旁白~bbb") == "voice_bbb"
        binding = store.bindings(voice_id="voice_bbb")
        assert binding and binding[0]["id"] == "bind_bbb"
        assert store.create_voice("旁白") == "voice_aaa"
        index = store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_voices_name_unique'"
        ).fetchone()
        assert index is not None
    finally:
        store.close()


def test_concurrent_same_name_create_voice_neither_leaks_transactions_nor_errors(
    tmp_path: pathlib.Path,
) -> None:
    """并发同名创建：撞 UNIQUE 的路径必须 rollback（否则挂起的隐式事务会让
    并发写者的 commit 饿死在 busy timeout 上，实测 8 线程炸 6 个）。"""
    store = TaeHanazono(tmp_path / "race.sqlite3")
    barrier = threading.Barrier(8)
    errors: list[str] = []
    ids: set[str] = set()
    leak: list[bool] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            ids.add(store.create_voice("旁白"))
            leak.append(store._conn.in_transaction)
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == [], f"并发创建不应有异常逃逸：{errors}"
        assert len(ids) == 1, "同名并发应复用同一条逻辑音色"
        assert not any(leak), "每个调用结束后连接上不得残留未提交事务"
    finally:
        store.close()


def test_concurrent_bind_on_same_voice_and_vendor_stays_unique(
    tmp_path: pathlib.Path,
) -> None:
    """并发对同一 (音色, 厂商) bind：撞 UNIQUE 后转更新路径，不得裸抛、不得产生第二条。"""
    store = TaeHanazono(tmp_path / "bindrace.sqlite3")
    voice_id = store.create_voice("旁白")
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            store.bind(voice_id, "minimax", f"mm-{i}")
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == [], f"并发 bind 不应有异常逃逸：{errors}"
        assert len(store.bindings(voice_id=voice_id)) == 1, "UNIQUE 约束下只应有一条绑定"
    finally:
        store.close()


def test_tencent_cjk_limit_also_covers_kana() -> None:
    """腾讯 150 字口径应命中假名（原区段在 303F 与 3400 之间漏了 \\u3040-\\u30ff）。"""
    from tts_hub.providers.tencent import _CJK_RE

    assert _CJK_RE.search("你好")
    assert _CJK_RE.search("こんにちは")  # 纯假名：原实现误判为英文按 500 放行
    assert _CJK_RE.search("テスト文化")
    assert not _CJK_RE.search("hello world")


def test_huawei_clone_status_pages_past_the_first_hundred(cassette: AyaMaruyama) -> None:
    token_route(cassette)
    first_page = [{"voice_name": f"voice_{n:03d}"} for n in range(100)]
    cassette.add(
        "GET",
        VOICES_PATH,
        json_body={"result": {"voices": first_page}},
        predicate=lambda entry: entry["query"].get("offset") == "0",
    )
    cassette.add(
        "GET",
        VOICES_PATH,
        json_body={"result": {"voices": [{"voice_name": "target"}]}},
        predicate=lambda entry: entry["query"].get("offset") == "100",
    )
    adapter = huawei(cassette, project_id=PROJECT)
    try:
        task = adapter.clone_status("target")
        assert task.status == STATUS_READY
        assert task.voice_id == "target"
    finally:
        adapter.close()


def test_akai_haato_keeps_multibyte_characters_split_across_chunks() -> None:
    line = 'data: {"m":"你好"}'
    raw = line.encode("utf-8") + b"\n"
    start = raw.index("好".encode())
    cut = start + 1  # 劈在"好"的三字节 UTF-8 序列中间
    assert list(akai_haato([raw[:cut], raw[cut:]])) == [line]


def test_cli_table_alignment_accounts_for_cjk_width() -> None:
    assert _disp_width("旁白") == 4  # CJK 占两格
    assert _pad("旁白", 6) == "旁白" + "  "  # 补齐按显示宽度，len() 是 2 会少补
    table = murasaki_shion([("旁白", "mm1")], ("名称", "绑定ID"))
    header, separator, body = table.splitlines()
    assert len(_pad("名称", _disp_width("绑定ID"))) >= 2
    assert separator.count("-") >= _disp_width("名称")
    assert header.startswith("名称")
    assert "旁白" in body
