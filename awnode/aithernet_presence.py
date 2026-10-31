"""This node's Presence Card: a signed, expiring, per-field-disclosed assertion.

WHAT THIS IS
------------
The AitherNet's front door is one small document. A node says "here is who I am,
here is the little I am willing to disclose, and I am saying it NOW" -- signs it
with an Ed25519 key, and serves the REDACTED form at
``/.well-known/aithernet/node.json``. A stranger can verify it offline against the
key embedded in ``identity.node_id`` with no registry, no account and no network.

The rules that make the card worth verifying (plan §9, grafts K1-K4):

* **Signed.** ``sig`` is Ed25519 over the card body (everything except ``sig``)
  in canonical JSON -- sorted keys, no whitespace, UTF-8. Any change to any byte
  of the body invalidates it: verification fails CLOSED, never "valid but
  unverified".
* **Expiring.** ``expires_at`` defaults to fifteen minutes. A node that stops
  re-asserting loses its listing instead of leaving a stale one behind.
* **Per-field.** Six disclosure bits, every one defaulting to ``false``.
  ``redact()`` emits ONLY the fields whose bit is true; with ``existence`` false
  nothing but the signature envelope leaves the box.
* **Revocable.** A withdrawal is a signed tombstone at ``seq + 1``:
  ``visibility=private``, every bit false, no content keys.

WHERE THE KEY COMES FROM
------------------------
Preference order (first hit wins):

1. ``AITHERNET_PRESENCE_KEY_PATH`` -- an explicit file, for tests and operators.
2. The device key ``adk enroll`` already made with awseal
   (``AWSEAL_KEY_PATH``, else ``~/.aither/awseal/signing.key``). awnode is a facet
   of that device, so the card is signed by the key the device already enrolled.
3. ``~/.aither/aithernet/node_ed25519``, created 0600 on first use.

``cryptography`` is required. A seal library that degrades to "signing
unavailable, continuing" is the failure awseal exists to prevent; this module
raises rather than serve an unverifiable document.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # The verifier must be able to SAY it cannot judge, never silently pass.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - the import-failure path is exercised
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    serialization = None  # type: ignore[assignment]
    Ed25519PrivateKey = None  # type: ignore[assignment]
    Ed25519PublicKey = None  # type: ignore[assignment]
    _HAVE_CRYPTO = False

log = logging.getLogger("awnode.presence")

SCHEMA = "aither.presence/1"
STATE_SCHEMA = "aither.presence.state/1"

#: The visibility ladder. Only ``listed`` may reach a global index, and even then
#: only the fields whose bits are true.
VISIBILITIES = ("private", "circle", "mesh", "listed")

#: The ladder as ranks: a viewer may see a card whose rank is at least its own.
#: ``public`` is an anonymous stranger, so it needs the widest (``listed``).
_VISIBILITY_RANK = {name: index for index, name in enumerate(VISIBILITIES)}
_AUDIENCE_RANK = {"circle": 1, "mesh": 2, "public": 3, "listed": 3}

#: The disclosure bitmask. Order is display order; every bit defaults to False.
FIELDS = ("existence", "hostname", "capacity", "owner_name", "spaces", "live_status")

#: What the node is willing to do for the network. Off unless the owner opted in.
VOLUNTEER_MODES = ("off", "embed", "inference", "training")

DEFAULT_TTL_SECONDS = 15 * 60

SIGNING_DOMAIN = b"aither.presence/1\n"

#: The fixed envelope a redacted card always carries (``sig`` is added by signing).
#: None of these reveal a node fact -- they are the assertion's own coordinates and
#: its disclosure decision.
ENVELOPE_KEYS = frozenset({
    "schema", "identity", "visibility", "fields", "seq", "asserted_at", "expires_at",
})

#: Content keys and the disclosure bit that licenses each one.
CONTENT_BITS = {
    "hostname": "hostname",
    "lends": "capacity",
    "volunteer": "capacity",
    "display_name": "owner_name",
    "spaces": "spaces",
    "live_status": "live_status",
}

#: Keys that are NEVER public, whatever the bitmask says: third-party grants and
#: data-donation categories are about OTHER people, not about this node's owner.
NEVER_PUBLIC = ("data_donation", "share_with")

IDENTITY_KEYS = frozenset({"account_id", "node_id", "tenant_id", "mesh_id"})

ALLOWED_KEYS = frozenset(ENVELOPE_KEYS | set(CONTENT_BITS) | set(NEVER_PUBLIC) | {"sig"})


# ── Errors ───────────────────────────────────────────────────────────────────


class PresenceError(ValueError):
    """Base class: anything that makes a card unusable. Fail closed."""


class PresenceSchemaError(PresenceError):
    """Unknown schema, missing key, wrong type, or an undeclared key."""


class PresenceSignatureError(PresenceError):
    """The signature does not verify, or is not bound to the claimed node id."""


class PresenceExpiredError(PresenceError):
    """``expires_at`` has passed: the assertion was not re-asserted."""


class PresenceUnreadableError(PresenceError):
    """The card could not be read at all, so no verdict was reached.

    Distinct from every other failure on purpose: "I never saw it" must not be
    reported as "I saw it and it was bad", or a typo'd path looks like a forgery.
    """


class PresenceStaleError(PresenceError):
    """The card's ``seq`` is below the newest assertion the caller has seen.

    A card is safe to verify offline, but it cannot know on its own that a later
    signed assertion -- most importantly a withdrawal tombstone at ``seq + 1`` --
    supersedes it. A caller that holds that newer document passes its ``seq`` as
    ``min_seq`` so a replayed pre-withdrawal card is refused instead of verified.
    """


class PresenceDuplicateKeyError(PresenceSchemaError):
    """The JSON document repeats a key, so its bytes and its parsed card differ.

    The signature covers canonical JSON of the PARSED body (one key per name). A
    document that repeats a key is therefore ambiguous: a last-wins parser and a
    first-wins parser read different cards under the same signature. Refuse it
    rather than elect a winner -- "any change to any byte invalidates it" has to
    mean exactly that.
    """


# ── Canonical form ───────────────────────────────────────────────────────────


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. The ONE byte string that gets signed."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def signing_bytes(card: Dict[str, Any]) -> bytes:
    """The signed payload: a domain tag plus the card body without ``sig``.

    The domain tag keeps a Presence Card signature from ever being replayable as a
    signature over some other document that happens to canonicalise the same way.
    """
    body = {k: v for k, v in card.items() if k != "sig"}
    return SIGNING_DOMAIN + canonical_json(body)


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    """``object_pairs_hook`` that refuses a repeated key instead of choosing one."""
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise PresenceDuplicateKeyError(
                f"duplicate JSON key {key!r}: a card must have exactly one "
                "representation, or its bytes and its parsed body can disagree"
            )
        obj[key] = value
    return obj


def loads_card(text: str) -> Any:
    """Parse card JSON, refusing duplicate keys.

    ``verify_file`` uses this so a document whose bytes differ from the signed
    canonical body cannot verify: the signature covers one value per key, and a
    repeated key makes "which one" parser-dependent.
    """
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except PresenceDuplicateKeyError:
        raise
    except ValueError as exc:
        raise PresenceSchemaError(f"not JSON: {exc}") from exc


def _check_json_safe(value: Any, path: str = "card") -> None:
    """Refuse values ``canonical_json`` cannot encode: NaN/Infinity and lone surrogates.

    ``json.loads`` happily produces NaN/Infinity, and a Python ``str`` can hold a
    lone surrogate, but the signed form is UTF-8 with ``allow_nan=False``. Letting
    either through turns "this card is invalid" into an uncaught ValueError or
    UnicodeEncodeError -- a 500 on the serving surface, or a traceback from the
    checker. A parse/encode failure must never outrank the verdict.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PresenceSchemaError(f"{path} has a non-string key {key!r}")
            _check_json_safe(key, path)
            _check_json_safe(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_json_safe(item, f"{path}[{index}]")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PresenceSchemaError(
                f"{path} carries a character that is not valid UTF-8: {exc}"
            ) from exc
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise PresenceSchemaError(f"{path} is {value!r}, which JSON cannot carry")
    elif value is None or isinstance(value, (bool, int)):
        return
    else:  # pragma: no cover - json.loads cannot produce one, but a caller can
        raise PresenceSchemaError(
            f"{path} carries a non-JSON value of type {type(value).__name__}"
        )


# ── Vocabulary (lends may only name a kind the platform recognises) ───────────


_VOCAB_CACHE: Dict[str, Tuple[str, ...]] = {}


def clear_vocabulary_cache() -> None:
    """Forget the cached vocabulary (tests and long-lived processes)."""
    _VOCAB_CACHE.clear()


def capability_kinds() -> Tuple[str, ...]:
    """The node-capability vocabulary: the yaml when in a checkout, else awdk's KINDS.

    ``AitherOS/config/node_capabilities.yaml`` is the single source; awnode depends
    on awdk, whose ``KINDS`` mirrors it under a checker, so a pip-installed node
    (where the monorepo yaml is absent) can still judge a card. If neither is
    readable the caller MUST fail closed -- an unjudgeable ``lends`` is not a pass.
    """
    if "kinds" in _VOCAB_CACHE:
        return _VOCAB_CACHE["kinds"]
    kinds = _kinds_from_yaml() or _kinds_from_adk()
    if not kinds:
        raise PresenceError(
            "node capability vocabulary unavailable: neither "
            "AitherOS/config/node_capabilities.yaml nor adk.node_capabilities.KINDS "
            "could be read, so `lends` cannot be judged"
        )
    _VOCAB_CACHE["kinds"] = kinds
    return kinds


def _kinds_from_yaml() -> Tuple[str, ...]:
    explicit = (os.environ.get("AITHER_NODE_CAPABILITIES") or "").strip()
    candidates: List[Path] = [Path(explicit)] if explicit else []
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "AitherOS" / "config" / "node_capabilities.yaml")
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            import yaml  # awnode already depends on PyYAML

            data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            kinds = tuple(str(k) for k in (data.get("kinds") or {}))
            if kinds:
                return kinds
        except (OSError, ValueError, ImportError) as exc:
            log.debug("presence: cannot read %s (%s)", cand, exc)
    return ()


