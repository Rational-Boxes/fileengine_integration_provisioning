# FileEngine Provisioning — Integration Guide

**Script fully-wired FileEngine project spaces from your own system** — folder tree,
ACLs, metadata, **and** per-folder automation (folder-action bindings: webhooks, notify,
sorters, review chains) — from a single **inline JSON blueprint**, customized per project
by typed parameters.

This is a **server-to-server** API (no browser). It complements the
[embedding kit](https://github.com/Rational-Boxes/fileengine_commercial_integration_components)
(the browser-facing UI components): the kit *shows* documents; provisioning *creates the
correctly-wired spaces* they live in.

> **What it is not.** There is no stored template library, no template CRUD, no SPA
> template editor. Blueprints live in **your** system and are posted **inline**. The
> service applies them idempotently and records just enough (space uid, node map, applied
> version, bindings) to reconcile and audit.

---

## Table of contents

1. [Where it sits](#1-where-it-sits)
2. [Authentication & authorization](#2-authentication--authorization)
3. [The blueprint document](#3-the-blueprint-document)
4. [Applying a space](#4-applying-a-space)
5. [Managing spaces](#5-managing-spaces)
6. [Per-space setup API (§6.3)](#6-per-space-setup-api-63)
7. [Tenant-scoped resources](#7-tenant-scoped-resources)
8. [Reconcile (drift sweep)](#8-reconcile-drift-sweep)
9. [Worked example](#9-worked-example)
10. [Deployment & configuration](#10-deployment--configuration)
11. [Errors](#11-errors)
12. [Security model](#12-security-model)
13. [Endpoint & claims reference](#13-endpoint--claims-reference)

---

## 1. Where it sits

The provisioning service is a standalone FastAPI lane (default `:8100`, **AGPL**). It
does not touch document content; it drives the FileEngine core (folders/ACLs/metadata)
and the folder-actions service (automation bindings) as your integration principal.

```
  Your system  ──POST /v1/provisioning/spaces {inline blueprint}──▶  provisioning (:8100)
                                                                        │
                              gRPC (folders, ACLs, metadata) ──────────┤─▶ FileEngine core
                              REST (bindings, resources) ──────────────┴─▶ folder_actions (:8099)
```

Every folder and binding it creates carries a **`managed_by`** marker, so FileEngine
admin UIs flag them as externally managed (and warn before manual edits).

---

## 2. Authentication & authorization

The service authenticates a **bridge-issued token** (shared `FILEENGINE_JWT_SECRET`,
HS256) presented as `Authorization: Bearer <jwt>`. The token must identify an
**integration with the provisioning capability**:

```
amr:          ["integration"]
capabilities: ["provisioning"]     ← required; else 403
```

Your **scope** is carried in the same token and enforced on every call:

| Claim | Meaning | Enforced when |
|---|---|---|
| `prov_tenants` | tenants you may provision (globs, e.g. `["*"]` or `["acme-*"]`) | every request |
| `prov_namespace` | your namespace for tenant-scoped resources (isolates your configs) | resource apply |
| `prov_roots` | folder uids you may root spaces under (empty = the integration's root) | `parent_uid` |
| `prov_principals` | principals you may grant in ACLs | ACL grants |
| `prov_actions` | folder-action machine names you may bind (`"*"` or a list) | each `action.type` |
| `prov_resources` | resource types you may create (`"*"` or a list) | each `resource.type` |
| `aip` | caller IPs bound to the token (optional; source-IP defense-in-depth) | when `PROV_ENFORCE_AIP` |

You obtain this token from the FileEngine deployment as part of your registration — a
bridge-minted **integration token** scoped to your provisioning grant. (In deployments
that enable the token-exchange service path, this is the `token_type:service` outcome
scoped for provisioning.) The `prov_*` claims **are** your grant — treat the token as a
capability.

Check what a token resolves to:

```
GET /v1/provisioning/whoami        Authorization: Bearer <jwt>
→ { integration_id, tenant, namespace, capabilities }
```

---

## 3. The blueprint document

A blueprint is a single JSON object with four sections. Only `name` and `root` are
required.

```jsonc
{
  "name": "std-project",                 // logical name (stamped on the space root)
  "params": { … },                       // §3.1 typed, validated per-space inputs
  "root":   { … },                       // §3.2 the folder tree (+ ACLs + metadata)
  "actions":   [ … ],                    // §3.3 per-folder automation bindings
  "resources": [ … ]                     // §3.4 tenant-scoped configs the actions depend on
}
```

### 3.1 `params` — typed inputs

`params` is a map of `{ name: { type, required?, default?, ... } }`. Values are supplied
per space in the request's `params` and validated against these declarations.

| type | notes |
|---|---|
| `string`, `int`, `bool` | scalars |
| `url` | a URL (e.g. a webhook callback) |
| `enum` | one of a declared set |
| `map` | a structured object — injected **whole** (e.g. a webhook **context data map**) |
| `list` | an array — injected whole (e.g. notify recipients) |
| `principal` | a role/user/claim principal (validated against the tenant) |
| `ref` | a reference to a declared `resource` |
| `secret` | **write-only** — never returned by the config API (redacted as `***`) |

### 3.2 `root` — the folder tree

```jsonc
"root": {
  "name": "${code}",                                  // the space root folder name
  "metadata": { "project.stage": "active" },          // arbitrary metadata
  "acls": [
    { "principal": "role:${lead}", "allow": ["r","w"] }
  ],
  "children": [
    { "name": "Incoming" },
    { "name": "Approved" },
    { "name": "Rejected", "ref": "rej" }              // optional explicit address
  ]
}
```

A **node** is `{ name, ref?, metadata?, acls?, children? }`. Tree size is bounded
(`PROV_MAX_TREE_NODES`, `PROV_MAX_TREE_DEPTH`).

### 3.3 `actions` — per-folder automation

Bind any installed folder-action (by machine name) to a folder in the tree:

```jsonc
"actions": [
  { "ref": "route", "folder": "Incoming", "type": "move_review",
    "on_events": ["upload"], "mime_types": ["application/pdf"],
    "config": { "on_approved": "${node:Approved}", "cs": "${resource:cs}" } },
  { "ref": "hook", "folder": "Incoming", "type": "webhook",
    "config": { "url": "${webhook_url}", "context": "${webhook_ctx}" } }
]
```

`folder` addresses a node in *this* blueprint (see addressing below). `type` must be in
your `prov_actions` scope.

### 3.4 `resources` — tenant-scoped dependent configs

Declare tenant-scoped configs the actions reference (created/reconciled at apply time),
so a project's automation is self-contained:

```jsonc
"resources": [
  { "ref": "cs", "type": "classifier_set", "name": "${code}-cs", "body": { … } }
]
```

Supported types: **`classifier_set`**, **`notify_template`** (extensible via the handler
registry). Keyed by `(tenant, namespace, type, name)` and **ref-counted** — shared across
spaces, deleted only when the last space releases them. `type` must be in your
`prov_resources` scope.

### 3.5 Reference tokens

Anywhere a value is a string, these `${…}` tokens are resolved at apply time:

| Token | Resolves to |
|---|---|
| `${param}` | a param value (scalars inline; a `map`/`list` param injected as the whole value) |
| `${node:<addr>}` | the **freshly-minted uid** of a folder in this blueprint (never hard-code a uid) |
| `${resource:<ref>}` | the id of a created `resource` (by its `ref`) |

**Node addressing:** the root is addressed by its written `name`; descendants by their
name-path relative to the root (`Approved`, `Incoming/Rejected`); a node's explicit `ref`
is an additional address. So `${node:Approved}` in an action config resolves to that
folder's real uid in the created space.

### 3.6 Validate without applying

To check a blueprint (schema + `${…}` resolution + scope) and see the normalized plan
without creating anything, apply it with **`dry_run: true`** (§4). The response has
`status: "planned"` and the resolved `nodes`/`actions`, but no `provisioned_space` record
is written and nothing is created. Run it in your CI against a scratch tenant.

---

## 4. Applying a space

```
POST /v1/provisioning/spaces
Authorization: Bearer <integration token>
Content-Type: application/json

{
  "tenant":      "acme",
  "external_id": "project-42",          // idempotency key, unique per (tenant, integration)
  "version":     "3",                   // YOUR blueprint version — stamped on the root
  "mode":        "reconcile",           // create | reconcile (default) | enforce
  "dry_run":     false,
  "parent_uid":  null,                  // defaults to your scoped root (must be in prov_roots)
  "blueprint":   { … },                 // the inline document (§3)
  "params":      { … }                  // the per-space values
}
```

**Modes**

| mode | behavior |
|---|---|
| `create` | fail (`409 already_exists`) if the space already exists |
| `reconcile` *(default)* | create-missing, additive, **non-destructive** |
| `enforce` | also correct **drifted** ACLs / metadata / action config to match the blueprint (never destructive to user content) |

**Idempotency.** A repeat `POST` with the same `external_id` reconciles the *same* space —
never a duplicate. `dry_run: true` returns the plan without applying or recording.

**Preconditions.** The tenant's directory (LDAP) OU must exist (your external app
initializes it first) — else `409 tenant_not_initialized`; the core materializes the
tenant's storage lazily on first write. `tenant ∈ prov_tenants`; `parent_uid ∈ prov_roots`;
each `action.type ∈ prov_actions`; each `resource.type ∈ prov_resources`; the blueprint
validates.

**Response** (`ApplyResult`):

```jsonc
{
  "external_id": "project-42",
  "blueprint_name": "std-project",
  "version": "3",
  "status": "created",                  // created | reconciled | planned (dry_run)
  "space_uid": "…",
  "nodes":     [ { "path": "Approved", "uid": "…", "action": "applied" }, … ],
  "actions":   [ { "ref": "route", "folder_uid": "…", "binding_id": "…", "action": "applied" }, … ],
  "resources": [ { "ref": "cs", "type": "classifier_set", "name": "acme-cs", "id": "…", "action": "applied" } ],
  "warnings":  []
}
```

The root is stamped with `provision.*` metadata (name, version, integration id,
external id, applied-at) so drift and ownership are inspectable.

---

## 5. Managing spaces

| Endpoint | Purpose |
|---|---|
| `GET /v1/provisioning/spaces?tenant=&external_id=` | list within scope (by `external_id`) |
| `GET /v1/provisioning/spaces/{space_uid}` | inspect: node map, applied blueprint name+version, and **drift** vs the last-applied blueprint |
| `PATCH /v1/provisioning/spaces/{space_uid}` | re-apply / upgrade to a newer blueprint/version (`reconcile`|`enforce`) |
| `DELETE /v1/provisioning/spaces/{space_uid}` | **soft-delete** — removes the tree (recoverably) + `managed` bindings; the record is retained for audit/idempotency (requires `PROV_ALLOW_SPACE_DELETE`) |

---

## 6. Per-space setup API (§6.3)

A space's **automation config** is separately inspectable and adjustable per project — so
you can rotate a webhook's context map or change notify recipients **without re-authoring
the whole blueprint**.

```
GET   /v1/provisioning/spaces/{space_uid}/config?tenant=acme
→ { space_uid, external_id, version,
    params: { … secrets redacted as "***" … },
    actions: [ { ref, type, folder, folder_uid, binding_id, on_events, mime_types } ] }

PATCH /v1/provisioning/spaces/{space_uid}/config
      { "tenant": "acme", "params": { "notify_to": ["ops@acme"], "webhook_ctx": { … } } }
→ merges the automation params, re-renders the affected bindings (idempotent reconcile
  of the stored blueprint), and reports per action.
```

`PATCH` is **non-destructive and idempotent**. Patch only *automation* params (recipients,
context maps, callback URLs) — patching a **structural** param that drives a folder name
would relocate the space.

---

## 7. Tenant-scoped resources

Beyond spaces, you can manage the tenant-scoped configs directly:

```
POST   /v1/provisioning/resources
       { "tenant": "acme", "type": "classifier_set", "name": "acme-mfg", "body": { … } }
DELETE /v1/provisioning/resources/{type}/{name}?tenant=acme        (ref-count guarded)
```

Resources are namespaced by your `prov_namespace` and marked `managed_by` your
integration. A resource still referenced by a live space is not deleted unless forced.

---

## 8. Reconcile (drift sweep)

The `provisioning-service-reconcile` console script compares each **persisted** space
against the **live** core metadata (`managed_by` + `provision.*` stamps) and reports
drift — **read-only** (remediation is a separate, deliberate re-apply):

```bash
provisioning-service-reconcile acme beta        # tenants as args, or PROV_RECONCILE_TENANTS
```

It reports, per space: `missing_space` (root folder gone), `missing_node` (a provisioned
folder gone), `ownership_drift` (`managed_by` no longer yours), `version_drift` (the root
`provision.version` ≠ what was persisted). Exits non-zero when drift is found — wire it
into a periodic check.

---

## 9. Worked example

Provision a project, then rotate its notify recipients.

```bash
TOKEN='<your integration token>'
BASE='https://provision.example.com'

# 1) Apply
curl -sX POST "$BASE/v1/provisioning/spaces" -H "Authorization: Bearer $TOKEN" \
 -H 'Content-Type: application/json' -d '{
   "tenant": "acme", "external_id": "project-42", "version": "1", "mode": "reconcile",
   "params": { "code": "ACME", "lead": "eng", "notify_to": ["ops@acme"] },
   "blueprint": {
     "name": "std-project",
     "params": { "code":{"type":"string","required":true},
                 "lead":{"type":"principal","required":true},
                 "notify_to":{"type":"list"} },
     "root": { "name": "${code}",
               "acls": [{ "principal": "role:${lead}", "allow": ["r","w"] }],
               "children": [ { "name": "Incoming" }, { "name": "Approved" } ] },
     "resources": [ { "ref": "cs", "type": "classifier_set", "name": "${code}-cs", "body": {} } ],
     "actions": [
       { "ref": "route",  "folder": "Incoming", "type": "move_review",
         "config": { "on_approved": "${node:Approved}", "cs": "${resource:cs}" } },
       { "ref": "notify", "folder": "Incoming", "type": "notify",
         "config": { "to": "${notify_to}" } }
     ]
   }
 }'
# → { "space_uid": "SPACE", "status": "created", "nodes": [...], "actions": [...] }

# 2) Later — change notify recipients in place (no re-authoring)
curl -sX PATCH "$BASE/v1/provisioning/spaces/SPACE/config" -H "Authorization: Bearer $TOKEN" \
 -H 'Content-Type: application/json' -d '{ "tenant": "acme", "params": { "notify_to": ["ops@acme","lead@acme"] } }'
```

---

## 10. Deployment & configuration

Run the service (installed console script or module):

```bash
provisioning-service                          # serves PROV_HTTP_HOST:PROV_HTTP_PORT (default 127.0.0.1:8100)
# or: python -c 'from provisioning_service.app import main; main()'
```

It keeps its own Postgres (per-tenant schema, created on first use), publishes audit
events to the FileEngine Redis audit stream, drives the core over gRPC and folder_actions
over REST.

**Key environment** (service-private `PROV_*`; shared platform `FILEENGINE_*`):

| Var | Purpose |
|---|---|
| `PROV_HTTP_HOST` / `PROV_HTTP_PORT` | listener (default `127.0.0.1:8100`; front with a reverse proxy) |
| `PROV_CORS_ORIGINS` | browser origins allowed to call it (comma-separated, exact) |
| `PROV_PG_*` | its Postgres (host/port/user/password/database) |
| `PROV_BRIDGE_URL` / `PROV_BRIDGE_INTROSPECT_TTL` | bridge base for token verification/introspection |
| `PROV_FOLDER_ACTIONS_URL` / `PROV_FOLDER_ACTIONS_TIMEOUT_S` | folder_actions REST endpoint |
| `PROV_MAX_TREE_NODES` / `PROV_MAX_TREE_DEPTH` | blueprint tree limits |
| `PROV_APPLY_RATE_PER_MIN` | apply rate limit |
| `PROV_ALLOW_SPACE_DELETE` | enable `DELETE /spaces/{uid}` |
| `PROV_ENFORCE_TENANT_LDAP` | require the tenant OU to exist before apply |
| `PROV_ENFORCE_AIP` / `PROV_TRUSTED_PROXIES` / `PROV_IP_ALLOWLIST` | source-IP enforcement |
| `PROV_RECONCILE_TENANTS` | default tenants for the reconcile sweep |
| `PROV_MONITORING_ALLOW_IPS` | client-IP allow-list for `/healthz` `/readyz` `/poolz` |
| `FILEENGINE_JWT_SECRET` | shared secret to verify bridge tokens (**must match the bridge's**) |
| `FILEENGINE_GRPC_HOST` / `_PORT` | the core gRPC backend |
| `FILEENGINE_REDIS_*`, `FILEENGINE_AUDIT_STREAM` | audit stream |
| `FILEENGINE_LDAP_ENDPOINT` / `_TENANT_BASE` | tenant validation |

Monitoring endpoints (`/healthz`, `/readyz`, `/poolz`) bind for a loopback/allow-listed
reporter only.

---

## 11. Errors

OAuth/HTTP-style bodies `{ "code": "…", … }`:

| Status | `code` | Meaning |
|---|---|---|
| 401 | (bearer) | missing/invalid bridge token |
| 403 | `not_integration` | token lacks `amr:["integration"]` + `capabilities:["provisioning"]` |
| 403 | `tenant_not_allowed` | `tenant ∉ prov_tenants` |
| 403 | `root_not_allowed` | `parent_uid ∉ prov_roots` |
| 403 | `action_not_allowed` | an `action.type ∉ prov_actions` |
| 403 | `resource_not_allowed` | a `resource.type ∉ prov_resources` |
| 403 | `ip_not_allowed` | source IP outside the allow-list / `aip` |
| 409 | `tenant_not_initialized` | the tenant's directory OU doesn't exist yet |
| 409 | `already_exists` | `mode:create` and the space exists |
| 409 | `space_deleted` / `no_blueprint` | config PATCH on a deleted / blueprint-less space |
| 422 | `invalid_blueprint` | schema / `${…}` resolution / validation failure (`errors: [...]`) |

---

## 12. Security model

- **Capability tokens.** The bridge token *is* your grant — `prov_*` claims bound its
  reach. Short-lived; re-mint as needed. Compromise of the provisioning service cannot
  widen your scope (the claims are signed by the bridge).
- **Least privilege.** Scope `prov_tenants`, `prov_actions`, `prov_resources`,
  `prov_roots`, `prov_principals` to exactly what your integration needs.
- **Non-destructive by default.** `reconcile` never removes user content; `enforce`
  corrects only *your* provisioned ACLs/metadata/bindings; `DELETE` soft-deletes and is
  gated by config.
- **`managed_by` transparency.** Every provisioned folder/binding/resource is marked, so
  tenant admins see (and are warned about) what your integration controls.
- **Source-IP defense-in-depth.** `PROV_ENFORCE_AIP` re-checks the token's `aip` against
  the real client IP (trusted-proxy aware).
- **Isolated state.** Per-tenant schema; your resources are namespaced by
  `prov_namespace`.

---

## 13. Endpoint & claims reference

**Endpoints** (all under `/v1/provisioning`, bearer-authenticated unless noted):

```
POST   /spaces                         apply an inline blueprint (create/reconcile/enforce/dry_run)
GET    /spaces?tenant=&external_id=     list within scope
GET    /spaces/{uid}                    inspect (node map, version, drift)
PATCH  /spaces/{uid}                    re-apply / upgrade
DELETE /spaces/{uid}                    soft-delete
GET    /spaces/{uid}/config             resolved automation config (secrets redacted)
PATCH  /spaces/{uid}/config             update automation params in place
POST   /resources                       apply a tenant-scoped resource
DELETE /resources/{type}/{name}         delete a resource (ref-count guarded)
GET    /whoami                          resolve the calling integration + scope
GET    /healthz  /readyz  /poolz        monitoring (loopback/allow-listed)
```

**Token claims the service reads:** `amr` (must contain `integration`), `capabilities`
(must contain `provisioning`), `prov_tenants`, `prov_namespace`, `prov_roots`,
`prov_principals`, `prov_actions`, `prov_resources`, `aip`.

**Blueprint param types:** `string`, `int`, `bool`, `url`, `enum`, `map`, `list`,
`principal`, `ref`, `secret`.

**Reference tokens:** `${param}`, `${node:<addr>}`, `${resource:<ref>}`.

---

*See `SPECIFICATIONS.md` for the full design rationale, and the embedding kit's
Integrator's Guide for the browser-facing components that consume these spaces.*
