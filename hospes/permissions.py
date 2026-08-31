"""The permission matrix as a loadable object, and the map from it to enforcement.

`spec/permission-matrix.yaml` has declared six team roles with their reads,
decides, and exports since 1.0, and **no runtime code has ever read it**.
Enforcement instead lives in nineteen module-local frozensets reached through
seven divergent `_require_role` helpers. Nothing compared the two, so nothing
could notice when they disagreed — and they disagree at eight of nineteen sites,
every one of them in the direction of the code being more permissive than the
contract.

This module is the first half of closing that: it makes the matrix loadable, and
it names — explicitly, in `ENFORCEMENT_SITES` — which capability each enforcement
point is *about*. That second part cannot be inferred. The matrix has 21
capability names sharing only 10 distinct role-sets, so matching a frozenset to
a capability by set equality finds coincidences at a high rate
(`CLEARANCE_DECISION_ROLES` equals `reads:contacts` exactly, and means nothing
like it). The mapping is therefore hand-authored and reviewed, and the predicate
diffs against it rather than guessing.

What this module deliberately does NOT do is enforce anything. Deriving
enforcement from the matrix as currently written would tighten eight sites in a
single flag day — revoking, among others, a producer's ability to decide a
clearance and an editorial owner's ability to read guest history. The divergences
are surfaced first and decided one at a time; `require_capability` comes after
that, not before it.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

import yaml

from hospes.service import HumanRole

CAPABILITY_AXES = ("reads", "decides", "exports")
KNOWN_SCOPES = frozenset({"show", "network"})


class PermissionMatrixError(ValueError):
    """Raised when the declared permission contract is malformed or drifted."""


@dataclass(frozen=True)
class PermissionMatrix:
    roles: frozenset[str]
    capabilities: Mapping[str, frozenset[str]]
    scopes: Mapping[str, str]

    def holders(self, capability: str) -> frozenset[str]:
        try:
            return self.capabilities[capability]
        except KeyError as exc:
            raise PermissionMatrixError(f"matrix declares no capability {capability!r}") from exc

    def declares(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class EnforcementSite:
    """One place the code decides who may act, and the capability it is about.

    `capability` is always a name, even when the matrix does not declare it yet
    — a proposed name such as `decides:distribution_draft` for a site the
    matrix has no vocabulary for. Storing the proposed name rather than `None`
    means the moment a maintainer adds that name to the matrix, `declares()`
    starts returning true and the site is picked up by both `divergences()`
    and `unnamed_sites()` automatically — no second, easily-forgotten code
    edit swapping `None` for the real string is required to activate it.
    """

    module: str
    attribute: str
    capability: str
    note: str

    @property
    def key(self) -> str:
        return f"{self.module}:{self.attribute}"

    def roles(self) -> frozenset[str]:
        """The live value, imported — never re-parsed out of source.

        A predicate that regexes the source can pass while the running code
        does something else. Importing the attribute means the census is of
        what actually executes.
        """
        module = importlib.import_module(f"hospes.{self.module}")
        try:
            value = getattr(module, self.attribute)
        except AttributeError as exc:
            raise PermissionMatrixError(f"enforcement site {self.key} no longer exists — the map is stale") from exc
        if not isinstance(value, frozenset):
            raise PermissionMatrixError(f"enforcement site {self.key} is not a frozenset")
        return frozenset(str(item) for item in value)


# Hand-authored and reviewed. A capability the matrix does not declare yet still
# gets its proposed name here, not `None` — see EnforcementSite's docstring for
# why. `unnamed_sites()` still reports these; the absence is a finding, not a
# resting state, and it is the MATRIX's declaration that decides, not this map.
ENFORCEMENT_SITES: tuple[EnforcementSite, ...] = (
    EnforcementSite("clearances", "AUTHORIZED_CLEARANCE_ROLES", "reads:clearances", "read a clearance row"),
    EnforcementSite("clearances", "CLEARANCE_DECISION_ROLES", "decides:clearances", "clear or deny rights"),
    EnforcementSite(
        "contact_roster",
        "AUTHORIZED_CONTACT_ROLES",
        "reads:contacts",
        "decrypt publicist/manager/agent name, email, phone, notes",
    ),
    EnforcementSite(
        "touchpoints",
        "AUTHORIZED_TOUCHPOINT_ROLES",
        "reads:touchpoints",
        "decrypt informal interaction notes",
    ),
    EnforcementSite("guest_crm", "AUTHORIZED_HISTORY_ROLES", "reads:guest_history", "read cross-season guest history"),
    EnforcementSite("guest_crm", "DIRECTIVE_ROLES", "decides:do_not_contact", "set a do-not-contact directive"),
    EnforcementSite("distribution", "READ_ROLES", "reads:pipeline", "read the distribution pipeline"),
    EnforcementSite("distribution", "WRITE_ROLES", "decides:distribution_draft", "draft a distribution"),
    EnforcementSite("distribution", "PUBLISH_ROLES", "decides:publication", "authorize publication"),
    EnforcementSite("sponsors", "READ_ROLES", "reads:revenue", "read sponsor revenue"),
    EnforcementSite("sponsors", "WRITE_ROLES", "decides:sponsor_inventory", "declare or assign sponsor slots"),
    EnforcementSite("sponsors", "CLAIM_APPROVAL_ROLES", "decides:sponsor_claim_approval", "approve a sponsor claim"),
    EnforcementSite("network_dashboard", "READ_ROLES", "reads:network_portfolio", "read the network portfolio"),
    EnforcementSite("pilot_service", "DECISION_ROLES", "decides:pilot_runs", "decide a pilot run"),
    EnforcementSite("research_agent", "AUTHORIZED_RESEARCH_ROLES", "decides:research", "start a research job"),
    EnforcementSite("notifications", "TEAM_ROLES", "reads:notifications", "receive notifications"),
    EnforcementSite("notifications", "ASSIGNING_ROLES", "decides:assignments", "assign work"),
    EnforcementSite(
        "encryption",
        "AUTHORIZED_OPERATOR_ROLES",
        "reads:private_fields",
        "operate the field vault",
    ),
)


@dataclass(frozen=True)
class Divergence:
    site: str
    capability: str
    code: frozenset[str]
    declared: frozenset[str]

    @property
    def code_grants_extra(self) -> tuple[str, ...]:
        return tuple(sorted(self.code - self.declared))

    @property
    def code_refuses_granted(self) -> tuple[str, ...]:
        return tuple(sorted(self.declared - self.code))


def _matrix_path() -> Any:
    return files("hospes.resources").joinpath("spec/permission-matrix.yaml")


def load_matrix(path: str | Path | None = None) -> PermissionMatrix:
    try:
        text = Path(path).read_text(encoding="utf-8") if path else _matrix_path().read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise PermissionMatrixError(f"cannot read permission matrix: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PermissionMatrixError("permission matrix must be a YAML object")

    team_roles = raw.get("team_roles")
    if not isinstance(team_roles, Mapping) or not team_roles:
        raise PermissionMatrixError("permission matrix requires team_roles")

    known = {member.value for member in HumanRole}
    unknown = sorted(set(team_roles) - known)
    if unknown:
        raise PermissionMatrixError(f"matrix declares roles absent from HumanRole: {unknown}")
    undeclared = sorted(known - set(team_roles))
    if undeclared:
        raise PermissionMatrixError(f"HumanRole members absent from the matrix: {undeclared}")

    capabilities: dict[str, set[str]] = {}
    scopes: dict[str, str] = {}
    for role, body in team_roles.items():
        if not isinstance(body, Mapping):
            raise PermissionMatrixError(f"role {role} must be an object")
        scope = body.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise PermissionMatrixError(f"role {role} requires a scope")
        scope = scope.strip()
        if scope not in KNOWN_SCOPES:
            raise PermissionMatrixError(f"role {role} scope {scope!r} is not in {sorted(KNOWN_SCOPES)}")
        scopes[role] = scope
        for axis in CAPABILITY_AXES:
            names = body.get(axis)
            if names is None:
                names = []
            elif not isinstance(names, list) or not all(isinstance(item, str) for item in names):
                raise PermissionMatrixError(f"role {role} {axis} must be a list of names")
            for name in names:
                if not name.strip():
                    raise PermissionMatrixError(f"role {role} {axis} has a blank capability name")
                capabilities.setdefault(f"{axis}:{name}", set()).add(role)

    return PermissionMatrix(
        roles=frozenset(team_roles),
        capabilities={key: frozenset(value) for key, value in capabilities.items()},
        scopes=scopes,
    )


def divergences(matrix: PermissionMatrix | None = None) -> list[Divergence]:
    """Sites whose enforced role-set disagrees with the matrix's declaration."""
    matrix = matrix or load_matrix()
    out: list[Divergence] = []
    for site in ENFORCEMENT_SITES:
        if not matrix.declares(site.capability):
            continue
        code = site.roles()
        declared = matrix.holders(site.capability)
        if code != declared:
            out.append(Divergence(site.key, site.capability, code, declared))
    return out