def _kinds_from_adk() -> Tuple[str, ...]:
    try:
        from adk.node_capabilities import KINDS

        return tuple(str(k) for k in KINDS)
    except (ImportError, AttributeError) as exc:
        log.debug("presence: adk.node_capabilities unavailable (%s)", exc)
        return ()


def validate_lends(lends: Any) -> List[str]:
    """Return ``lends`` as a list, or raise when a kind is outside the vocabulary."""
    if lends is None:
        return []
    if not isinstance(lends, (list, tuple)):
        raise PresenceSchemaError("lends must be a list of capability kinds")
    vocabulary = capability_kinds()
    out: List[str] = []
    for kind in lends:
        if not isinstance(kind, str) or kind not in vocabulary:
            raise PresenceSchemaError(
                f"lends names {kind!r}, which is not a node capability kind "
                f"(known: {', '.join(vocabulary)})"
            )
        if kind not in out:
            out.append(kind)
    return out


# ── Time ─────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_time(moment: datetime) -> str:
    """ISO-8601 UTC, second resolution, trailing ``Z`` (3.10-compatible parse)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_time(value: Any, *, name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 stamp; a naive stamp is read as UTC. Raise, never default."""
    if not isinstance(value, str) or not value.strip():
        raise PresenceSchemaError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PresenceSchemaError(f"{name} is not ISO-8601: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ── Keys ─────────────────────────────────────────────────────────────────────


def _aither_home() -> Path:
    return Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))


