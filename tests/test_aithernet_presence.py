"""AitherNet slice 1: the Presence Card signs, redacts, expires and fails closed.

The contract being tested is not "the happy path works" but "every way of being
wrong is refused": a tampered body, an expired assertion, a tombstone, a card that
carries a field its bitmask forbids, a private card requested over the wire, and a
remote caller trying to publish. A verifier that cannot fail is not a verifier.
"""

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from awnode import aithernet_presence as presence  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate this node: its own key, its own state, its own ~/.aither."""
    monkeypatch.setenv("AITHERNET_PRESENCE_KEY_PATH", str(tmp_path / "node_ed25519"))
    monkeypatch.setenv("AITHERNET_PRESENCE_STATE", str(tmp_path / "presence.json"))
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "aither"))
    monkeypatch.delenv("AWSEAL_KEY_PATH", raising=False)
    monkeypatch.delenv("AITHERNET_MESH_ID", raising=False)
    return tmp_path


def _past_and_future():
    now = datetime.now(timezone.utc)
    return now - timedelta(minutes=5), now + timedelta(minutes=10)


# ── Sign / verify round trip ─────────────────────────────────────────────────


def test_sign_and_verify_round_trip(env):
    card = presence.publish({
        "visibility": "listed",
        "fields": {"existence": True, "capacity": True},
        "lends": ["embed_gpu"],
        "display_name": "selftest-node",
    })
    info = presence.verify(card)
    assert info["ok"] is True
    assert info["schema"] == "aither.presence/1"
    assert info["node_id"].startswith("ed25519:")
    assert info["tombstone"] is False
    assert info["visibility"] == "listed"


def test_canonical_json_is_sorted_and_compact(env):
    body = {"b": 1, "a": {"d": 4, "c": 3}}
    assert presence.canonical_json(body) == b'{"a":{"c":3,"d":4},"b":1}'


def test_verify_binds_the_signature_to_the_claimed_node_id(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    other = presence.load_private_key(env / "other_key")
    with pytest.raises(presence.PresenceSignatureError):
        presence.verify(card, expected_pubkey=presence.public_key_hex(other))


def test_device_key_is_preferred_over_a_fresh_node_key(env, monkeypatch, tmp_path):
    """awseal's enrolled device key IS the node's identity when it exists."""
    monkeypatch.delenv("AITHERNET_PRESENCE_KEY_PATH", raising=False)
    device = tmp_path / "awseal" / "signing.key"
    presence._create_key(device)
    monkeypatch.setenv("AWSEAL_KEY_PATH", str(device))

    assert presence.key_source() == "device"
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    info = presence.verify(card, expected_pubkey=presence.public_key_hex(path=device))
    assert info["ok"] is True
    assert not (env / "aithernet" / "node_ed25519").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not honoured on Windows")
def test_node_key_is_created_0600(env):
    presence.load_private_key()
    mode = (env / "node_ed25519").stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows ignores POSIX modes; the DACL is the surface")
def test_private_key_and_state_are_owner_only_on_windows(env):
    """os.chmod reports success on Windows without restricting anything, so the
    key and the state file (which holds the UNREDACTED card) must be ACL-restricted."""
    presence.load_private_key()
    presence.publish({"visibility": "listed", "fields": {"existence": True}})
    for path in (env / "node_ed25519", env / "presence.json"):
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, result.stderr
        assert "(I)" not in result.stdout, result.stdout


# ── Fail closed ──────────────────────────────────────────────────────────────


def test_tampered_body_is_refused(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    tampered = dict(card)
    tampered["seq"] = card["seq"] + 1
    with pytest.raises(presence.PresenceSignatureError):
        presence.verify(tampered)


def test_swapping_the_node_id_is_refused(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    other = presence.load_private_key(env / "other_key")
    swapped = json.loads(json.dumps(card))
    swapped["identity"]["node_id"] = presence.node_id(other)
    with pytest.raises(presence.PresenceSignatureError):
        presence.verify(swapped)


def test_unknown_schema_is_refused(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    card["schema"] = "aither.presence/2"
    with pytest.raises(presence.PresenceSchemaError):
        presence.verify(card)


def test_undeclared_key_cannot_ride_along(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    card["secret_notes"] = "should never be accepted"
    with pytest.raises(presence.PresenceSchemaError):
        presence.verify(card)


def test_expired_card_is_refused(env):
    asserted, expired = _past_and_future()
    full = presence.build_card(
        seq=1,
        asserted_at=presence.format_time(asserted - timedelta(minutes=30)),
        expires_at=presence.format_time(expired - timedelta(minutes=30)),
    )
    card = presence.sign_card(presence.redact(full))
    with pytest.raises(presence.PresenceExpiredError):
        presence.verify(card)


def test_expiry_is_judged_against_supplied_now(env):
    asserted, expiry = _past_and_future()
    card = presence.sign_card(presence.redact(presence.build_card(
        seq=1, asserted_at=presence.format_time(asserted), expires_at=presence.format_time(expiry),
    )))
    # Valid now, expired later: the same card gets two different verdicts.
    assert presence.verify(card)["ok"] is True
    with pytest.raises(presence.PresenceExpiredError):
        presence.verify(card, now=expiry + timedelta(seconds=1))


def test_lends_outside_the_vocabulary_is_refused(env):
    with pytest.raises(presence.PresenceSchemaError):
        presence.build_card(lends=["gpu"])  # a legacy tag, not a node capability kind


def test_non_finite_number_is_refused_not_a_crash(env):
    """json.loads accepts NaN; canonical_json (allow_nan=False) must never be reached."""
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    bad = json.loads(json.dumps(card))
    bad["identity"]["mesh_id"] = float("nan")
    with pytest.raises(presence.PresenceError):
        presence.verify(bad)


def test_lone_surrogate_is_refused_not_a_crash(env):
    """A str can hold a lone surrogate; UTF-8 encoding would raise UnicodeEncodeError."""
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    bad = json.loads(json.dumps(card))
    bad["display_name"] = "\ud800"
    with pytest.raises(presence.PresenceError):
        presence.verify(bad)


def test_duplicate_json_keys_are_refused(env, tmp_path):
    """The signature covers one value per key; a repeated key makes the bytes ambiguous."""
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    raw = json.dumps(card).replace('"seq":', '"seq": 99, "seq":', 1)
    path = tmp_path / "dup.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(presence.PresenceSchemaError):
        presence.verify_file(path)


def test_a_later_assertion_supersedes_an_older_card_when_min_seq_is_given(env):
    first = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    stone = presence.withdraw()
    with pytest.raises(presence.PresenceStaleError):
        presence.verify(first, min_seq=stone["seq"])
    # Without the newer document, the old card is still a valid assertion.
    assert presence.verify(first)["ok"] is True


def test_verify_file_carries_min_seq(env, tmp_path):
    first = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    stone = presence.withdraw()
    path = tmp_path / "first.json"
    path.write_text(json.dumps(first), encoding="utf-8")
    with pytest.raises(presence.PresenceStaleError):
        presence.verify_file(path, min_seq=stone["seq"])


def test_default_ttl_is_fifteen_minutes(env):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    asserted = presence.parse_time(card["asserted_at"])
    assert presence.parse_time(card["expires_at"]) - asserted == timedelta(
        seconds=presence.DEFAULT_TTL_SECONDS
    )


# ── Redaction ────────────────────────────────────────────────────────────────


def test_redaction_emits_only_licensed_fields(env):
    full = presence.build_card(
        display_name="owner-name",
        hostname="private-host.internal",
        visibility="listed",
        fields={"existence": True},  # owner_name and hostname bits stay false
        seq=1,
    )
    served = presence.redact(full)
    assert "display_name" not in served
    assert "hostname" not in served
    assert set(served) == {
        "schema", "identity", "visibility", "fields", "seq", "asserted_at", "expires_at",
    }


def test_redaction_without_existence_is_only_the_envelope(env):
    full = presence.build_card(
        display_name="owner-name",
        hostname="private-host.internal",
        lends=["embed_gpu"],
        visibility="mesh",
    )
    served = presence.redact(full)
    assert set(served) == {
        "schema", "identity", "visibility", "fields", "seq", "asserted_at", "expires_at",
    }


def test_disclosure_leak_detector_catches_a_validly_signed_leak(env):
    full = presence.build_card(
        display_name="owner-name",
        visibility="listed",
        fields={"existence": True},
        seq=1,
    )
    leaky = presence.sign_card(full)  # the signature is GOOD; the disclosure is not
    assert presence.verify(leaky)["ok"] is True
    leaks = presence.disclosure_leaks(leaky)
    assert any("display_name" in line for line in leaks)


def test_never_public_grants_are_always_a_leak(env):
    full = presence.build_card(
        visibility="listed",
        fields={"existence": True},
        share_with=[{"principal": "someone-else", "scope": "spaces"}],
        seq=1,
    )
    leaky = presence.sign_card(full)
    assert any("share_with" in line for line in presence.disclosure_leaks(leaky))


def test_redaction_never_publishes_account_tenant_or_mesh_ids(env):
    """Only node_id (the public key) travels; account/tenant/mesh ids are node facts."""
    full = presence.build_card(
        account_id="acct-SECRET-123",
        tenant_id="tenant-SECRET-456",
        mesh_id="mesh-SECRET-789",
        visibility="listed",
        fields={"existence": False},
    )
    served = presence.sign_card(presence.redact(full))
    assert set(served["identity"]) == {"node_id"}
    assert presence.disclosure_leaks(served) == []


def test_a_validly_signed_card_carrying_account_ids_is_a_leak(env):
    served = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    leaky = presence.sign_card(
        {**served, "identity": {**served["identity"], "account_id": "acct-SECRET-123"}}
    )
    assert presence.verify(leaky)["ok"] is True  # the signature is GOOD
    assert any("account_id" in line for line in presence.disclosure_leaks(leaky))


def test_withdraw_tombstone_never_carries_account_ids(env):
    presence.publish({"visibility": "listed", "fields": {"existence": True}})
    stone = presence.withdraw()
    assert set(stone["identity"]) == {"node_id"}
    assert presence.disclosure_leaks(stone) == []


# ── Tombstone ────────────────────────────────────────────────────────────────


def test_withdraw_publishes_a_signed_tombstone_at_seq_plus_one(env):
    first = presence.publish(
        {"visibility": "listed", "fields": {"existence": True, "capacity": True}}
    )
    stone = presence.withdraw()

    assert stone["seq"] == first["seq"] + 1
    assert presence.is_tombstone(stone) is True
    assert presence.verify(stone)["tombstone"] is True
    assert stone["visibility"] == "private"
    assert not any(stone["fields"].values())
    assert presence.current_card() is None


def test_republishing_after_withdraw_moves_seq_forward(env):
    presence.publish({"visibility": "listed", "fields": {"existence": True}})
    presence.withdraw()
    again = presence.publish({"visibility": "mesh", "fields": {"existence": True}})
    assert again["seq"] == 3


# ── Served surface ───────────────────────────────────────────────────────────


@pytest.fixture()
def server():
    return importlib.import_module("awnode.server")


def _client(server_module, client=None):
    from fastapi.testclient import TestClient

    return TestClient(server_module.app) if client is None else TestClient(
        server_module.app, client=client
    )


def test_well_known_is_404_before_the_first_publish(env, server):
    r = _client(server).get("/.well-known/aithernet/node.json")
    assert r.status_code == 404


def test_well_known_is_404_while_private(env, server):
    presence.publish({"visibility": "private", "fields": {"existence": True}})
    r = _client(server).get("/.well-known/aithernet/node.json")
    assert r.status_code == 404


def test_well_known_serves_the_verifiable_redacted_card(env, server):
    published = presence.publish({
        "visibility": "listed",
        "fields": {"existence": True, "capacity": True},
        "lends": ["embed_gpu"],
        "display_name": "owner-name",
        "hostname": "private-host.internal",
    })
    r = _client(server).get("/.well-known/aithernet/node.json")

    assert r.status_code == 200
    served = r.json()
    assert served == published
    assert presence.verify(served)["ok"] is True
    assert "hostname" not in served  # the bit was never set
    assert "display_name" not in served


def test_well_known_is_404_after_withdraw(env, server):
    presence.publish({"visibility": "listed", "fields": {"existence": True}})
    presence.withdraw()
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404


def test_well_known_is_404_when_the_stored_card_leaks(env, server):
    """A stored card that discloses an unlicensed field is a 404, not a disclosure."""
    leaky = presence.sign_card(presence.build_card(
        display_name="owner-name",
        visibility="listed",
        fields={"existence": True},
        seq=1,
    ))
    presence.save_state({
        "schema": presence.STATE_SCHEMA,
        "seq": 1,
        "card": leaky,
        "full": None,
        "node_id": leaky["identity"]["node_id"],
    })
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404


def test_well_known_is_404_when_the_stored_card_was_edited(env, server):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    tampered = dict(card, seq=card["seq"] + 7)
    presence.save_state({
        "schema": presence.STATE_SCHEMA,
        "seq": 7,
        "card": tampered,
        "full": None,
        "node_id": card["identity"]["node_id"],
    })
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404


def test_owner_publish_and_withdraw_from_loopback(env, server):
    r = _client(server).post(
        "/net/publish", json={"visibility": "listed", "fields": {"existence": True}}
    )
    assert r.status_code == 200
    assert presence.verify(r.json()["card"])["ok"] is True

    r = _client(server).post("/net/withdraw")
    assert r.status_code == 200
    assert presence.is_tombstone(r.json()["tombstone"]) is True


def test_publish_refuses_an_unknown_key(env, server):
    r = _client(server).post("/net/publish", json={"visibility": "listed", "seq": 99})
    assert r.status_code == 400
    assert "seq" in r.json()["detail"]


def test_publish_and_withdraw_refuse_a_non_owner(env, server):
    # A remote peer with no enrolled bearer: the SAME gate as every other
    # owner surface, which the well-known read deliberately does not need.
    remote = ("203.0.113.9", 55555)
    client = _client(server, client=remote)
    assert client.post("/net/publish", json={"visibility": "listed"}).status_code == 403
    assert client.post("/net/withdraw").status_code == 403


def test_publish_refuses_a_foreign_browser_origin(env, server):
    r = _client(server).post(
        "/net/publish",
        json={"visibility": "listed"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_publish_and_withdraw_refuse_a_same_host_tunnel(env, server):
    """A tunnel forwards a REMOTE client to 127.0.0.1 and stamps proxy headers.

    The direct peer therefore LOOKS local; the loopback shortcut in `_install_gate`
    must not fire, or any internet client a tunnel forwards reaches owner routes.
    """
    client = _client(server, client=("127.0.0.1", 40000))
    headers = {"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9"}
    r = client.post(
        "/net/publish",
        json={"visibility": "listed", "fields": {"existence": True}},
        headers=headers,
    )
    assert r.status_code == 403
    assert client.post("/net/withdraw", headers=headers).status_code == 403
    assert presence.current_card() is None


@pytest.mark.parametrize("visibility,mesh_sees", [("circle", False), ("mesh", True)])
def test_well_known_serves_only_listed_to_the_public(env, server, visibility, mesh_sees):
    presence.publish({"visibility": visibility, "fields": {"existence": True}})
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404
    # The node itself still holds the card; the ladder gates the public surface only.
    assert presence.current_card() is not None
    assert (presence.current_card(audience="mesh") is not None) is mesh_sees


@pytest.mark.parametrize(
    "body",
    [
        {"ttl_seconds": "abc"},
        {"spaces": 5},
        {"share_with": 5},
        {"data_donation": "x"},
    ],
)
def test_publish_malformed_types_are_400_not_500(env, server, body):
    r = _client(server).post("/net/publish", json=body)
    assert r.status_code == 400, r.text


def test_well_known_is_404_when_the_stored_card_carries_nan(env, server):
    """A stored NaN must be a fail-closed 404, never an uncaught ValueError -> 500."""
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    bad = json.loads(json.dumps(card))
    bad["identity"]["mesh_id"] = float("nan")
    presence.save_state({
        "schema": presence.STATE_SCHEMA,
        "seq": card["seq"],
        "card": bad,
        "full": None,
        "node_id": card["identity"]["node_id"],
    })
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404


def test_well_known_is_404_when_the_stored_card_leaks_identity(env, server):
    """account/tenant/mesh ids are node facts; a signed card carrying them is a leak."""
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    body = json.loads(json.dumps(card))
    body["identity"]["account_id"] = "acct-SECRET-123"
    leaky = presence.sign_card(body)
    assert presence.verify(leaky)["ok"] is True
    presence.save_state({
        "schema": presence.STATE_SCHEMA,
        "seq": card["seq"],
        "card": leaky,
        "full": None,
        "node_id": card["identity"]["node_id"],
    })
    assert _client(server).get("/.well-known/aithernet/node.json").status_code == 404


# ── CLI ──────────────────────────────────────────────────────────────────────


def _run_cli(monkeypatch, argv):
    from awnode import cli

    monkeypatch.setenv("AWNODE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(sys, "argv", ["awnode"] + argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return int(exc.value.code or 0)


def test_cli_presence_verify_valid_then_missing(env, monkeypatch, tmp_path):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    good = tmp_path / "card.json"
    good.write_text(json.dumps(card), encoding="utf-8")

    assert _run_cli(monkeypatch, ["presence", "verify", "--json", str(good)]) == 0
    assert _run_cli(monkeypatch, ["presence", "verify", str(tmp_path / "nope.json")]) == 2


def test_cli_presence_publish_and_show(env, monkeypatch, capsys):
    assert _run_cli(monkeypatch, [
        "presence", "publish", "--visibility", "listed", "--disclose", "existence",
    ]) == 0
    out = capsys.readouterr().out
    card = json.loads(out[out.index("{"):])
    assert presence.verify(card)["visibility"] == "listed"

    assert _run_cli(monkeypatch, ["presence", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["seq"] == card["seq"]


def test_cli_presence_rejects_an_unknown_disclosure_bit(env, monkeypatch):
    assert _run_cli(monkeypatch, [
        "presence", "publish", "--disclose", "existence,everything",
    ]) == 1


def test_cli_presence_verify_tampered_is_one(env, monkeypatch, tmp_path):
    card = presence.publish({"visibility": "listed", "fields": {"existence": True}})
    card["seq"] = card["seq"] + 1
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(card), encoding="utf-8")
    assert _run_cli(monkeypatch, ["presence", "verify", str(bad)]) == 1