def unnamed_sites(matrix: PermissionMatrix | None = None) -> list[EnforcementSite]:
    """Sites the matrix has no vocabulary for — it cannot govern these at all.

    Widening one of these sites' enforced role set is a real event even though
    no capability comparison can catch it (there is nothing declared yet to
    compare against), so the caller baselines `unnamed_signature`, not just
    `site.key` — a key-only baseline cannot see a site it already lists grant
    a new role.
    """
    matrix = matrix or load_matrix()
    return [site for site in ENFORCEMENT_SITES if not matrix.declares(site.capability)]


def unnamed_signature(site: EnforcementSite) -> str:
    """`site.key` plus its currently enforced members, for an exact baseline."""
    return f"{site.key}|{','.join(sorted(site.roles()))}"


@dataclass(frozen=True)
class RoleLiteral:
    """A role set written anywhere in the package — named, inline, or in a table."""

    module: str
    scope: str
    kind: str  # "str" | "enum"
    members: tuple[str, ...]
    unknown: tuple[str, ...]  # members outside HumanRole — a typo or an invented role

    @property
    def key(self) -> str:
        return f"{self.module}:{self.scope}"

    @property
    def signature(self) -> str:
        """Key plus members.

        A scope can hold several role sets — `service._RECEIPT_ROLES` maps seven
        receipt types to seven of them — so the key alone cannot express the
        census. Including the members also makes a WIDENED set a new signature,
        which is the point: a site already known to be uncovered must not be
        able to quietly grant more.
        """
        return f"{self.key}|{','.join(self.members)}"


