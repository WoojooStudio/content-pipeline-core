"""Fixture tests — publisher (layered automatic publishing).

Verifies every layer without tokens using FakeTransport. Real ThreadsTransport
(graph.threads.net) publishing is verified separately once a token exists
(this file covers contracts, enforcement and idempotency only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT / "toolbelt") not in sys.path:
    sys.path.insert(0, str(_ROOT / "toolbelt"))

from core.approval import approval_digest  # noqa: E402
from core.ledger import read_all as read_ledger  # noqa: E402
from publisher import (  # noqa: E402
    CredentialsMissing,
    FakeTransport,
    PublishBlocked,
    PublisherError,
    ThreadsTransport,
    publish,
)
from publisher import nonce as nonce_mod  # noqa: E402
from publisher import adapters  # noqa: E402
from publisher.adapters import threads as threads_adapter  # noqa: E402
from publisher.adapters import instagram as instagram_adapter  # noqa: E402
from publisher.artifact import build_artifact  # noqa: E402
from publisher.signature import SignatureError, sign, verify  # noqa: E402


@pytest.fixture
def tmp_repo(tmp_path):
    (tmp_path / "channels").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "nonce").mkdir()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "ledger.jsonl").touch()
    return tmp_path


def _channel(channels_root: Path, ch_id: str = "CH-001", auto_publish: bool = True,
             slug: str | None = None) -> Path:
    slug = slug or f"{ch_id}-threads-main"
    ch = channels_root / slug
    (ch / "content").mkdir(parents=True)
    profile = {
        "schema_version": 2, "channel_id": ch_id, "name": "Threads channel",
        "platform": "threads", "auto_publish": auto_publish,
        "credentials_ref": "threads_main", "lifecycle_state": "calibrating",
    }
    (ch / "profile.json").write_text(json.dumps(profile, ensure_ascii=False))
    return ch


def _make_content(ch: Path, cnt_id: str, lifecycle: dict,
                  draft: str = "Hello Day Master?\n\nWhich force are you?",
                  channel_id: str = "CH-001") -> Path:
    cnt_dir = ch / "content" / cnt_id
    cnt_dir.mkdir(parents=True, exist_ok=True)
    lc = {k: None for k in [
        "drafted_at", "gate1_ok_at", "gate2_ok_at", "publishing_at",
        "published_at", "measured_at", "rejected_at", "failed_at", "cancelled_at",
    ]}
    lc["drafted_at"] = "2026-07-16T00:00:00+00:00"
    lc.update(lifecycle)
    content = {
        "schema_version": 4, "id": cnt_id, "channel_id": channel_id,
        "title": "Test", "scheduled_for": None, "experiment": None,
        "lifecycle": lc,
        "gates": {"g2": {"checks": {}, "by": None, "at": None, "provenance": None}},
        "production": {"brief": None, "draft": draft, "human_minutes": 0, "fail_reason": None},
        "outputs": [], "jobs": [],
    }
    if lc.get("gate2_ok_at"):  # like a real approval — store the approval digest too
        content["gates"]["g2"]["provenance"] = {
            "approved_by": "human", "approval_digest": approval_digest(content, cnt_dir)}
    p = cnt_dir / "content.json"
    p.write_text(json.dumps(content, ensure_ascii=False))
    return p


_GATE2 = {"gate1_ok_at": "2026-07-16T09:00:00+00:00", "gate2_ok_at": "2026-07-16T10:00:00+00:00"}


class TestGateEnforcement:
    def test_blocks_without_gate2(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=False)
        _make_content(ch, "CNT-001-0001", {})  # no gate2
        with pytest.raises(PublishBlocked):
            publish("CNT-001-0001", tmp_repo / "channels",
                    transport=FakeTransport(), ledger_path=tmp_repo / "ledger.jsonl",
                    nonce_root=tmp_repo / "state" / "nonce")

    @pytest.mark.parametrize("exit_key", ["rejected_at", "cancelled_at", "failed_at"])
    def test_blocks_exit_states(self, tmp_repo, exit_key):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        lc = dict(_GATE2)
        lc[exit_key] = "2026-07-16T11:00:00+00:00"
        _make_content(ch, "CNT-001-0001", lc)
        with pytest.raises(PublishBlocked):
            publish("CNT-001-0001", tmp_repo / "channels",
                    transport=FakeTransport(), ledger_path=tmp_repo / "ledger.jsonl",
                    secrets_root=tmp_repo / "secrets", nonce_root=tmp_repo / "state" / "nonce")

    def test_invalid_id(self, tmp_repo):
        _channel(tmp_repo / "channels")
        with pytest.raises(PublisherError):
            publish("BAD-ID", tmp_repo / "channels", transport=FakeTransport())


class TestPolicy:
    def test_auto_publish_false_blocks(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=False)
        _make_content(ch, "CNT-001-0001", _GATE2)
        with pytest.raises(PublishBlocked, match="auto_publish=false"):
            publish("CNT-001-0001", tmp_repo / "channels",
                    transport=FakeTransport(), ledger_path=tmp_repo / "ledger.jsonl",
                    nonce_root=tmp_repo / "state" / "nonce")


def _sign_approval(tmp_repo, ch, cnt_id, draft, channel_id="CH-001", platform="threads"):
    """Plant the signing key + sidecar token in secrets/ needed for an auto_publish=true publish."""
    key = b"test-signing-key-do-not-use-in-prod"
    (tmp_repo / "secrets" / "publish_signing_key").write_bytes(key)
    content = json.loads((ch / "content" / cnt_id / "content.json").read_text())
    artifact = build_artifact(content, platform)
    # needs the same nonce service will use — reserve it up front and bind it in the sidecar
    nonce = nonce_mod.reserve(cnt_id, artifact.payload_hash, tmp_repo / "state" / "nonce")
    sidecar = sign(
        content_id=cnt_id, channel_id=channel_id,
        payload_hash=artifact.payload_hash, nonce=nonce.value,
        approver="human", key=key,
    )
    (tmp_repo / "secrets" / f"approval_{cnt_id}.json").write_text(
        json.dumps({
            "content_id": sidecar.content_id, "channel_id": sidecar.channel_id,
            "payload_hash": sidecar.payload_hash, "nonce": sidecar.nonce,
            "approved_at": sidecar.approved_at, "approver": sidecar.approver,
            "signature": sidecar.signature,
        }, ensure_ascii=False)
    )


class TestPublish:
    def test_dry_run_no_writeback(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        p = _make_content(ch, "CNT-001-0001", _GATE2)
        _sign_approval(tmp_repo, ch, "CNT-001-0001", None)
        result = publish("CNT-001-0001", tmp_repo / "channels",
                         transport=FakeTransport(), ledger_path=tmp_repo / "ledger.jsonl",
                         secrets_root=tmp_repo / "secrets",
                         nonce_root=tmp_repo / "state" / "nonce", dry_run=True)
        assert result.changed is False
        assert result.payload_hash
        content = json.loads(p.read_text())
        assert content["lifecycle"]["published_at"] is None
        assert content["outputs"] == []

    def test_real_publish_writeback(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        p = _make_content(ch, "CNT-001-0001", _GATE2)
        _sign_approval(tmp_repo, ch, "CNT-001-0001", None)
        transport = FakeTransport(post_id="THREADS_123")
        result = publish("CNT-001-0001", tmp_repo / "channels",
                         transport=transport, ledger_path=tmp_repo / "ledger.jsonl",
                         secrets_root=tmp_repo / "secrets",
                         nonce_root=tmp_repo / "state" / "nonce")
        assert result.changed is True
        assert result.post_id == "THREADS_123"
        content = json.loads(p.read_text())
        assert content["lifecycle"]["published_at"] is not None
        assert content["lifecycle"]["publishing_at"] is not None
        assert content["outputs"][0]["platform_id"] == "THREADS_123"
        # ordering invariant: published_at ≥ gate2_ok_at
        assert content["lifecycle"]["published_at"] >= content["lifecycle"]["gate2_ok_at"]

    def test_idempotent(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        p = _make_content(ch, "CNT-001-0001", _GATE2)
        _sign_approval(tmp_repo, ch, "CNT-001-0001", None)
        args = dict(transport=FakeTransport(post_id="THREADS_123"),
                    ledger_path=tmp_repo / "ledger.jsonl",
                    secrets_root=tmp_repo / "secrets",
                    nonce_root=tmp_repo / "state" / "nonce")
        publish("CNT-001-0001", tmp_repo / "channels", **args)
        result2 = publish("CNT-001-0001", tmp_repo / "channels", **args)
        assert result2.changed is False
        content = json.loads(p.read_text())
        assert len(content["outputs"]) == 1  # no duplicate append

    def test_ledger_recorded_once(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        _make_content(ch, "CNT-001-0001", _GATE2)
        _sign_approval(tmp_repo, ch, "CNT-001-0001", None)
        args = dict(transport=FakeTransport(post_id="THREADS_123"),
                    ledger_path=tmp_repo / "ledger.jsonl",
                    secrets_root=tmp_repo / "secrets",
                    nonce_root=tmp_repo / "state" / "nonce")
        publish("CNT-001-0001", tmp_repo / "channels", **args)
        publish("CNT-001-0001", tmp_repo / "channels", **args)
        events = [e for e in read_ledger(tmp_repo / "ledger.jsonl")
                  if e.get("event") == "content.published"]
        assert len(events) == 1
        assert events[0]["module"] == "publish_threads"


class TestSignature:
    def test_blocks_without_signature(self, tmp_repo):
        ch = _channel(tmp_repo / "channels", auto_publish=True)
        _make_content(ch, "CNT-001-0001", _GATE2)
        # no signing key or sidecar
        with pytest.raises(PublishBlocked):
            publish("CNT-001-0001", tmp_repo / "channels",
                    transport=FakeTransport(), ledger_path=tmp_repo / "ledger.jsonl",
                    secrets_root=tmp_repo / "secrets",
                    nonce_root=tmp_repo / "state" / "nonce")

    def test_signature_hash_binding(self, tmp_repo):
        key = b"k"
        sc = sign(content_id="CNT-001-0001", channel_id="CH-001",
                  payload_hash="abc", nonce="n1", approver="human", key=key)
        # verifying with a different payload_hash → fails (TOCTOU guard)
        with pytest.raises(SignatureError, match="payload_hash"):
            verify(sc, content_id="CNT-001-0001", channel_id="CH-001",
                   payload_hash="DIFFERENT", nonce="n1", key=key)

    def test_signature_valid(self, tmp_repo):
        key = b"k"
        sc = sign(content_id="CNT-001-0001", channel_id="CH-001",
                  payload_hash="abc", nonce="n1", approver="human", key=key)
        verify(sc, content_id="CNT-001-0001", channel_id="CH-001",
               payload_hash="abc", nonce="n1", key=key)  # passes if no exception


class TestCredentials:
    def test_missing_credentials(self, tmp_repo):
        transport = ThreadsTransport(None)
        with pytest.raises(CredentialsMissing):
            transport.send({"text": "hi", "media_type": "TEXT"})


class TestArtifactAndAdapter:
    def test_artifact_hash_stable(self, tmp_repo):
        ch = _channel(tmp_repo / "channels")
        _make_content(ch, "CNT-001-0001", _GATE2)
        content = json.loads((ch / "content" / "CNT-001-0001" / "content.json").read_text())
        a1 = build_artifact(content, "threads")
        a2 = build_artifact(content, "threads")
        assert a1.payload_hash == a2.payload_hash

    def test_adapter_build_and_parse(self):
        req = threads_adapter.build_request({"text": "hello", "lang": "en", "mode": "native"})
        assert req["media_type"] == "TEXT"
        assert req["text"] == "hello"
        post_id, permalink = threads_adapter.parse_response({"id": "P1"})
        assert post_id == "P1"
        assert "P1" in permalink


class TestInstagramAdapter:
    """Card-news publishing rail — IG carousel adapter (owns no credentials or nonces)."""

    def test_for_platform_selects_adapter(self):
        assert adapters.for_platform("threads") is threads_adapter
        assert adapters.for_platform("instagram") is instagram_adapter

    def test_for_platform_unknown_raises(self):
        with pytest.raises(ValueError):
            adapters.for_platform("tiktok")

    def test_carousel_request_shape(self):
        req = instagram_adapter.build_request(
            {"images": ["u1", "u2", "u3"], "text": "caption"}
        )
        assert req["platform"] == "instagram"
        assert req["media_type"] == "CAROUSEL"
        assert req["images"] == ["u1", "u2", "u3"]
        assert req["caption"] == "caption"

    def test_single_image_is_not_carousel(self):
        req = instagram_adapter.build_request({"images": ["only"], "text": ""})
        assert req["media_type"] == "IMAGE"

    def test_empty_images_rejected(self):
        # without image_gen output, reject explicitly at publish time (never skip silently)
        with pytest.raises(ValueError):
            instagram_adapter.build_request({"images": [], "text": "x"})

    def test_too_many_images_rejected(self):
        with pytest.raises(ValueError):
            instagram_adapter.build_request({"images": [f"u{i}" for i in range(11)], "text": ""})

    def test_parse_response(self):
        media_id, permalink = instagram_adapter.parse_response({"id": "M9"})
        assert media_id == "M9"
        assert "M9" in permalink

    def test_parse_response_no_id_raises(self):
        with pytest.raises(ValueError):
            instagram_adapter.parse_response({})

    def test_artifact_includes_images_for_instagram(self, tmp_repo):
        ch = _channel(tmp_repo / "channels")
        cnt = _make_content(ch, "CNT-001-0001", _GATE2)
        content = json.loads(cnt.read_text())
        content.setdefault("production", {})["images"] = ["img1", "img2"]
        a = build_artifact(content, "instagram")
        assert a.payload["images"] == ["img1", "img2"]
        assert a.platform == "instagram"

    def test_service_publishes_carousel_via_fake_transport(self, tmp_repo):
        # auto_publish=false would block via policy, so testing the IG rail itself needs
        # a true channel + a valid signature. Mirrors the threads tests for IG.
        ch = _channel(tmp_repo / "channels", ch_id="CH-002", auto_publish=True,
                      slug="CH-002-instagram-main")
        # switch the profile to the instagram platform
        profile = json.loads((ch / "profile.json").read_text())
        profile["platform"] = "instagram"
        (ch / "profile.json").write_text(json.dumps(profile, ensure_ascii=False))
        cnt = _make_content(ch, "CNT-002-0001", _GATE2, channel_id="CH-002")
        content = json.loads(cnt.read_text())
        content.setdefault("production", {})["images"] = ["img1", "img2"]
        cnt.write_text(json.dumps(content, ensure_ascii=False))
        # create the signed sidecar (after writing images, since the artifact hash includes them)
        _sign_approval(tmp_repo, ch, "CNT-002-0001", None,
                       channel_id="CH-002", platform="instagram")
        result = publish(
            "CNT-002-0001", tmp_repo / "channels",
            transport=FakeTransport(post_id="IG_1"),
            ledger_path=tmp_repo / "ledger.jsonl",
            secrets_root=tmp_repo / "secrets",
            nonce_root=tmp_repo / "state" / "nonce",
        )
        assert result.changed is True
        assert result.post_id == "IG_1"
        out = json.loads(cnt.read_text())
        assert out["outputs"][0]["platform_id"] == "IG_1"
