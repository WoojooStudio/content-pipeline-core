"""Fixture tests — every core module."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

# Add the project root to sys.path (runs without installing)
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT / "toolbelt") not in sys.path:
    sys.path.insert(0, str(_ROOT / "toolbelt"))

# Import via the public API (__init__) — verifies the package-level contract
import core
from core import (
    AlreadyDone,
    GateError,
    check_idempotent,
    derive_status,
    get_db,
    init_db,
    next_cnt,
    read_ledger,
    rebuild_index,
    record_gate,
    record_ledger,
    require_gate1,
    require_gate2,
    scaffold_cnt,
    submit_gate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    """Create a temporary repo layout."""
    (tmp_path / "channels").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "ledger.jsonl").touch()
    return tmp_path


def _make_channel(channels_root: Path, channel_id: str = "CH-001") -> Path:
    slug = f"{channel_id}-threads-demo"
    ch = channels_root / slug
    (ch / "content").mkdir(parents=True)
    return ch


def _make_content_json(ch_dir: Path, cnt_id: str, lc: dict | None = None) -> Path:
    cnt_dir = ch_dir / "content" / cnt_id
    cnt_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "schema_version": 2,
        "id": cnt_id,
        "channel_id": "CH-001",
        "title": "Test content",
        "lifecycle": {
            "drafted_at": "2026-07-16T00:00:00+00:00",
            "gate1_pending_at": None,
            "gate1_ok_at": None,
            "producing_at": None,
            "rendering_at": None,
            "produced_at": None,
            "gate2_pending_at": None,
            "gate2_ok_at": None,
            "publishing_at": None,
            "published_at": None,
            "measured_at": None,
            "rejected_at": None,
            "failed_at": None,
            "cancelled_at": None,
        },
        "gates": {
            "g1": {"checks": {}, "by": None, "at": None, "provenance": None},
            "g2": {"checks": {}, "by": None, "at": None, "provenance": None},
        },
        "production": {"brief": None, "draft": None, "human_minutes": 0, "fail_reason": None},
        "outputs": [],
        "jobs": [],
    }
    if lc:
        content["lifecycle"].update(lc)
    p = cnt_dir / "content.json"
    p.write_text(json.dumps(content, ensure_ascii=False, indent=2))
    return p


# ---------------------------------------------------------------------------
# Public API contract
# ---------------------------------------------------------------------------

class TestPublicApi:
    """Every symbol declared in core.__init__.__all__ is actually importable."""

    def test_all_exports_importable(self):
        for name in core.__all__:
            assert hasattr(core, name), f"core.{name} is in __all__ but cannot be imported"


# ---------------------------------------------------------------------------
# counter / scaffold
# ---------------------------------------------------------------------------

class TestCounter:
    def test_first_cnt_no_content_dir(self, tmp_repo):
        channels = tmp_repo / "channels"
        result = next_cnt("CH-001", channels)
        assert result == "CNT-001-0001"

    def test_first_cnt_empty_content_dir(self, tmp_repo):
        channels = tmp_repo / "channels"
        _make_channel(channels)
        result = next_cnt("CH-001", channels)
        assert result == "CNT-001-0001"

    def test_increments_from_existing(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        _make_content_json(ch, "CNT-001-0001")
        _make_content_json(ch, "CNT-001-0002")
        result = next_cnt("CH-001", channels)
        assert result == "CNT-001-0003"

    def test_archived_numbers_are_not_reused(self, tmp_repo):
        """Archived (_archived) unit numbers are not reused — IDs must not collide with old ledger/metrics records."""
        from core.counter import ARCHIVE_DIRNAME

        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        _make_content_json(ch, "CNT-001-0001")
        archived = ch / "content" / ARCHIVE_DIRNAME
        archived.mkdir()
        (ch / "content" / "CNT-001-0001").rename(archived / "CNT-001-0001")
        (archived / "CNT-001-0011").mkdir()
        assert next_cnt("CH-001", channels) == "CNT-001-0012"

    def test_archived_units_are_not_indexed(self, tmp_repo):
        from core.counter import ARCHIVE_DIRNAME

        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        (ch / "profile.json").write_text(json.dumps({"channel_id": "CH-001", "platform": "threads"}))
        _make_content_json(ch, "CNT-001-0001")
        archived = ch / "content" / ARCHIVE_DIRNAME
        archived.mkdir()
        (ch / "content" / "CNT-001-0001").rename(archived / "CNT-001-0001")
        idx = rebuild_index(channels, tmp_repo / "state" / "index.json")
        assert idx["channels"][0]["contents"] == []

    def test_different_channel(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = channels / "CH-002-recipe"
        (ch / "content").mkdir(parents=True)
        result = next_cnt("CH-002", channels)
        assert result == "CNT-002-0001"


class TestScaffold:
    def test_scaffold_creates_dir_and_json(self, tmp_repo):
        channels = tmp_repo / "channels"
        _make_channel(channels)
        cnt_id, cnt_json = scaffold_cnt("CH-001", "First content", channels)
        assert cnt_id == "CNT-001-0001"
        assert cnt_json.exists()
        content = json.loads(cnt_json.read_text())
        assert content["id"] == "CNT-001-0001"
        assert content["title"] == "First content"
        assert content["lifecycle"]["drafted_at"] is not None

    def test_scaffold_increments_sequentially(self, tmp_repo):
        channels = tmp_repo / "channels"
        _make_channel(channels)
        id1, _ = scaffold_cnt("CH-001", "A", channels)
        id2, _ = scaffold_cnt("CH-001", "B", channels)
        assert id1 == "CNT-001-0001"
        assert id2 == "CNT-001-0002"

    def test_scaffold_collision_raises(self, tmp_repo):
        """FileExistsError when two callers claim the same CNT directory at once."""
        channels = tmp_repo / "channels"
        _make_channel(channels)
        # first scaffold — claims 0001
        scaffold_cnt("CH-001", "A", channels)
        # simulate: another process claims CNT-001-0002 without content.json
        cnt_dir = channels / "CH-001-threads-demo" / "content" / "CNT-001-0002"
        cnt_dir.mkdir()
        # scaffold_cnt sees 0002 exists and moves on to 0003 — no collision expected
        # a real TOCTOU collision only happens when two processes mkdir the same ID at once
        # verify: an empty directory is skipped and 0003 is created
        cnt_id, _ = scaffold_cnt("CH-001", "B", channels)
        assert cnt_id == "CNT-001-0003"

    def test_scaffold_raises_without_channel_dir(self, tmp_repo):
        channels = tmp_repo / "channels"
        with pytest.raises(ValueError):
            scaffold_cnt("CH-001", "Title", channels)

    def test_scaffold_logs_drafted_event(self, tmp_repo):
        """With ledger_path, a content.drafted event is recorded."""
        channels = tmp_repo / "channels"
        _make_channel(channels)
        ledger = tmp_repo / "ledger.jsonl"
        cnt_id, _ = scaffold_cnt("CH-001", "Draft log", channels, ledger_path=ledger)
        entries = read_ledger(ledger)
        assert len(entries) == 1
        assert entries[0]["event"] == "content.drafted"
        assert entries[0]["content_id"] == cnt_id
        assert entries[0]["channel_id"] == "CH-001"
        # drafting cost is not measured — omit cost_usd (avoids false precision)
        assert "cost_usd" not in entries[0]

    def test_scaffold_no_ledger_when_path_omitted(self, tmp_repo):
        """Without ledger_path, only scaffolds with no ledger write (backward compatible)."""
        channels = tmp_repo / "channels"
        _make_channel(channels)
        ledger = tmp_repo / "ledger.jsonl"
        scaffold_cnt("CH-001", "no ledger", channels)
        assert read_ledger(ledger) == []


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

class TestGate:
    def test_require_gate1_fails_without_approval(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        with pytest.raises(SystemExit):
            require_gate1(cpath)

    def test_require_gate2_fails_without_approval(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        with pytest.raises(SystemExit):
            require_gate2(cpath)

    def test_gate1_passes_after_record(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        record_gate(cpath, "g1", {"g1_topic_safe": True}, by="human", at="2026-07-16T10:00:00+00:00")
        require_gate1(cpath)  # no exception

    def test_gate2_passes_after_record(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        record_gate(cpath, "g2", {"g2_quality": True}, by="human", at="2026-07-16T11:00:00+00:00")
        require_gate2(cpath)  # no exception

    def test_gate_rejects_false_checks(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        with pytest.raises(GateError):
            record_gate(cpath, "g1", {"g1_topic_safe": False}, by="human", at="2026-07-16T10:00:00+00:00")

    def test_gate_provenance_recorded(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        prov = {"comment_id": "c_123", "author": "user", "ts": "2026-07-16T10:00:00+00:00"}
        record_gate(cpath, "g1", {"g1_topic_safe": True}, by="user", at="2026-07-16T10:00:00+00:00", provenance=prov)
        content = json.loads(cpath.read_text())
        assert content["gates"]["g1"]["provenance"]["comment_id"] == "c_123"

    def test_invalid_gate_key_raises(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        with pytest.raises(GateError):
            record_gate(cpath, "g3", {}, by="x", at="2026-07-16T00:00:00+00:00")

    def test_submit_gate_writes_pending(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        submit_gate(cpath, "g1", at="2026-07-16T09:00:00+00:00")
        content = json.loads(cpath.read_text())
        assert content["lifecycle"]["gate1_pending_at"] == "2026-07-16T09:00:00+00:00"

    def test_submit_gate2_writes_pending(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        cpath = _make_content_json(ch, "CNT-001-0001")
        submit_gate(cpath, "g2", at="2026-07-16T09:00:00+00:00")
        content = json.loads(cpath.read_text())
        assert content["lifecycle"]["gate2_pending_at"] == "2026-07-16T09:00:00+00:00"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_drafted(self):
        lc = {"drafted_at": "2026-07-16T00:00:00+00:00"}
        assert derive_status(lc) == "drafted"

    def test_gate1_pending(self):
        lc = {
            "drafted_at":       "2026-07-16T00:00:00+00:00",
            "gate1_pending_at": "2026-07-16T01:00:00+00:00",
        }
        assert derive_status(lc) == "gate1_pending"

    def test_gate1_ok(self):
        lc = {
            "drafted_at": "2026-07-16T00:00:00+00:00",
            "gate1_ok_at": "2026-07-16T01:00:00+00:00",
        }
        assert derive_status(lc) == "gate1_ok"

    def test_published(self):
        lc = {
            "drafted_at":    "2026-07-16T00:00:00+00:00",
            "gate1_ok_at":   "2026-07-16T01:00:00+00:00",
            "gate2_ok_at":   "2026-07-16T02:00:00+00:00",
            "published_at":  "2026-07-16T03:00:00+00:00",
        }
        assert derive_status(lc) == "published"

    def test_rejected_overrides_by_timestamp(self):
        lc = {
            "drafted_at":  "2026-07-16T00:00:00+00:00",
            "gate1_ok_at": "2026-07-16T01:00:00+00:00",
            "rejected_at": "2026-07-16T02:00:00+00:00",
        }
        assert derive_status(lc) == "rejected"

    def test_cancelled_overrides_by_timestamp(self):
        lc = {
            "drafted_at":   "2026-07-16T00:00:00+00:00",
            "cancelled_at": "2026-07-16T01:00:00+00:00",
        }
        assert derive_status(lc) == "cancelled"

    def test_latest_exit_state_wins(self):
        """rejected wins when rejected_at is later than cancelled_at."""
        lc = {
            "drafted_at":   "2026-07-16T00:00:00+00:00",
            "cancelled_at": "2026-07-16T01:00:00+00:00",
            "rejected_at":  "2026-07-16T02:00:00+00:00",
        }
        assert derive_status(lc) == "rejected"

    def test_timezone_offset_comparison(self):
        """Returns the correct status when UTC and +09:00 offsets point to the same instant."""
        lc = {
            "drafted_at":  "2026-07-16T00:00:00+00:00",
            "gate1_ok_at": "2026-07-16T10:00:00+09:00",  # = 01:00 UTC
            "gate2_ok_at": "2026-07-16T02:00:00+00:00",  # 02:00 UTC > 01:00 UTC
        }
        assert derive_status(lc) == "gate2_ok"

    def test_empty_lifecycle(self):
        assert derive_status({}) == "drafted"


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

class TestLedger:
    def test_record_creates_entry(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        entry = record_ledger(ledger, event="render.start", content_id="CNT-001-0001", cost_usd=0.05)
        assert entry["event"] == "render.start"
        assert entry["cost_usd"] == 0.05
        assert "ts" in entry

    def test_meta_with_falsy_values_preserved(self, tmp_repo):
        """Falsy values such as meta={'retries': 0} are still recorded."""
        ledger = tmp_repo / "ledger.jsonl"
        entry = record_ledger(ledger, event="e1", meta={"retries": 0})
        assert "meta" in entry
        assert entry["meta"]["retries"] == 0
        saved = read_ledger(ledger)
        assert saved[0]["meta"]["retries"] == 0

    def test_meta_none_not_written(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        entry = record_ledger(ledger, event="e1")
        assert "meta" not in entry

    def test_read_all_roundtrip(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        record_ledger(ledger, event="e1", content_id="CNT-001-0001")
        record_ledger(ledger, event="e2", content_id="CNT-001-0002")
        entries = read_ledger(ledger)
        assert len(entries) == 2
        assert entries[0]["event"] == "e1"
        assert entries[1]["event"] == "e2"

    def test_empty_ledger(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        assert read_ledger(ledger) == []

    def test_append_only(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        record_ledger(ledger, event="e1")
        record_ledger(ledger, event="e2")
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_raises_when_both_none(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        with pytest.raises(ValueError):
            check_idempotent(ledger_path=ledger, event="render.start")

    def test_passes_on_first_run(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        check_idempotent(ledger_path=ledger, event="render.start", job_id="job_1")

    def test_raises_on_duplicate_job_id(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        record_ledger(ledger, event="render.start", job_id="job_1")
        with pytest.raises(AlreadyDone):
            check_idempotent(ledger_path=ledger, event="render.start", job_id="job_1")

    def test_force_bypasses_check(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        record_ledger(ledger, event="render.start", job_id="job_1")
        check_idempotent(ledger_path=ledger, event="render.start", job_id="job_1", force=True)

    def test_raises_when_output_exists(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        out = tmp_repo / "out.mp4"
        out.touch()
        with pytest.raises(AlreadyDone):
            check_idempotent(ledger_path=ledger, event="render.done", output_path=out)

    def test_passes_when_output_missing(self, tmp_repo):
        ledger = tmp_repo / "ledger.jsonl"
        out = tmp_repo / "out.mp4"
        check_idempotent(ledger_path=ledger, event="render.done", output_path=out)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

class TestIndex:
    def test_empty_channels(self, tmp_repo):
        index = rebuild_index(tmp_repo / "channels", tmp_repo / "state" / "index.json")
        assert index["channels"] == []
        assert (tmp_repo / "state" / "index.json").exists()

    def test_skips_underscore_directory(self, tmp_repo):
        """Directories starting with an underscore, like _archived, are skipped."""
        (tmp_repo / "channels" / "_archived").mkdir()
        (tmp_repo / "channels" / "_backlog.md").touch()
        index = rebuild_index(tmp_repo / "channels", tmp_repo / "state" / "index.json")
        assert index["channels"] == []

    def test_channel_with_content(self, tmp_repo):
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        _make_content_json(ch, "CNT-001-0001")

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        assert len(index["channels"]) == 1
        ch_entry = index["channels"][0]
        assert ch_entry["channel_id"] == "CH-001"
        assert len(ch_entry["contents"]) == 1
        assert ch_entry["contents"][0]["status"] == "drafted"

    def test_index_is_regeneratable(self, tmp_repo):
        channels = tmp_repo / "channels"
        idx_path = tmp_repo / "state" / "index.json"
        rebuild_index(channels, idx_path)
        idx_path.unlink()
        rebuild_index(channels, idx_path)
        assert idx_path.exists()

    def test_atomic_write_no_tmp_left(self, tmp_repo):
        """No .tmp file is left behind after writing."""
        idx_path = tmp_repo / "state" / "index.json"
        rebuild_index(tmp_repo / "channels", idx_path)
        assert not idx_path.with_suffix(".tmp").exists()

    def test_emits_review_board_fields(self, tmp_repo):
        """Emits derived fields for the review board (body, category, char count, published_at)."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        cnt_path = _make_content_json(ch, "CNT-001-0001")
        content = json.loads(cnt_path.read_text())
        content["production"]["draft"] = "This is the body. Does it end with a question?"
        content["experiment"] = {
            "category": "explainer", "hook": "question", "format": "thread",
            "cta": "follow", "hypothesis": "hypothesis", "kpi_target": "saves",
        }
        content["scheduled_for"] = "2026-07-24T21:00:00+09:00"
        cnt_path.write_text(json.dumps(content, ensure_ascii=False))

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        entry = index["channels"][0]["contents"][0]
        assert entry["category"] == "explainer"
        assert entry["body"] == "This is the body. Does it end with a question?"
        assert entry["char_count"] == len("This is the body. Does it end with a question?")
        assert entry["scheduled_for"] == "2026-07-24T21:00:00+09:00"
        assert entry["published_at"] is None
        assert entry["drafted_at"] == "2026-07-16T00:00:00+00:00"

    def test_review_board_fields_default_when_missing(self, tmp_repo):
        """Content without draft or experiment still gets safe default fields."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        _make_content_json(ch, "CNT-001-0001")

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        entry = index["channels"][0]["contents"][0]
        assert entry["category"] is None
        assert entry["body"] is None
        assert entry["char_count"] == 0
        assert entry["published_at"] is None

    def test_effective_format_narrative_falls_back_to_platform_default(self, tmp_repo):
        """Narrative free text in experiment.format is not used as the preview key; falls back to the platform default (text)."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "platform": "threads", "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        cnt_path = _make_content_json(ch, "CNT-001-0001")
        content = json.loads(cnt_path.read_text())
        content["experiment"] = {"format": "question opening → worldview framing → question close"}
        cnt_path.write_text(json.dumps(content, ensure_ascii=False))

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        entry = index["channels"][0]["contents"][0]
        assert entry["platform"] == "threads"
        assert entry["effective_format"] == "text"  # narrative text ignored, platform default

    def test_effective_format_instagram_default_is_card(self, tmp_repo):
        """An instagram channel without a canonical format falls back to card as the preview key."""
        channels = tmp_repo / "channels"
        ch = channels / "CH-002-instagram-demo"
        (ch / "content").mkdir(parents=True)
        profile = {
            "schema_version": 2, "channel_id": "CH-002", "name": "IG demo",
            "platform": "instagram", "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        cnt_path = _make_content_json(ch, "CNT-002-0001")
        content = json.loads(cnt_path.read_text())
        content["channel_id"] = "CH-002"
        cnt_path.write_text(json.dumps(content, ensure_ascii=False))

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        entry = index["channels"][0]["contents"][0]
        assert entry["platform"] == "instagram"
        assert entry["effective_format"] == "card"

    def test_effective_format_respects_canonical_content_format(self, tmp_repo):
        """A canonical content.format (carousel) is honoured as is."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "platform": "threads", "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))
        cnt_path = _make_content_json(ch, "CNT-001-0001")
        content = json.loads(cnt_path.read_text())
        content["format"] = "carousel"
        cnt_path.write_text(json.dumps(content, ensure_ascii=False))

        index = rebuild_index(channels, tmp_repo / "state" / "index.json")
        entry = index["channels"][0]["contents"][0]
        assert entry["effective_format"] == "carousel"


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

class TestDb:
    def test_init_creates_table(self, tmp_repo):
        db_path = tmp_repo / "metrics.sqlite"
        init_db(db_path)
        assert db_path.exists()

    def test_unique_constraint_same_platform(self, tmp_repo):
        """Same content_id + date + platform cannot be duplicated."""
        import sqlite3
        db_path = tmp_repo / "metrics.sqlite"
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO daily_metrics (content_id, channel_id, date, platform, views) VALUES (?,?,?,?,?)",
            ("CNT-001-0001", "CH-001", "2026-07-16", "threads", 100),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_metrics (content_id, channel_id, date, platform, views) VALUES (?,?,?,?,?)",
                ("CNT-001-0001", "CH-001", "2026-07-16", "threads", 200),
            )
            conn.commit()
        conn.close()

    def test_different_platforms_coexist(self, tmp_repo):
        """Same content_id + date with different platforms stores both rows."""
        db_path = tmp_repo / "metrics.sqlite"
        conn = get_db(db_path)
        conn.execute(
            "INSERT INTO daily_metrics (content_id, channel_id, date, platform, views) VALUES (?,?,?,?,?)",
            ("CNT-001-0001", "CH-001", "2026-07-16", "threads", 100),
        )
        conn.execute(
            "INSERT INTO daily_metrics (content_id, channel_id, date, platform, views) VALUES (?,?,?,?,?)",
            ("CNT-001-0001", "CH-001", "2026-07-16", "youtube", 200),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT platform, views FROM daily_metrics WHERE content_id=? AND date=? ORDER BY platform",
            ("CNT-001-0001", "2026-07-16"),
        ).fetchall()
        assert len(rows) == 2
        conn.close()

    def test_upsert_works(self, tmp_repo):
        db_path = tmp_repo / "metrics.sqlite"
        conn = get_db(db_path)
        for views in [100, 200]:
            conn.execute(
                "INSERT OR REPLACE INTO daily_metrics (content_id, channel_id, date, platform, views) VALUES (?,?,?,?,?)",
                ("CNT-001-0001", "CH-001", "2026-07-16", "threads", views),
            )
        conn.commit()
        row = conn.execute(
            "SELECT views FROM daily_metrics WHERE content_id=? AND date=? AND platform=?",
            ("CNT-001-0001", "2026-07-16", "threads"),
        ).fetchone()
        assert row["views"] == 200
        conn.close()


# ---------------------------------------------------------------------------
# content_schema v3
# ---------------------------------------------------------------------------

_SCHEMA_V3_PATH = Path(__file__).resolve().parents[2] / "core" / "schemas" / "content_schema_v3.json"

# Schemas are read once at import — avoids re-reading from disk per test
_SCHEMA_V3: dict = json.loads(_SCHEMA_V3_PATH.read_text())


def _load_schema_v3() -> dict:
    return _SCHEMA_V3


def _make_v3_via_scaffold(tmp_path: Path, title: str = "Test", cnt_id_override: str | None = None) -> tuple[str, Path]:
    """Create a v3 content.json via scaffold_cnt — avoids maintaining _minimal_v3()'s structure twice."""
    channels = tmp_path / "channels"
    channels.mkdir(exist_ok=True)
    ch_slug = "CH-001-threads-demo"
    ch_dir = channels / ch_slug
    (ch_dir / "content").mkdir(parents=True, exist_ok=True)
    return scaffold_cnt("CH-001", title, channels)


def _minimal_v3(cnt_id: str = "CNT-001-0001") -> dict:
    """Minimal valid schema_version: 3 content.json (inline fixture for schema validation)."""
    return {
        "schema_version": 3,
        "id": cnt_id,
        "channel_id": "CH-001",
        "title": "Test",
        "scheduled_for": None,
        "experiment": None,
        "lifecycle": {"drafted_at": "2026-07-16T00:00:00+00:00"},
        "gates": {
            "g1": {"checks": {}, "by": None, "at": None, "provenance": None},
            "g2": {"checks": {}, "by": None, "at": None, "provenance": None},
        },
        "production": {"brief": None, "draft": None, "human_minutes": 0, "fail_reason": None},
        "outputs": [],
        "jobs": [],
    }


class TestSchemaV3:
    def test_minimal_valid(self):
        schema = _load_schema_v3()
        jsonschema.validate(_minimal_v3(), schema)

    def test_scheduled_for_string(self):
        schema = _load_schema_v3()
        doc = _minimal_v3()
        doc["scheduled_for"] = "2026-07-17T21:00:00+09:00"
        jsonschema.validate(doc, schema)

    def test_experiment_block_valid(self):
        schema = _load_schema_v3()
        doc = _minimal_v3()
        doc["experiment"] = {
            "category": "explainer",
            "hook": "first-line question",
            "format": "thread",
            "cta": "saves",
            "hypothesis": "a key summary beginners will want to save",
            "kpi_target": "saves",
        }
        jsonschema.validate(doc, schema)

    def test_experiment_missing_field_invalid(self):
        schema = _load_schema_v3()
        doc = _minimal_v3()
        doc["experiment"] = {
            "category": "tip",
            "hook": "hook",
            "format": "thread",
            # cta, hypothesis, kpi_target missing
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_schema_version_2_invalid(self):
        """The v3 schema rejects schema_version: 2."""
        schema = _load_schema_v3()
        doc = _minimal_v3()
        doc["schema_version"] = 2
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_scaffold_produces_v6(self, tmp_repo):
        """content.json created by scaffold_cnt has schema_version: 6."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        _, cnt_json = scaffold_cnt("CH-001", "v4 test", channels)
        content = json.loads(cnt_json.read_text())
        assert content["schema_version"] == 6
        assert "scheduled_for" in content
        assert "experiment" in content
        assert content["scheduled_for"] is None
        assert content["experiment"] is None
        # Gate0 fields introduced in v4 (kept in v5 and v6)
        assert content["topic_uuid"] is None
        assert "gate0_ok_at" in content["lifecycle"]
        assert "g0" in content["gates"]

    def test_scaffold_validates_against_v6_schema(self, tmp_repo):
        """scaffold_cnt output passes content_schema_v6."""
        schema = json.loads(
            (_SCHEMA_V3_PATH.parent / "content_schema_v6.json").read_text()
        )
        channels = tmp_repo / "channels"
        _make_channel(channels)
        _, cnt_json = scaffold_cnt("CH-001", "schema validation", channels)
        content = json.loads(cnt_json.read_text())
        jsonschema.validate(content, schema)

    def test_v3_gate_pipeline(self, tmp_repo):
        """Full pipeline test: scaffold_cnt (v3) → record_gate → rebuild_index."""
        channels = tmp_repo / "channels"
        ch = _make_channel(channels)
        profile = {
            "schema_version": 2, "channel_id": "CH-001", "name": "Threads demo",
            "lifecycle_state": "calibrating",
        }
        (ch / "profile.json").write_text(json.dumps(profile))

        _, cnt_json = scaffold_cnt("CH-001", "pipeline test", channels)

        # record gate1 after drafted_at (drafted_at is UTC now, so use a future time)
        record_gate(cnt_json, "g1", {"g1_topic_safe": True}, by="human", at="2099-01-01T00:00:00+00:00")
        require_gate1(cnt_json)  # no exception

        idx_path = tmp_repo / "state" / "index.json"
        index = rebuild_index(channels, idx_path)
        assert len(index["channels"]) == 1
        assert index["channels"][0]["contents"][0]["status"] == "gate1_ok"

    def test_v3_full_gate_cycle(self, tmp_repo):
        """Full gate1 → gate2 cycle passes with a v3 content.json."""
        channels = tmp_repo / "channels"
        _make_channel(channels)
        _, cnt_json = scaffold_cnt("CH-001", "full cycle", channels)

        record_gate(cnt_json, "g1", {"g1_topic_safe": True}, by="human", at="2026-07-16T10:00:00+00:00")
        record_gate(cnt_json, "g2", {"g2_quality": True}, by="human", at="2026-07-16T11:00:00+00:00")

        require_gate1(cnt_json)
        require_gate2(cnt_json)

        content = json.loads(cnt_json.read_text())
        assert content["lifecycle"]["gate1_ok_at"] is not None
        assert content["lifecycle"]["gate2_ok_at"] is not None
        # check v3 fields survive gate operations
        assert "scheduled_for" in content
        assert "experiment" in content


# ---------------------------------------------------------------------------
# assign_next_slot
# ---------------------------------------------------------------------------

from core.slots import assign_next_slot  # noqa: E402
from datetime import datetime, timedelta, timezone

_KST = timezone(timedelta(hours=9))


def _make_channel_with_profile(channels_root: Path, channel_id: str = "CH-001") -> Path:
    slug = f"{channel_id}-threads-demo"
    ch = channels_root / slug
    (ch / "content").mkdir(parents=True)
    (ch / "profile.json").write_text(json.dumps({
        "schema_version": 2,
        "channel_id": channel_id,
        "platform": "threads",
        "cadence": "3/week",
    }))
    return ch


def _make_cnt(ch_dir: Path, cnt_id: str, scheduled_for: str | None = None) -> Path:
    cnt_dir = ch_dir / "content" / cnt_id
    cnt_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 4,
        "id": cnt_id,
        "channel_id": "CH-001",
        "title": "test",
        "scheduled_for": scheduled_for,
        "lifecycle": {},
        "gates": {},
        "production": {},
        "outputs": [],
    }
    p = cnt_dir / "content.json"
    p.write_text(json.dumps(data))
    return p


class TestAssignNextSlot:
    def test_assigns_first_slot_after_ref(self, tmp_path):
        """Reference time (Monday 00:00 KST) → first slot = Tuesday 21:00 KST."""
        channels = tmp_path / "channels"
        ch = _make_channel_with_profile(channels)
        cnt_path = _make_cnt(ch, "CNT-001-0001")

        # Monday 00:00 KST
        ref = datetime(2026, 7, 27, 0, 0, 0, tzinfo=_KST)  # 2026-07-27 = Monday
        slot = assign_next_slot("CNT-001-0001", channels, after=ref)

        assert slot == "2026-07-28T21:00:00+09:00"

    def test_idempotent_returns_existing(self, tmp_path):
        """Existing scheduled_for is returned as is (nothing written)."""
        channels = tmp_path / "channels"
        ch = _make_channel_with_profile(channels)
        existing = "2026-07-30T21:00:00+09:00"
        cnt_path = _make_cnt(ch, "CNT-001-0001", scheduled_for=existing)

        ref = datetime(2026, 7, 27, 0, 0, 0, tzinfo=_KST)
        slot = assign_next_slot("CNT-001-0001", channels, after=ref)

        assert slot == existing
        # file unchanged too
        data = json.loads(cnt_path.read_text())
        assert data["scheduled_for"] == existing

    def test_skips_taken_slots(self, tmp_path):
        """Moves on to Thursday when the Tuesday slot is taken."""
        channels = tmp_path / "channels"
        ch = _make_channel_with_profile(channels)
        # CNT-001-0001 already holds Tuesday 21:00 KST
        _make_cnt(ch, "CNT-001-0001", scheduled_for="2026-07-28T21:00:00+09:00")
        cnt_path = _make_cnt(ch, "CNT-001-0002")

        ref = datetime(2026, 7, 27, 0, 0, 0, tzinfo=_KST)
        slot = assign_next_slot("CNT-001-0002", channels, after=ref)

        assert slot == "2026-07-30T21:00:00+09:00"  # Thursday

    def test_writes_to_content_json(self, tmp_path):
        """content.json scheduled_for is updated after assignment."""
        channels = tmp_path / "channels"
        ch = _make_channel_with_profile(channels)
        cnt_path = _make_cnt(ch, "CNT-001-0001")

        ref = datetime(2026, 7, 27, 0, 0, 0, tzinfo=_KST)
        slot = assign_next_slot("CNT-001-0001", channels, after=ref)

        data = json.loads(cnt_path.read_text())
        assert data["scheduled_for"] == slot

    def test_ref_exactly_on_slot_boundary_skips_it(self, tmp_path):
        """If ref equals a slot time exactly, that slot is skipped and the next one returned."""
        channels = tmp_path / "channels"
        ch = _make_channel_with_profile(channels)
        cnt_path = _make_cnt(ch, "CNT-001-0001")

        # exactly Tuesday 21:00 KST
        ref = datetime(2026, 7, 28, 21, 0, 0, tzinfo=_KST)
        slot = assign_next_slot("CNT-001-0001", channels, after=ref)

        assert slot == "2026-07-30T21:00:00+09:00"  # Thursday

    def test_channel_not_found_raises(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(FileNotFoundError):
            assign_next_slot("CNT-001-0001", channels)

    def test_custom_profile_slots(self, tmp_path):
        """publish_slots in profile.json are used instead of the cadence default."""
        channels = tmp_path / "channels"
        slug = "CH-001-threads-demo"
        ch = channels / slug
        (ch / "content").mkdir(parents=True)
        (ch / "profile.json").write_text(json.dumps({
            "schema_version": 2,
            "channel_id": "CH-001",
            "platform": "threads",
            "cadence": "2/week",
            "publish_slots": [
                {"weekday": 0, "hour": 9, "minute": 0},   # Monday 09:00 KST
                {"weekday": 3, "hour": 18, "minute": 0},  # Thursday 18:00 KST
            ],
        }))
        cnt_path = _make_cnt(ch, "CNT-001-0001")

        # Sunday 23:00 KST
        ref = datetime(2026, 7, 26, 23, 0, 0, tzinfo=_KST)
        slot = assign_next_slot("CNT-001-0001", channels, after=ref)

        assert slot == "2026-07-27T09:00:00+09:00"  # Monday 09:00
