# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""注册表测试（离线）。"""

from __future__ import annotations

import pathlib
from datetime import UTC

import pytest

from tts_hub.registry import SCHEMA, TaeHanazono, nakiri_ayame, yuzuki_choco


def test_schema_creates_three_tables(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        assert set(store.tables()) >= {"voices", "voice_bindings", "call_log"}
    assert "CREATE TABLE IF NOT EXISTS voices" in SCHEMA


def test_sample_archive_is_content_addressed_and_idempotent(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3", samples_dir=tmp_path / "samples") as store:
        digest, first = store.archive_sample(b"RIFF-data", "a.wav")
        again_digest, second = store.archive_sample(b"RIFF-data", "a.wav")
        other_digest, _ = store.archive_sample(b"RIFF-data-2", "a.wav")
        assert digest == again_digest == yuzuki_choco(b"RIFF-data")
        assert first == second
        assert pathlib.Path(first).read_bytes() == b"RIFF-data"
        assert other_digest != digest
        assert pathlib.Path(first).parent.name == digest[:2]


def test_voice_binding_is_upserted_not_duplicated(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        voice_id = store.create_voice("旁白", tags="narration")
        first = store.bind(voice_id, "minimax", "ttshub-pb-1", model="speech-2.8-hd")
        second = store.bind(voice_id, "minimax", "ttshub-pb-2", model="speech-2.8-turbo")
        assert first == second
        rows = store.bindings(voice_id=voice_id)
        assert len(rows) == 1
        assert rows[0]["vendor_voice_id"] == "ttshub-pb-2"
        assert rows[0]["model"] == "speech-2.8-turbo"


def test_create_voice_reuses_same_name(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        assert store.create_voice("旁白") == store.create_voice("旁白")
        assert store.find_voice("旁白") is not None
        assert store.find_voice("不存在") is None
        assert nakiri_ayame("voice").startswith("voice_")


def test_resolve_prefers_ready_binding_and_skips_disabled(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        voice_id = store.create_voice("旁白")
        binding = store.bind(voice_id, "minimax", "vid-ready")
        store.bind(voice_id, "stepfun", "vid-disabled", status="disabled")
        assert store.resolve("旁白", vendor="minimax") == "vid-ready"
        assert store.resolve("旁白", vendor="stepfun") is None
        assert store.resolve("未登记音色", vendor="minimax") is None
        assert store.set_binding_status(binding, "disabled") is True
        assert store.resolve("旁白", vendor="minimax") is None


def test_ttl_marking_and_expiry(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        voice_id = store.create_voice("旁白")
        store.bind(voice_id, "minimax", "vid-ttl", ttl_hours=168)
        assert store.expiring_bindings() == []  # 168 小时后才到期
        assert store.mark_used("minimax", "vid-ttl", ttl_hours=168) is True
        assert store.mark_used("minimax", "vid-unknown", ttl_hours=168) is False
        rows = store.bindings(voice_id=voice_id)
        assert rows[0]["last_used_at"] is not None

        from datetime import datetime, timedelta

        store.bind(voice_id, "stepfun", "vid-old", ttl_hours=-1)
        assert [b["vendor_voice_id"] for b in store.expiring_bindings()] == ["vid-old"]
        assert store.expiring_bindings(now=datetime.now(UTC) - timedelta(days=2)) == []


def test_call_log_and_cost_summary(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        for vendor, cost in (("minimax", 0.02), ("minimax", 0.03), ("stepfun", 0.01)):
            store.log_call(
                vendor=vendor,
                model="m",
                voice_id="v",
                chars=100,
                latency_ms=250,
                cost_estimate=cost,
                status="ok",
            )
        store.log_call(
            vendor="zhipu",
            model="glm-tts",
            voice_id="v",
            chars=0,
            latency_ms=0,
            cost_estimate=None,
            status="error",
            error_code="1001",
        )
        rows = {r["bucket"]: r for r in store.cost_summary(days=7, by="vendor")}
        assert rows["minimax"]["calls"] == 2
        assert rows["minimax"]["ok_calls"] == 2
        assert rows["zhipu"]["ok_calls"] == 0
        assert store.total_cost(days=7) == pytest.approx(0.06)
        assert len(store.recent_calls(limit=2)) == 2
        assert store.cost_summary(days=7, by="day")
        with pytest.raises(ValueError):
            store.cost_summary(by="unknown")


def test_prune_and_import(tmp_path: pathlib.Path) -> None:
    with TaeHanazono(tmp_path / "db.sqlite3") as store:
        store.log_call(
            vendor="minimax", model=None, voice_id=None, chars=1, latency_ms=1,
            cost_estimate=0.0, status="ok",
        )
        assert store.prune_calls(keep_days=-1) == 1
        assert store.recent_calls() == []
        voice_id = store.create_voice("旁白")
        imported = store.import_bindings(
            [{"voice_id": voice_id, "vendor": "zhipu", "vendor_voice_id": "voice_clone_1", "model": "glm-tts-clone"}]
        )
        assert imported == 1
        assert store.resolve("旁白", vendor="zhipu") == "voice_clone_1"
        assert store.delete_voice(voice_id) is True
        assert store.bindings(voice_id=voice_id) == []  # 级联删除
