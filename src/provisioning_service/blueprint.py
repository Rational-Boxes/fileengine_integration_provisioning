# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Blueprint parsing, validation, and reference/parameter resolution (SPEC §5).

Pure, side-effect-free logic — no I/O, no core calls — so the novel provisioning
semantics are fully unit-testable. A *blueprint* is the inline JSON document an
integrator posts (§5.0):

    { "name": str, "params": {name: {type, required, default, ...}},
      "root": <node>, "actions": [<action>], "resources": [<resource>] }

where <node> = { "name": str, "ref"?: str, "metadata"?: {}, "acls"?: [...],
"children"?: [<node>] }.

Reference tokens (§5.1):
  - ``${param}``            scalar substitution (or whole-value for map/list params)
  - ``${node:<addr>}``      a folder in *this* blueprint's tree — resolved to the
                            space's freshly-minted uid at apply time (§5.3/§7.1)
  - ``${resource:<ref>}``   a tenant-scoped dependent resource declared in ``resources``
                            (§5.8) — resolved to the created object's id at apply time

Node addressing: the root is addressed by its written ``name``; descendants by their
name-path relative to the root (e.g. ``Approved``, ``Incoming/Rejected``). A node may
also carry an explicit ``ref`` which becomes an additional address.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# --- vocabulary -------------------------------------------------------------

# Declared parameter types (§5.1). ``node`` is a token, not a param type.
PARAM_TYPES = {
    "string", "int", "bool", "url", "enum", "map", "list", "principal", "ref", "secret",
}
# ACL permission letters the platform understands (§5.2 / core ACL model).
PERM_LETTERS = set("rwdluvbsmix")  # + the special CULL_VERSIONS token handled separately

# ${...} token; the inner text is either a bare param name, ``node:<addr>`` or
# ``resource:<ref>``.
_TOKEN = re.compile(r"\$\{([^}]+)\}")


class BlueprintError(ValueError):
    """Raised by :func:`validate_or_raise`; carries the list of messages."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# --- token helpers ----------------------------------------------------------

def _classify(token: str) -> tuple[str, str]:
    """('node'|'resource'|'param', target) for a raw token body."""
    if token.startswith("node:"):
        return "node", token[len("node:"):]
    if token.startswith("resource:"):
        return "resource", token[len("resource:"):]
    return "param", token


def iter_tokens(value: Any) -> Iterable[tuple[str, str]]:
    """Yield (kind, target) for every ``${...}`` token anywhere in ``value``
    (recursing dicts/lists). ``kind`` is 'node' | 'resource' | 'param'."""
    if isinstance(value, str):
        for m in _TOKEN.finditer(value):
            yield _classify(m.group(1))
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_tokens(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from iter_tokens(v)


# --- tree addressing --------------------------------------------------------

def _walk(node: dict, prefix: tuple[str, ...], depth: int):
    """Yield (address_relative_path, node, depth) for a node and descendants.

    The root is yielded at depth 0 with an empty relative prefix; children carry
    their name-path relative to the root."""
    yield (prefix, node, depth)
    for child in node.get("children") or []:
        name = child.get("name", "")
        yield from _walk(child, prefix + (name,), depth + 1)


def node_addresses(root: dict) -> set[str]:
    """Every valid ``${node:<addr>}`` / ``action.folder`` address in the tree:
    the root by its written ``name``, descendants by relative name-path, plus any
    explicit per-node ``ref``."""
    addrs: set[str] = set()
    root_name = root.get("name", "")
    if root_name:
        addrs.add(root_name)
    for rel, node, _depth in _walk(root, (), 0):
        if rel:  # a descendant (root has empty rel)
            addrs.add("/".join(rel))
        ref = node.get("ref")
        if ref:
            addrs.add(ref)
    return addrs


def tree_stats(root: dict) -> tuple[int, int]:
    """(node_count, max_depth) for the tree."""
    count = 0
    max_depth = 0
    for _rel, _node, depth in _walk(root, (), 0):
        count += 1
        max_depth = max(max_depth, depth)
    return count, max_depth


# --- validation -------------------------------------------------------------

def validate(
    doc: dict,
    *,
    max_nodes: int = 500,
    max_depth: int = 12,
    allowed_actions: Optional[set[str]] = None,
    allowed_resources: Optional[set[str]] = None,
) -> list[str]:
    """Return a list of error messages (empty ⇒ valid). Structural + reference
    integrity per §5.5. ``allowed_actions``/``allowed_resources`` (when given) gate
    action ``type`` / resource ``type`` against what is installed/permitted."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["blueprint must be an object"]

    params = doc.get("params") or {}
    if not isinstance(params, dict):
        errors.append("params must be an object of {name: schema}")
        params = {}
    for pname, pschema in params.items():
        ptype = (pschema or {}).get("type") if isinstance(pschema, dict) else None
        if ptype not in PARAM_TYPES:
            errors.append(f"param {pname!r}: unknown/missing type {ptype!r}")

    root = doc.get("root")
    if not isinstance(root, dict) or not root.get("name"):
        errors.append("root must be an object with a name")
        return errors  # nothing else resolves without a tree

    count, depth = tree_stats(root)
    if count > max_nodes:
        errors.append(f"tree too large: {count} nodes > max {max_nodes}")
    if depth > max_depth:
        errors.append(f"tree too deep: {depth} > max {max_depth}")

    # ACL permission letters on every node.
    for _rel, node, _d in _walk(root, (), 0):
        for entry in node.get("acls") or []:
            for effect in ("allow", "deny"):
                for p in entry.get(effect) or []:
                    if p not in PERM_LETTERS and p != "CULL_VERSIONS":
                        errors.append(
                            f"node {node.get('name')!r}: unknown permission {p!r}")

    addrs = node_addresses(root)
    resources = doc.get("resources") or []
    res_refs = {r.get("ref") for r in resources if isinstance(r, dict) and r.get("ref")}

    # resources
    for r in resources:
        if not isinstance(r, dict):
            errors.append("each resource must be an object")
            continue
        if not r.get("ref") or not r.get("name") or not r.get("type"):
            errors.append(f"resource {r.get('ref') or r!r}: needs ref, name, type")
        if allowed_resources is not None and r.get("type") not in allowed_resources:
            errors.append(f"resource type {r.get('type')!r} not permitted")

    # actions
    for a in doc.get("actions") or []:
        if not isinstance(a, dict):
            errors.append("each action must be an object")
            continue
        ref = a.get("ref")
        if not ref or not a.get("type"):
            errors.append(f"action {ref or a!r}: needs ref and type")
        folder = a.get("folder")
        if folder is None or folder not in addrs:
            errors.append(f"action {ref!r}: folder {folder!r} is not a node in the tree")
        if allowed_actions is not None and a.get("type") not in allowed_actions:
            errors.append(f"action {ref!r}: type {a.get('type')!r} not an installed action")

    # every ${node}/${resource}/${param} token resolves to something declared.
    scan = {"root": root, "actions": doc.get("actions"), "resources": resources}
    for kind, target in iter_tokens(scan):
        if kind == "node" and target not in addrs:
            errors.append(f"${{node:{target}}} does not resolve to a tree node")
        elif kind == "resource" and target not in res_refs:
            errors.append(f"${{resource:{target}}} is not a declared resource ref")
        elif kind == "param" and target not in params:
            errors.append(f"${{{target}}} is not a declared param")

    return errors


