"""Server configuration for scoped settings: scope tree, facility grants, seeded keys.

Loaded from the TOML file named by ``SCOPES_CONFIG``. Example::

    [facility]
    label = "ALS"

    [[beamline]]
    name = "7.0.1"
    branches = ["7.0.1.1", "7.0.1.2"]

    [facility_grants]            # optional; replaces the defaults when present
    "/beamlines/_all/admin" = "admin"
    "/beamlines/_all/owner" = "owner"
    "/BCG_staff" = "maintainer"

    [seed."voice.wake_word"]     # pre-approved keys: spec_from_dict fields minus "key"
    type = "str"
    scopes = ["branch", "user"]
    operator_writable = true

The configured tree seeds the database at startup (add-only); after that the
database is authoritative and facility admins add or remove beamlines and branches
through ``/logbook/scopes/tree``. Write endpoints build chains from it, never from
the client. Scopes not in the tree (other than users) do not exist.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from lightfall_settings import (
    DEFAULT_FACILITY_GRANTS,
    FACILITY,
    ScopeChain,
    ScopeId,
    SettingSpec,
    spec_from_dict,
    validate_facility_grants,
)


class UnknownScope(LookupError):
    """A beamline/branch scope that is not in the configured tree."""


@dataclass(frozen=True)
class ScopeTree:
    label: str = "facility"
    beamlines: tuple[str, ...] = ()
    branch_parent: dict[str, str] = field(default_factory=dict)

    def contains(self, scope: ScopeId) -> bool:
        if scope.kind in ("facility", "user"):
            return True
        if scope.kind == "beamline":
            return scope.name in self.beamlines
        return scope.name in self.branch_parent

    def chain_for(self, scope: ScopeId) -> ScopeChain:
        if not self.contains(scope):
            raise UnknownScope(str(scope))
        if scope.kind == "facility":
            return ScopeChain((FACILITY,))
        if scope.kind in ("beamline", "user"):
            return ScopeChain((FACILITY, scope))
        parent = ScopeId("beamline", self.branch_parent[scope.name])
        return ScopeChain((FACILITY, parent, scope))

    def scopes(self) -> list[ScopeId]:
        return (
            [FACILITY]
            + [ScopeId("beamline", b) for b in self.beamlines]
            + [ScopeId("branch", b) for b in self.branch_parent]
        )


@dataclass(frozen=True)
class ContentPolicy:
    """Limits on shared-content publishes (``[content]``). Admins of the target scope are
    exempt from all but ``hard_max_bundle_bytes``, the largest request the server accepts."""

    max_bundle_bytes: int = 50 * 2**20
    max_uncompressed_bytes: int = 200 * 2**20
    max_files: int = 2000
    hard_max_bundle_bytes: int = 500 * 2**20


def _content_policy(raw: object) -> ContentPolicy:
    if not isinstance(raw, dict):
        raise ValueError("[content] must be a table")
    fields = set(ContentPolicy.__dataclass_fields__)
    unknown = set(raw) - fields
    if unknown:
        raise ValueError(f"unknown [content] keys {sorted(unknown)}")
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"[content] {key} must be a positive integer")
    policy = ContentPolicy(**raw)
    if policy.max_bundle_bytes > policy.hard_max_bundle_bytes:
        raise ValueError("[content] max_bundle_bytes exceeds hard_max_bundle_bytes")
    return policy


@dataclass(frozen=True)
class ScopedConfig:
    tree: ScopeTree = field(default_factory=ScopeTree)
    facility_grants: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_FACILITY_GRANTS)
    )
    seed: tuple[SettingSpec, ...] = ()
    content: ContentPolicy = field(default_factory=ContentPolicy)


def parse_scoped_config(text: str) -> ScopedConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"invalid scopes config: {e}") from None

    label = str(data.get("facility", {}).get("label", "facility"))
    beamlines: list[str] = []
    parent: dict[str, str] = {}
    for bl in data.get("beamline", []):
        name = str(bl["name"])
        ScopeId("beamline", name)  # validates the name
        if name in beamlines:
            raise ValueError(f"duplicate beamline {name!r}")
        beamlines.append(name)
        for br in bl.get("branches", []):
            br = str(br)
            ScopeId("branch", br)
            if br in parent:
                raise ValueError(f"branch {br!r} listed under two beamlines")
            parent[br] = name

    grants = (
        validate_facility_grants(data["facility_grants"])
        if "facility_grants" in data
        else dict(DEFAULT_FACILITY_GRANTS)
    )
    seed = tuple(
        spec_from_dict({"key": key, **fields}) for key, fields in data.get("seed", {}).items()
    )
    content = _content_policy(data["content"]) if "content" in data else ContentPolicy()
    return ScopedConfig(ScopeTree(label, tuple(beamlines), parent), grants, seed, content)


def load_scoped_config(path: str | os.PathLike[str] | None = None) -> ScopedConfig:
    path = path or os.environ.get("SCOPES_CONFIG")
    if not path:
        return ScopedConfig()
    return parse_scoped_config(Path(path).read_text(encoding="utf-8"))