def _role_literal_members(node: Any, roles: frozenset[str]) -> tuple[str, ...] | None:
    """Role names in a set/frozenset literal, whether written as strings or enum members.

    Requiring every element be a *known* role (`literal <= roles`) is a
    structural filter, not just a validity check: this package has hundreds of
    unrelated same-shaped string-set literals (status vocabularies, format
    enums, JSON column lists), and dropping that requirement entirely swept
    all of them into the census as of one earlier revision of this function —
    39 real role sets became 200+. So the full-membership case stays the fast,
    precise path. Alongside it, a NEAR MISS — at least one recognized role
    name and at most one element that is not one — is also accepted: that
    shape is what a genuine role literal with a single typo or an invented
    role looks like, and empirically nothing else in the package matches it
    (verified against every literal here at the time this was written). A
    literal with zero overlap, or more than one unrecognized element, is not
    treated as role-shaped at all — it is far more likely to be unrelated data
    than a role set with multiple typos.
    """
    import ast

    elements = None
    if isinstance(node, ast.Set):
        elements = node.elts
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
        and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        elements = node.args[0].elts
    if not elements:
        return None

    literal = {e.value for e in elements if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if literal and len(literal) == len(elements) and _role_shaped(literal, roles):
        return tuple(sorted(literal))

    enum = {
        e.attr.lower()
        for e in elements
        if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id == "HumanRole"
    }
    if enum and len(enum) == len(elements) and _role_shaped(enum, roles):
        return tuple(sorted(enum))
    return None


def _role_shaped(candidate: set[str], roles: frozenset[str]) -> bool:
    if candidate <= roles:
        return True
    return bool(candidate & roles) and len(candidate - roles) <= 1


def role_literals(package_dir: Path | None = None) -> list[RoleLiteral]:
    """Every role set literal in the package, by AST — not just the named ones.

    The import-based census in :func:`divergences` can only see role sets bound
    to a module-level name. It is therefore blind to a set written inline at a
    call site, and to a table of them (``service._RECEIPT_ROLES`` maps seven
    receipt types to seven role sets). Twenty-one of thirty-nine role decisions
    in this package are invisible that way, so a gate reporting only the named
    ones would report a coverage number that is wrong by more than half.

    Sites are keyed by enclosing scope rather than line number so the key
    survives ordinary edits, and carry their members so a widened set reds even
    when the site itself is already known.
    """
    import ast

    base = package_dir or Path(__file__).resolve().parent
    roles = frozenset(member.value for member in HumanRole)
    out: list[RoleLiteral] = []

    for path in sorted(base.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue
        relative_module = path.relative_to(base).with_suffix("")
        module = ".".join(relative_module.parts)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue

        # The inner literal of `frozenset({...})` is the same site as the call.
        skip: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset"
                and node.args
            ):
                skip.add(id(node.args[0]))

        # Name every node's enclosing scope, so a key does not depend on a line.
        scope_of: dict[int, str] = {}
        for parent in ast.walk(tree):
            name = None
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = parent.name
            elif isinstance(parent, ast.Assign) and parent.targets and isinstance(parent.targets[0], ast.Name):
                name = parent.targets[0].id
            if name:
                for child in ast.walk(parent):
                    scope_of.setdefault(id(child), name)

        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            members = _role_literal_members(node, roles)
            if members is None:
                continue
            kind = (
                "enum" if isinstance(node, ast.Set) and any(isinstance(e, ast.Attribute) for e in node.elts) else "str"
            )
            out.append(
                RoleLiteral(
                    module=module,
                    scope=scope_of.get(id(node), "<module>"),
                    kind=kind,
                    members=members,
                    unknown=tuple(sorted(set(members) - roles)),
                )
            )
    return out


def uncovered_role_literals(package_dir: Path | None = None) -> list[RoleLiteral]:
    """Role sets no ENFORCEMENT_SITES entry accounts for."""
    mapped = {site.key for site in ENFORCEMENT_SITES}
    return [item for item in role_literals(package_dir) if item.key not in mapped]


def unknown_role_literals(package_dir: Path | None = None) -> list[RoleLiteral]:
    """Every role-set literal in the package naming a role HumanRole does not have.

    `unknown_roles()` only ever sees ENFORCEMENT_SITES, so a brand-new inline or
    table-based site naming an invalid role (a typo, a made-up role) previously
    vanished before either check saw it: `_role_literal_members` used to filter
    those literals out entirely rather than return them for `role_literals()`
    to collect. This is the counterpart for the wider census.
    """
    return [item for item in role_literals(package_dir) if item.unknown]


def unknown_roles() -> dict[str, tuple[str, ...]]:
    """Any role a site enforces that the matrix does not know.

    A site enforcing a NARROWER set than the matrix is legitimate — the CLI's
    lifecycle role choices exclude `editor` on purpose, and the demo personas are
    a deliberate subset. A site enforcing a role the matrix has never heard of is
    drift, and that is what this reports.
    """
    matrix = load_matrix()
    out: dict[str, tuple[str, ...]] = {}
    for site in ENFORCEMENT_SITES:
        stray = tuple(sorted(site.roles() - matrix.roles))
        if stray:
            out[site.key] = stray
    return out


__all__ = [
    "CAPABILITY_AXES",
    "KNOWN_SCOPES",
    "Divergence",
    "ENFORCEMENT_SITES",
    "EnforcementSite",
    "PermissionMatrix",
    "PermissionMatrixError",
    "RoleLiteral",
    "divergences",
    "load_matrix",
    "role_literals",
    "uncovered_role_literals",
    "unknown_role_literals",
    "unknown_roles",
    "unnamed_signature",
    "unnamed_sites",
]