def validate_or_raise(doc: dict, **kw) -> None:
    errs = validate(doc, **kw)
    if errs:
        raise BlueprintError(errs)


# --- parameter substitution -------------------------------------------------

def resolve_params(value: Any, values: dict, schema: Optional[dict] = None) -> Any:
    """Substitute ``${param}`` tokens with supplied ``values``, recursively.

    - A string that is *exactly* ``${p}`` where ``p`` is a map/list param yields the
      whole value (structured injection, §5.1) — e.g. a webhook context map.
    - Otherwise ``${p}`` scalars are string-interpolated.
    - ``${node:...}`` / ``${resource:...}`` tokens are left untouched here; they are
      resolved at apply time against the created uid/id maps (§7).
    """
    schema = schema or {}
    if isinstance(value, str):
        m = _TOKEN.fullmatch(value)
        if m:
            kind, target = _classify(m.group(1))
            if kind == "param" and target in values:
                # whole-value injection for structured params (or any exact match)
                return values[target]
            return value  # node:/resource:/unknown left as-is
        # inline scalar interpolation of param tokens only
        def _sub(mo: re.Match) -> str:
            kind, target = _classify(mo.group(1))
            if kind == "param" and target in values:
                return str(values[target])
            return mo.group(0)
        return _TOKEN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: resolve_params(v, values, schema) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_params(v, values, schema) for v in value]
    return value


def missing_required_params(doc: dict, values: dict) -> list[str]:
    """Required params (per the schema) with no supplied value and no default."""
    out = []
    for name, ps in (doc.get("params") or {}).items():
        ps = ps or {}
        if ps.get("required") and name not in values and "default" not in ps:
            out.append(name)
    return out


def with_defaults(doc: dict, values: dict) -> dict:
    """Merge declared param defaults under the supplied values."""
    merged = {}
    for name, ps in (doc.get("params") or {}).items():
        if isinstance(ps, dict) and "default" in ps:
            merged[name] = ps["default"]
    merged.update(values or {})
    return merged