def device_key_path() -> Path:
    """The Ed25519 key ``adk enroll`` made (awseal's device signing key)."""
    explicit = (os.environ.get("AWSEAL_KEY_PATH") or "").strip()
    return Path(explicit) if explicit else Path.home() / ".aither" / "awseal" / "signing.key"


def node_key_path() -> Path:
    """The AitherNet node key: this module's own, created on first use."""
    explicit = (os.environ.get("AITHERNET_PRESENCE_KEY_PATH") or "").strip()
    return Path(explicit) if explicit else _aither_home() / "aithernet" / "node_ed25519"


def key_path() -> Path:
    """The file the card will be signed with (may not exist yet)."""
    explicit = (os.environ.get("AITHERNET_PRESENCE_KEY_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    device = device_key_path()
    if device.is_file():
        return device
    return node_key_path()


def key_source() -> str:
    """``env`` | ``device`` | ``aithernet`` -- which key is in use, for a report."""
    if (os.environ.get("AITHERNET_PRESENCE_KEY_PATH") or "").strip():
        return "env"
    return "device" if device_key_path().is_file() else "aithernet"


def _require_crypto() -> None:
    if not _HAVE_CRYPTO:
        raise PresenceError(
            "the `cryptography` package is required to sign or verify a presence "
            "card. Refusing rather than emitting an unverifiable document"
        )


def _restrict_to_owner(path: Path) -> bool:
    """Best-effort owner-only access. Returns False and WARNS when it cannot.

    POSIX honours ``0600``. Windows ignores the mode and reports success anyway --
    ``os.chmod`` there only toggles the read-only attribute -- so the old "could
    not restrict" branch could never fire while the signing key and the state file
    kept inherited ACLs (SYSTEM/Administrators/every local account could read the
    private key). On Windows we rewrite the DACL: drop inherited ACEs and grant
    the current user alone.
    """
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("presence: could not restrict %s to the owner (%s)", path, exc)
        return False
    if os.name != "nt":
        return True
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        log.warning("presence: could not restrict %s: USERNAME is not set", path)
        return False
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("presence: could not restrict %s to the owner (%s)", path, exc)
        return False
    return True


def _create_key(path: Path) -> Path:
    """Create a PKCS#8 Ed25519 private key, mode 0600, never overwriting one."""
    _require_crypto()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path  # another facet won the race; use the key that is there
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    _restrict_to_owner(path)
    return path


def load_private_key(path: Optional[Path] = None):
    """Load (creating on first use) this node's Ed25519 signing key."""
    _require_crypto()
    target = Path(path) if path is not None else key_path()
    if not target.is_file():
        target = _create_key(target)
    try:
        return serialization.load_pem_private_key(target.read_bytes(), password=None)
    except (OSError, ValueError) as exc:
        raise PresenceError(f"cannot load the presence signing key at {target}: {exc}") from exc


def public_key_hex(private_key: Any = None, *, path: Optional[Path] = None) -> str:
    """The public half, hex -- safe to publish, and meant to be."""
    _require_crypto()
    key = private_key if private_key is not None else load_private_key(path)
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw.hex()


def node_id(private_key: Any = None, *, path: Optional[Path] = None) -> str:
    """The offline-verifiable node id: ``ed25519:<32-byte public key, hex>``."""
    return "ed25519:" + public_key_hex(private_key, path=path)


# ── Build / sign / redact ────────────────────────────────────────────────────


def _identity(account_id: str = "", tenant_id: str = "", mesh_id: Optional[str] = None,
              private_key: Any = None, *, path: Optional[Path] = None) -> Dict[str, Any]:
    key = private_key if private_key is not None else load_private_key(path)
    return {
        "account_id": str(account_id or ""),
        "node_id": node_id(key),
        "tenant_id": str(tenant_id or ""),
        "mesh_id": mesh_id,
    }


def local_identity() -> Dict[str, str]:
    """Account/tenant ids this box already knows (absent keys are empty strings).

    Read from the enrollment state awnode already keeps. The node id is NEVER
    taken from here -- it is derived from the signing key, so it cannot be spoofed
    by editing a state file.
    """
    aither_home = _aither_home()
    found: Dict[str, str] = {}
    for name in ("sync_state.json", "device.json"):
        try:
            loaded = json.loads((aither_home / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        for key in ("account_id", "tenant_id"):
            if loaded.get(key) and key not in found:
                found[key] = str(loaded[key])
    return {
        "account_id": found.get("account_id", ""),
        "tenant_id": found.get("tenant_id", ""),
    }


def build_card(
    *,
    account_id: str = "",
    tenant_id: str = "",
    mesh_id: Optional[str] = None,
    display_name: str = "",
    visibility: str = "private",
    fields: Optional[Dict[str, Any]] = None,
    lends: Optional[Iterable[str]] = None,
    volunteer: str = "off",
    data_donation: Optional[Dict[str, Any]] = None,
    share_with: Optional[List[Dict[str, Any]]] = None,
    hostname: Optional[str] = None,
    spaces: Optional[List[str]] = None,
    live_status: Optional[str] = None,
    seq: int = 1,
    asserted_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    private_key: Any = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build an UNSIGNED card. The one place defaults are applied.

    Every disclosure bit defaults to false, visibility defaults to ``private``,
    volunteering defaults to ``off``: a node that says nothing is invisible.
    """
    if visibility not in VISIBILITIES:
        raise PresenceSchemaError(
            f"visibility must be one of {', '.join(VISIBILITIES)}; got {visibility!r}"
        )
    bits = {name: False for name in FIELDS}
    if fields is not None:
        if not isinstance(fields, dict):
            raise PresenceSchemaError("fields must be an object of disclosure bits")
        unknown = sorted(set(fields) - set(FIELDS))
        if unknown:
            raise PresenceSchemaError(f"unknown disclosure bit(s): {', '.join(unknown)}")
        for name, value in fields.items():
            if not isinstance(value, bool):
                raise PresenceSchemaError(f"disclosure bit {name!r} must be true or false")
            bits[name] = value
    if volunteer not in VOLUNTEER_MODES:
        raise PresenceSchemaError(
            f"volunteer must be one of {', '.join(VOLUNTEER_MODES)}; got {volunteer!r}"
        )
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise PresenceSchemaError("seq must be a non-negative integer")
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise PresenceSchemaError(f"ttl_seconds must be an integer; got {ttl_seconds!r}")
    if ttl_seconds <= 0:
        raise PresenceSchemaError("ttl_seconds must be positive")
    if data_donation is not None and not isinstance(data_donation, dict):
        raise PresenceSchemaError("data_donation must be an object")
    if share_with is not None and not isinstance(share_with, (list, tuple)):
        raise PresenceSchemaError("share_with must be a list")
    if spaces is not None and not isinstance(spaces, (list, tuple)):
        raise PresenceSchemaError("spaces must be a list")

    moment = parse_time(asserted_at, name="asserted_at") if asserted_at else _utcnow()
    if expires_at:
        expiry = parse_time(expires_at, name="expires_at")
    else:
        expiry = moment + timedelta(seconds=ttl_seconds)
    if expiry <= moment:
        raise PresenceSchemaError("expires_at must be after asserted_at")

    key = private_key if private_key is not None else load_private_key(path)
    card: Dict[str, Any] = {
        "schema": SCHEMA,
        "identity": _identity(account_id, tenant_id, mesh_id, key),
        "display_name": str(display_name or ""),
        "visibility": visibility,
        "fields": bits,
        "lends": validate_lends(lends),
        "volunteer": volunteer,
        "data_donation": dict(data_donation or {}),
        "share_with": list(share_with or []),
        "seq": seq,
        "asserted_at": format_time(moment),
        "expires_at": format_time(expiry),
    }
    if hostname is not None:
        card["hostname"] = str(hostname)
    if spaces is not None:
        card["spaces"] = [str(s) for s in spaces]
    if live_status is not None:
        card["live_status"] = str(live_status)
    return card


def _validate_shape(card: Any) -> None:
    """Structural validation shared by sign and verify. Raises PresenceSchemaError."""
    if not isinstance(card, dict):
        raise PresenceSchemaError("a presence card is a JSON object")
    if card.get("schema") != SCHEMA:
        raise PresenceSchemaError(
            f"unknown schema {card.get('schema')!r}; this verifier speaks {SCHEMA}"
        )
    unknown = sorted(set(card) - ALLOWED_KEYS)
    if unknown:
        raise PresenceSchemaError(
            f"card carries key(s) the schema does not define: {', '.join(unknown)}"
        )
    missing = sorted(ENVELOPE_KEYS - set(card))
    if missing:
        raise PresenceSchemaError(f"card is missing required key(s): {', '.join(missing)}")

    ident = card["identity"]
    if not isinstance(ident, dict):
        raise PresenceSchemaError("identity must be an object")
    unknown_ident = sorted(set(ident) - IDENTITY_KEYS)
    if unknown_ident:
        raise PresenceSchemaError(
            f"identity carries key(s) the schema does not define: {', '.join(unknown_ident)}"
        )
    if ident.get("node_id") is not None and not isinstance(ident.get("node_id"), str):
        raise PresenceSchemaError("identity.node_id must be a string")
    for name in ("account_id", "tenant_id", "mesh_id"):
        value = ident.get(name)
        if value is not None and not isinstance(value, str):
            raise PresenceSchemaError(f"identity.{name} must be a string or null")

    if card["visibility"] not in VISIBILITIES:
        raise PresenceSchemaError(f"unknown visibility {card['visibility']!r}")

    bits = card["fields"]
    if not isinstance(bits, dict):
        raise PresenceSchemaError("fields must be an object of disclosure bits")
    if set(bits) != set(FIELDS):
        raise PresenceSchemaError(
            f"fields must carry exactly the disclosure bits {', '.join(FIELDS)}"
        )
    for name, value in bits.items():
        if not isinstance(value, bool):
            raise PresenceSchemaError(f"disclosure bit {name!r} must be true or false")

    if card.get("display_name") is not None and not isinstance(card["display_name"], str):
        raise PresenceSchemaError("display_name must be a string")
    if card.get("hostname") is not None and not isinstance(card["hostname"], str):
        raise PresenceSchemaError("hostname must be a string")
    if card.get("live_status") is not None and not isinstance(card["live_status"], str):
        raise PresenceSchemaError("live_status must be a string")
    if card.get("spaces") is not None and not isinstance(card["spaces"], list):
        raise PresenceSchemaError("spaces must be a list")
    if card.get("data_donation") is not None and not isinstance(card["data_donation"], dict):
        raise PresenceSchemaError("data_donation must be an object")
    if card.get("share_with") is not None and not isinstance(card["share_with"], list):
        raise PresenceSchemaError("share_with must be a list")

    volunteer = card.get("volunteer", "off")
    if volunteer not in VOLUNTEER_MODES:
        raise PresenceSchemaError(f"unknown volunteer mode {volunteer!r}")
    if "lends" in card:
        validate_lends(card["lends"])

    seq = card["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise PresenceSchemaError("seq must be a non-negative integer")

    asserted = parse_time(card["asserted_at"], name="asserted_at")
    expiry = parse_time(card["expires_at"], name="expires_at")
    if expiry <= asserted:
        raise PresenceSchemaError("expires_at must be after asserted_at")

    # Last, and over the WHOLE body: canonical_json must never raise on a card
    # this function has declared well-formed. NaN/Infinity and lone surrogates
    # otherwise escape as ValueError/UnicodeEncodeError (a 500, or a traceback
    # out of the checker) instead of the structured verdict they are owed.
    _check_json_safe(card)


def sign_card(
    card: Dict[str, Any], *, private_key: Any = None, path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Sign a card body; returns a NEW dict carrying ``sig``.

    The signature covers canonical JSON of every key except ``sig``, so the
    verifier can reconstruct exactly the bytes that were signed.
    """
    _require_crypto()
    _validate_shape(card)
    body = {k: v for k, v in card.items() if k != "sig"}
    key = private_key if private_key is not None else load_private_key(path)
    raw = key.sign(signing_bytes(body))
    signed = dict(body)
    signed["sig"] = "ed25519:" + raw.hex()
    return signed


def redact(card: Dict[str, Any]) -> Dict[str, Any]:
    """Emit ONLY the fields whose disclosure bit is true.

    The envelope (schema, identity, visibility, bitmask, seq, times) always
    travels: it is the assertion's coordinates and its disclosure decision, and it
    carries no node fact. ``identity`` is reduced to ``node_id`` -- the public key
    the signature binds to, which is what makes offline verification possible.
    ``account_id``, ``tenant_id`` and ``mesh_id`` ARE node facts and never leave
    the box, whatever the bits say. ``existence`` gates every content key, so with
    that bit false the caller gets nothing but the signature envelope.
    ``data_donation`` and ``share_with`` are never emitted -- they describe other
    people's grants.
    """
    _validate_shape(card)
    bits = card["fields"]
    ident = card["identity"]
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "identity": {key: ident[key] for key in ("node_id",) if key in ident},
        "visibility": card["visibility"],
        "fields": {name: bool(bits[name]) for name in FIELDS},
        "seq": card["seq"],
        "asserted_at": card["asserted_at"],
        "expires_at": card["expires_at"],
    }
    if not bits["existence"]:
        return out
    for key, bit in CONTENT_BITS.items():
        if not bits[bit]:
            continue
        if key == "lends":
            out["lends"] = list(card.get("lends") or [])
        elif key == "volunteer":
            out["volunteer"] = card.get("volunteer", "off")
        elif key == "display_name":
            if card.get("display_name"):
                out["display_name"] = card["display_name"]
        elif key == "spaces":
            out["spaces"] = list(card.get("spaces") or [])
        else:
            value = card.get(key)
            if value:
                out[key] = value
    return out


def disclosure_leaks(card: Dict[str, Any]) -> List[str]:
    """Content the card carries while its disclosure bit says it should not.

    A signed card can be perfectly valid and still be a leak: the signature covers
    whatever the node chose to put in the document. This is the rule that makes
    "listed" mean "exactly these fields" instead of "everything I have".
    """
    if not isinstance(card, dict):
        return ["a presence card must be a JSON object"]
    leaks: List[str] = []
    for key in NEVER_PUBLIC:
        if key in card:
            leaks.append(f"{key!r} is never public but the card carries it")
    ident = card.get("identity")
    if isinstance(ident, dict):
        # The node_id (public key) is the one identity value a published card
        # needs; account/tenant/mesh ids are account facts, not assertion
        # coordinates, and a good signature over them is still a leak.
        for key in ("account_id", "tenant_id", "mesh_id"):
            if ident.get(key):
                leaks.append(
                    f"identity.{key} is a node fact and must not be published"
                )
    bits = card.get("fields") if isinstance(card.get("fields"), dict) else {}
    if card.get("visibility") == "private":
        present = sorted(set(card) & set(CONTENT_BITS))
        if present:
            leaks.append(
                f"visibility is private but the card carries {', '.join(present)}"
            )
        return leaks
    if not bits.get("existence"):
        present = sorted(set(card) & set(CONTENT_BITS))
        if present:
            leaks.append(
                "disclosure bit 'existence' is false but the card carries "
                f"{', '.join(present)}"
            )
        return leaks
    for key, bit in CONTENT_BITS.items():
        if key in card and not bits.get(bit):
            leaks.append(f"{key!r} is present while its disclosure bit {bit!r} is false")
    return leaks


def is_tombstone(card: Any) -> bool:
    """A signed withdrawal: private, no bits set, no content."""
    if not isinstance(card, dict):
        return False
    if card.get("visibility") != "private":
        return False
    bits = card.get("fields")
    if not isinstance(bits, dict) or set(bits) != set(FIELDS):
        return False
    if any(bool(v) for v in bits.values()):
        return False
    return not (set(card) & set(CONTENT_BITS))


def tombstone(
    *,
    seq: int,
    identity: Optional[Dict[str, Any]] = None,
    asserted_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    private_key: Any = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build and sign the withdrawal at ``seq`` (callers pass the PREVIOUS seq + 1)."""
    key = private_key if private_key is not None else load_private_key(path)
    # A tombstone is PUBLIC, so it carries exactly the identity a served card
    # does: the public key, never the owner's account/tenant/mesh ids.
    base_identity = dict(identity) if identity else _identity(private_key=key)
    body = {
        "schema": SCHEMA,
        "identity": {k: base_identity[k] for k in ("node_id",) if k in base_identity},
        "visibility": "private",
        "fields": {name: False for name in FIELDS},
        "seq": int(seq),
        "asserted_at": "",
        "expires_at": "",
    }
    moment = parse_time(asserted_at, name="asserted_at") if asserted_at else _utcnow()
    expiry = (
        parse_time(expires_at, name="expires_at")
        if expires_at
        else moment + timedelta(seconds=int(ttl_seconds))
    )
    if expiry <= moment:
        raise PresenceSchemaError("expires_at must be after asserted_at")
    body["asserted_at"] = format_time(moment)
    body["expires_at"] = format_time(expiry)
    return sign_card(body, private_key=key)


# ── Verify ───────────────────────────────────────────────────────────────────


def verify(
    card: Any,
    *,
    now: Optional[datetime] = None,
    expected_pubkey: Optional[str] = None,
    allow_expired: bool = False,
    min_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """Verify a signed card. RAISES on any failure -- there is no "valid but dull".

    Checks, in order: the schema is known; the shape is exactly what
    ``aither.presence/1`` defines (an undeclared key is rejected, so nothing can
    ride along unredacted); ``lends`` names only vocabulary kinds; the card has not
    expired; ``identity.node_id`` embeds the key that actually signed the body.
    Returns a summary, never a bare boolean: the caller usually wants to know
    whether it was a tombstone and when it expires.

    ``min_seq`` is the newest sequence number the caller has already seen. A card
    below it is ``PresenceStaleError``: this is how a signed withdrawal tombstone
    (``seq + 1``) revokes a card the caller still holds. Without that context a
    card is only asserted to have been true when it was signed.
    """
    _require_crypto()
    _validate_shape(card)

    if min_seq is not None:
        if not isinstance(min_seq, int) or isinstance(min_seq, bool) or min_seq < 0:
            raise PresenceSchemaError("min_seq must be a non-negative integer")
        if card["seq"] < min_seq:
            raise PresenceStaleError(
                f"card seq {card['seq']} is below the newest assertion seen "
                f"(seq {min_seq}); a later signed document supersedes it"
            )

    ident = card["identity"]
    claimed = ident.get("node_id") or ""
    if not isinstance(claimed, str) or not claimed.startswith("ed25519:"):
        raise PresenceSignatureError(
            "identity.node_id must be 'ed25519:<hex public key>' so the card can be "
            "verified offline"
        )
    pub_hex = claimed[len("ed25519:"):].strip().lower()
    try:
        raw_pub = bytes.fromhex(pub_hex)
    except ValueError as exc:
        raise PresenceSignatureError(f"identity.node_id is not hex: {exc}") from exc
    if len(raw_pub) != 32:
        raise PresenceSignatureError(
            f"an Ed25519 public key is 32 bytes; identity.node_id carries {len(raw_pub)}"
        )
    if expected_pubkey:
        want = expected_pubkey.strip().lower()
        want = want[len("ed25519:"):] if want.startswith("ed25519:") else want
        if want != pub_hex:
            raise PresenceSignatureError(
                "the card is signed by a different key than the one expected"
            )

    sig_value = card.get("sig") or ""
    if not isinstance(sig_value, str) or not sig_value.startswith("ed25519:"):
        raise PresenceSignatureError("sig must be 'ed25519:<hex signature>'")
    try:
        sig = bytes.fromhex(sig_value[len("ed25519:"):].strip())
    except ValueError as exc:
        raise PresenceSignatureError(f"sig is not hex: {exc}") from exc
    if len(sig) != 64:
        raise PresenceSignatureError(
            f"an Ed25519 signature is 64 bytes; sig carries {len(sig)}"
        )
    try:
        Ed25519PublicKey.from_public_bytes(raw_pub).verify(sig, signing_bytes(card))
    except InvalidSignature as exc:
        raise PresenceSignatureError(
            "signature does not verify: the body was altered, or it was signed by "
            "another key"
        ) from exc

    expiry = parse_time(card["expires_at"], name="expires_at")
    moment = now if now is not None else _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment >= expiry and not allow_expired:
        raise PresenceExpiredError(
            f"the card expired at {card['expires_at']}; the node did not re-assert it"
        )

    return {
        "ok": True,
        "schema": SCHEMA,
        "node_id": claimed,
        "public_key": pub_hex,
        "visibility": card["visibility"],
        "tombstone": is_tombstone(card),
        "seq": card["seq"],
        "asserted_at": card["asserted_at"],
        "expires_at": card["expires_at"],
        "disclosure": dict(card["fields"]),
    }


def verify_file(path: Path, **kwargs: Any) -> Dict[str, Any]:
    """Load a card from a file and verify it.

    An unreadable file is ``PresenceUnreadableError`` ("no verdict"); a file that
    reads but does not hold a card is a schema failure ("invalid").
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PresenceUnreadableError(f"cannot read {path}: {exc}") from exc
    # loads_card refuses duplicate keys: the signature covers ONE value per key,
    # so a repeated key lets the file's bytes disagree with the signed body.
    card = loads_card(raw)
    return verify(card, **kwargs)


# ── Node state: the current published card ───────────────────────────────────


def state_path() -> Path:
    explicit = (os.environ.get("AITHERNET_PRESENCE_STATE") or "").strip()
    return Path(explicit) if explicit else _aither_home() / "aithernet" / "presence.json"


def load_state() -> Dict[str, Any]:
    """The node's presence state; a missing or unreadable file is an empty state.

    Deliberately not an error: a node that never published has no state, and that
    is a 404, not a crash. A CORRUPT state is also empty -- ``current_card()`` then
    verifies and serves nothing rather than serving something half-read.
    """
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except OSError:
        return {}
    except ValueError as exc:
        log.warning("presence: state file is not JSON (%s); treating as empty", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, text: str) -> None:
    """Atomic owner-only write: a half-written card would be served as a bad signature."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    _restrict_to_owner(tmp)
    os.replace(tmp, path)


def save_state(state: Dict[str, Any]) -> None:
    _write_private(state_path(), json.dumps(state, indent=2, sort_keys=True) + "\n")


#: Keys a publish request may set. Identity, seq, timestamps and sig are NOT here:
#: the subject is this machine, never the payload.
PUBLISH_KEYS = frozenset({
    "display_name", "visibility", "fields", "lends", "volunteer", "data_donation",
    "share_with", "hostname", "spaces", "live_status", "ttl_seconds",
})


def _config_from(card: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The reusable publish config carried by a previous full card."""
    if not isinstance(card, dict):
        return {}
    config = {key: card[key] for key in PUBLISH_KEYS if key in card}
    # The previous card's expiry/seq are not config; ttl is recomputed per publish.
    config.pop("ttl_seconds", None)
    return config


def publish(config: Optional[Dict[str, Any]] = None, *,
            private_key: Any = None, path: Optional[Path] = None) -> Dict[str, Any]:
    """Build, redact, sign and persist the current card. Returns the SERVED form.

    ``config`` is merged onto the previous publish, so flipping one switch (or one
    disclosure bit) does not wipe the rest. Unknown keys are refused: a typo must
    not silently produce a card that discloses nothing (or everything).
    """
    body = dict(config or {})
    unknown = sorted(set(body) - PUBLISH_KEYS)
    if unknown:
        raise PresenceSchemaError(f"unknown publish key(s): {', '.join(unknown)}")

    state = load_state()
    config_full = _config_from(state.get("full"))
    config_full.update(body)
    identity = local_identity()
    mesh_id = (os.environ.get("AITHERNET_MESH_ID") or "").strip() or None
    ttl_raw = config_full.pop("ttl_seconds", DEFAULT_TTL_SECONDS)
    if isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int):
        raise PresenceSchemaError(
            f"ttl_seconds must be an integer; got {ttl_raw!r}"
        )
    ttl = ttl_raw

    key = private_key if private_key is not None else load_private_key(path)
    full = build_card(
        account_id=identity["account_id"],
        tenant_id=identity["tenant_id"],
        mesh_id=mesh_id,
        ttl_seconds=ttl,
        seq=int(state.get("seq") or 0) + 1,
        private_key=key,
        **config_full,
    )
    served = sign_card(redact(full), private_key=key)
    # Paranoid self-check: a node must never serve what it cannot itself verify.
    info = verify(served)
    leaks = disclosure_leaks(served)
    if leaks:
        raise PresenceSchemaError(
            "refusing to publish a card that leaks: " + "; ".join(leaks)
        )
    save_state({
        "schema": STATE_SCHEMA,
        "seq": full["seq"],
        "card": served,
        "full": full,
        "node_id": info["node_id"],
    })
    return served


def withdraw(*, private_key: Any = None, path: Optional[Path] = None) -> Dict[str, Any]:
    """Publish a signed tombstone at ``seq + 1`` and persist it in place of the card."""
    state = load_state()
    key = private_key if private_key is not None else load_private_key(path)
    previous_full = state.get("full")
    identity = (
        previous_full.get("identity") if isinstance(previous_full, dict) else None
    )
    stone = tombstone(
        seq=int(state.get("seq") or 0) + 1,
        identity=identity,
        private_key=key,
    )
    verify(stone)  # never persist a withdrawal that does not verify
    save_state({
        "schema": STATE_SCHEMA,
        "seq": stone["seq"],
        "card": stone,
        "full": None,
        "node_id": stone["identity"]["node_id"],
    })
    return stone


def current_card(
    *, now: Optional[datetime] = None, audience: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The card to serve, or None. Fail closed: anything questionable is a 404.

    None means "disclose nothing", and it is the SAME answer for: never published,
    withdrawn (tombstone), expired, private, a bad signature, or a card that leaks
    a field its bitmask forbids.

    ``audience`` applies the visibility ladder to the SERVING surface. With no
    audience the caller is the node itself and any non-private card qualifies.
    ``"public"`` (an anonymous stranger at ``/.well-known/...``) requires
    ``listed``; ``"mesh"`` also accepts ``mesh`` and ``"circle"`` accepts both.
    Every narrower visibility is a 404 to a wider audience, so ``circle`` and
    ``mesh`` are no longer indistinguishable from ``listed`` on the wire.
    """
    card = load_state().get("card")
    if not isinstance(card, dict):
        return None
    try:
        info = verify(card, now=now)
    except PresenceError as exc:
        log.debug("presence: current card is not servable (%s)", exc)
        return None
    if info["tombstone"] or info["visibility"] == "private":
        return None
    if audience is not None:
        required = _AUDIENCE_RANK.get(audience)
        if required is None:
            return None  # unknown audience: fail closed, disclose nothing
        if _VISIBILITY_RANK.get(info["visibility"], 0) < required:
            return None
    if disclosure_leaks(card):
        return None
    return card


def served_bytes(card: Dict[str, Any]) -> bytes:
    """The exact bytes to serve: canonical JSON, so a verifier hashes what it sees."""
    return canonical_json(card)


def now_epoch() -> float:
    """Wall clock, kept in one place so tests can reason about TTLs."""
    return time.time()
