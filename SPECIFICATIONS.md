# FileEngine Provisioning Service — Specification

A standalone **AGPL** FastAPI service lane that implements the **embedding
provisioning API surface** (§14.7 of the commercial embedding kit): a
server-to-server API for an embedding application to **stand up and maintain the
project spaces it needs** — standardized "project"/space folder trees with owners,
roles, ACLs, metadata, **and per-space automation** (folder_actions bindings:
webhooks, notify, sorter, review chains — each customized per project) already
applied — so end users then simply operate within correctly-permissioned,
correctly-wired spaces. It is a **rich setup API**, not a static folder clone.

Follows the established FileEngine FastAPI service-lane conventions (as
`folder_actions`, `discussion`, `convert_search_ai`, `bcf_service`): `src/` layout,
shared `FILEENGINE_*` config + service-private `PROV_*` config, HS256 bridge-JWT
auth with introspection fallback, gRPC core access via `../python_interface`, its
own per-tenant Postgres, Redis audit emission, loopback monitoring endpoints, and a
`pyproject` console-script entrypoint.

- **License:** AGPL-3.0-or-later (matches the platform service lanes).
- **Port:** `8100` (next free after folder_actions :8099).
- **Package:** `provisioning_service` (dir `commercial_provisioning/`).

---

## 1. Role in the stack

```
Integrator backend  ──(integration-service token, §14.2)──▶  Provisioning Service (:8100)
  "provision project X"                                         │
                                                                ├─ own Postgres: templates + provisioned space/node/binding records (idempotency)
                                                                ├─ gRPC to core as the integration principal (mkdir / grant / metadata)
                                                                ├─ REST to folder_actions (:8099): per-space automation bindings (§7.1)
                                                                └─ Redis: emit provisioning.* audit events
Official SPA (admin) ──(admin JWT)──▶ template CRUD (System config → Provisioning)
```

- **Callers.** The integrator's *backend* (or a trusted orchestrator), never the
  browser or the embed kit. Requests carry an **integration-service token** (§14.2:
  `sub = integration_id`, `amr:["integration"]`, `provisioning` capability, scope
  claims). Template authoring is admin, carrying a normal admin bridge JWT.
- **Depends on** (upstream, embedding-kit §14): the integration **registry** +
  credential UI (§14.1, ldap_manager) and the **exchange endpoint** (§14.2,
  http_bridge) that mints the integration-service token. This service **consumes**
  those tokens; it does not mint them.
- **Writes go through the core** (gRPC via `python_interface`), so all existing
  ACL, versioning, tenancy, and audit invariants hold unchanged. This service adds
  orchestration + a template/idempotency store, not a new write path.

---

## 2. Scope boundary (what this service does / does not do)

**Does:** template storage + validation; declarative space **apply/reconcile**;
idempotent project provisioning keyed by the integrator's `external_id`; drift
inspection; scope enforcement; `provisioning.*` audit.

**Does not:** manage identity or tenant lifecycle. Creating roles/groups and
managing membership stays in the shared LDAP source-of-truth (the integrator writes
to the common directory, or uses ldap_manager admin) — templates *bind to* existing
roles/claims, they don't create them. **Tenants** are likewise defined by their LDAP
OU structure (§3.2), initialized by the embedding application *before* FileEngine is
touched; provisioning only validates + adopts them. No end-user surface (the embed
kit never calls this). No ACL-editing UI. No token minting (that's §14.2).

---

## 3. Authentication & acting identity

Mirrors the lane's auth stack (`jwt_verify.py` + `bridge_auth.py` + `http_auth.py`):

- **Local HS256 verify** of the bearer against the shared `FILEENGINE_JWT_SECRET`
  (alg-pinned, `exp` enforced, roles map scoped by `X-Tenant`), with a **bridge
  introspection fallback** (`PROV_BRIDGE_URL` → `GET /v1/auth/introspect`) when the
  shared secret is absent — identical to how folder_actions/csai/discussion accept
  the bridge token.
- **Two token types:**
  - **Integration-service token** (space ops) — must carry `amr:["integration"]`,
    the `provisioning` capability, and the **scope claims** (§3.1). Enforced by
    `require_integration(scope=...)`.
  - **Admin bridge JWT** (template CRUD) — normal tenant-admin, enforced by
    `require_admin`.
- **Acting on the core — as the integration principal.** For each apply, the
  service constructs a `CoreClient` bound to the **integration's service identity**
  (`user = integration_id`, a `provisioning` role, `tenant = target`) via
  `python_interface`. The core then enforces ACLs against the grants the
  integration holds on its **scoped root** (established once at registration,
  §14.7.4). This is least-privilege: the service is not a god-mode principal; a
  scope bug still cannot write outside the integration's granted subtree.
  - *Alternative (documented, not default):* a single dedicated provisioning
    service principal with tenant-wide rights, with scope enforced only in-app.
    Rejected as default because it concentrates privilege and loses core-side
    enforcement.

### 3.1 Scope claims (carried by the integration-service token)
Minted by the exchange endpoint from the registry (§14.1) so this service enforces
without reading the registry DB:
- `prov_tenants: [..]` — allowed tenant(s).
- `prov_roots: [uid|path, ..]` — root prefix(es) under which spaces may be created.
- `prov_principals: [pattern, ..]` — role/claim patterns it may grant (e.g.
  `role:project:*`), never `system_admin`.
- `prov_actions: [type, ..] | "*"` — folder_actions binding types it may configure
  (`webhook · notify · sorter · move_review · raise_review`), and whether it may set
  `secret`s. Absent ⇒ no automation setup permitted.
Every request is checked against these ceilings **before** any core call; the core
ACL check is the second, authoritative gate.

### 3.2 Tenant model — implicit & LDAP-driven (lazy bootstrap)

The service has **no tenant-registration step**. A tenant is defined by its **OU
structure in the shared LDAP directory** (`FILEENGINE_LDAP_TENANT_BASE`, e.g.
`ou=<tenant>,ou=tenants,…`) — the source-of-truth (Posture B, embedding-kit §5.0).

- **Prerequisite ordering (integrator's responsibility).** The embedding
  application **MUST initialize the tenant's LDAP OU structure** (its users/roles
  OUs per the shared schema) **before** touching FileEngine. FileEngine is
  downstream of the directory; provisioning never creates the tenant *identity*.
- **Validate → adopt → apply (per request).** For the request's tenant `T` (from
  the token's tenant / `X-Tenant`, and it must be within `prov_tenants`):
  1. **Validate** `T` against LDAP (`ldap_auth`): a well-formed tenant OU must exist
     under the tenant base. Absent/malformed ⇒ reject **`409 tenant_not_initialized`**
     (audited) — the integrator must init the LDAP OU first.
  2. **Boot the tenant metadata:** load `T`'s config from the directory into the
     service's tenant metadata (cached), and idempotently `ensure_tenant_schema` for
     the provisioning DB.
  3. **Core materializes `T` lazily** on the first write — `TenantManager.get_tenant_context`
     creates the tenant's schema + storage on first access, so acting as the
     integration principal against `T`'s root **auto-provisions** the core-side
     tenant. No explicit `CreateTenant` call is needed.
  4. Proceed with the apply (§7).
- **No garbage tenants.** Step 1 gates on LDAP validity, so the service never
  materializes a tenant the directory doesn't define: **the LDAP OU is the gate; the
  core lazy-create is the mechanism.**
- **Upstream note:** this relies only on the core's existing lazy tenant
  materialization — no new tenant RPC required. If strict pre-materialization
  (schema/storage without a first write) is ever wanted, a small admin "ensure
  tenant" op could be added later; not needed for v1.

---

## 4. Data model (own Postgres, per-tenant schemas)

Owns its own database (`PROV_PG_*`, default DB `provisioning`), per-tenant schemas
`tenant_<slug>` (as folder_actions), idempotent DDL via `ensure_tenant_schema`.

- **`provisioned_space`** — the idempotency + provenance record. **No template
  library is stored** (§5.0): the integrator passes the blueprint inline each call.
  `external_id` (integrator's key, **unique per (tenant, integration_id)**),
  `integration_id`, `tenant`, `space_uid` (core root uid), `blueprint_name`
  (integrator's logical id), `version` (integrator-supplied — also stamped on the
  root metadata, §5.6), `last_blueprint` JSONB (snapshot of the last applied
  document, for reconcile/drift), `params` JSONB (secrets redacted), `status`,
  `created_at`, `last_applied_at`.
- **`provisioned_node`** — template-path → core `uid` map for a space (so reconcile
  is stable across runs, drift can be computed, and `${node:<path>}` references
  resolve): `space_id`, `path`, `uid`, `kind`, `created_at`.
- **`provisioned_binding`** — the space's automation map: `space_id`, `ref`,
  `folder_uid`, `fa_binding_id` (folder_actions id), `type`, `config_hash`,
  `secret_refs`, `created/updated_at`. Backs idempotent reconcile of actions and
  drift detection; holds **no raw secrets** (§5.4).
- **`apply_run`** — per-apply log (audit companion): `space_id`, `mode`, `dry_run`,
  `outcome`, `report` JSONB (per-node actions/warnings), `actor` (integration_id),
  `ts`.

Replica/failover envs mirror the lane (`PROV_PG_REPLICA_*`), read-mostly ops can use
the replica.

---

## 5. Space blueprint (inline JSON document) — structure **and** setup

### 5.0 No stored templates — the blueprint is passed inline
FileEngine does **not** store a provisioning template library. The integrator's
backend passes a **rich JSON blueprint document** to the integration endpoint
(§6.1) on each apply, describing the desired space. This keeps the source-of-truth
for "what a project space looks like" in the integrator's own system (versioned in
their repo/config), not duplicated in FileEngine. The provisioning service snapshots
the last-applied blueprint per space (`last_blueprint`) only for reconcile/drift.

A blueprint is **not** a static folder clone. It declares the full initial setup of
a space — folder tree, ACLs, metadata, **and per-space automation** (folder_actions
bindings: webhooks, notify, sorter, review chains) — driven by a **typed parameter
schema** so each space/project is customized at apply time. Real setups differ per
project: a webhook's **context data map** and callback URL, a **notify** action's
recipients/template/subject, a sorter's classifier set, review reviewers — all are
parameters supplied per space.

The document carries top-level `name` (the integrator's logical blueprint id) and
`version` (§5.6), plus `params` (schema), `root` (structure), and `actions`
(automation).

### 5.1 Parameter schema (`params`) — typed, validated per-space inputs
Each declared param has a `type`, `required`, optional `default`/`description`, and
constraints; apply/patch calls must satisfy it.
- Types: `string · int · bool · url · enum · map · list · principal · ref · secret`.
  - **`map`** carries structured per-space data — e.g. a **webhook context data
    map** or a notify field set — injected verbatim.
  - **`secret`** is **write-only pass-through**: forwarded to folder_actions'
    encrypted store, never returned or persisted raw by provisioning (§5.4).
  - **`principal`** must match the token's `prov_principals`; **`ref`** points at an
    existing object (e.g. a `classifier_set_id`).
- Referenced by `${param}` (scalars) or whole-value injection for `map`/`list`.
- **`${node:<template-path>}`** — a **symbolic folder reference** resolved at apply
  time to *this space's* freshly-minted folder UUID (§5.3). Any folder reference in
  an action's config (sorter route destinations, `move_review` on_approved/on_rejected,
  webhook `move_to`) MUST use this token — **never a literal UUID**, which would point
  at another space or nothing.

### 5.2 Structure (root / children / acls / metadata)
```jsonc
"root": {
  "name": "${project_code}",
  "metadata": { "type": "project", "code": "${project_code}" },
  "acls": [
    { "principal": "role:${manager_role}", "allow": ["r","w","d","m"] },
    { "principal": "role:${member_role}",  "allow": ["r","w"] },
    { "principal": "everyone",             "deny":  ["r"] }
  ],
  "children": [
    { "name": "Documents" },
    { "name": "Drawings", "children": [ { "name": "Superseded" } ] },
    { "name": "Incoming", "acls": [ { "principal": "role:${member_role}", "allow": ["r","w"] } ] },
    { "name": "Approved", "acls": [ { "principal": "role:${member_role}", "allow": ["r"] } ] }
  ]
}
```
- **Inheritance:** a child with no `acls` inherits the parent's (mirrors the core's
  `ACL_INHERIT`); a child may add/override. Permission keys reuse the platform ACL
  letter vocabulary (`r w d l u v b s m i …`).

### 5.3 Automation (`actions`) — per-space folder_actions bindings
Each entry creates a folder_actions binding on a folder in the tree (by template
`path`/`ref`), with **parameterized** `config`. Types mirror folder_actions
(`webhook · notify · sorter · move_review · raise_review`); binding-level
`on_events` + `mime_types` apply. Every field is `${param}`-substitutable, so the
same template yields per-project-distinct automation.

```jsonc
"actions": [
  {
    "ref": "callback-webhook",
    "folder": "Incoming",
    "type": "webhook",
    "on_events": ["file.created","conversion.complete"],
    "mime_types": ["application/pdf"],
    "config": {
      "url": "${callback_url}",              // per-project endpoint
      "secret": "${callback_secret}",        // secret param -> folder_actions encrypts at rest
      "context": "${webhook_context}"        // per-project CONTEXT DATA MAP injected verbatim
    }
  },
  {
    "ref": "approvals-notify",
    "folder": "Approved",
    "type": "notify",
    "on_events": ["review.approved"],
    "config": {
      "recipients": "${notify_recipients}",  // per-project recipients (users/roles)
      "template": "${notify_template}",       // per-project template ref
      "fields": "${notify_context}"           // per-project notify context/merge data
    }
  },
  {
    "ref": "route-approved",
    "folder": "Incoming",
    "type": "move_review",
    "on_events": ["review.approved","review.rejected"],
    "config": {
      "on_approved": "${node:Approved}",      // resolved to THIS space's Approved uid
      "on_rejected": "${node:Incoming/Rejected}"
    }
  }
]
```
- The **webhook context data map** and the **notify** specifics (recipients,
  template, merge fields) are per-space `map`/`list`/`ref` params — each project's
  automation carries its own routing/correlation/recipient data.
- **Folder references are symbolic, not cloned.** Because each new space has **fresh
  folder UUIDs**, every destination in an action config (move/sorter destinations,
  webhook `move_to`) uses `${node:<path>}` and is resolved to the space's real UUID
  at apply time (§7.1). A blueprint can therefore *never* be a verbatim clone of an
  existing binding export — the UUIDs are space-specific and must be re-bound. This
  is the core reason provisioning **orchestrates** folder_actions rather than copying.
- Actions are created via folder_actions' API (§7.1); provisioning owns the mapping
  `ref → (folder_uid, folder_actions binding id)` for idempotent reconcile.

### 5.4 Secrets handling
`secret` params (webhook secrets, tokens embedded in context) are **never persisted
raw** by provisioning and **never returned** by read endpoints (redacted). They are
forwarded once to folder_actions, which encrypts them at rest (its `secrets.py`).
Provisioning stores only a reference + a hash for drift detection. Rotation goes
through the setup API (§6.4), not by reading the old value.

### 5.5 Validation (`templates.py` / `blueprint.py`)
Referenced params exist and are typed; `principal` params ⊆ `prov_principals`;
action `type` ∈ the token's `prov_actions`; **every `${node:<path>}` reference
resolves to a folder declared in the tree** (else the blueprint is rejected — no
dangling destinations); no cyclic/oversized trees (bounded depth + node count);
permission letters and event names known; `config` shapes validated against each
action type's schema (delegated to folder_actions' `/action-types` contract).

### 5.6 Versioning & trackability (stamped on the space root metadata)
Rather than a stored template version, the integrator passes a **`version`** (their
own version of the provisioning structure) on each apply. Provisioning **stamps it
onto the space root folder's metadata** in the core, so the version travels with the
space and any client can inspect it:
- Root metadata keys written on apply: `provision.name`, `provision.version`,
  `provision.integration_id`, `provision.external_id`, `provision.applied_at`.
- **The embedder inspects** the root's metadata (`GET /v1/nodes/{root}/metadata`) to
  learn which generation of the provisioning structure a space is on, and decides
  whether to trigger an upgrade (the integrator re-applies a newer blueprint + higher
  `version`; §7 reconcile/enforce converges it and re-stamps the metadata).
- The provisioning DB also records `version`/`blueprint_name` on `provisioned_space`
  for its own listing/idempotency, but **the core metadata on the root is the
  authoritative, inspectable version marker** — it needs no call to this service.
- Version is opaque to provisioning (string; integrators may use semver or an int);
  it is not interpreted, only stamped and echoed.
- **Upgrade is the embedding application's responsibility, not the service's.** The
  integrator inspects existing spaces' root-metadata version, and where it lags the
  app's current structure, applies the updated blueprint (`reconcile`/`enforce`) to
  migrate the folder/automation schema and **bump the stamped version**. Provisioning
  never proactively upgrades or scans for out-of-date spaces — it applies what it is
  told and records the result. (The optional `reconcile.py` sweep is for *internal
  drift* — e.g. a binding manually changed — not version migration.)

---

## 6. API surface

Base path `/v1/provisioning`. All routes require auth (§3). Space routes require an
**integration-service token**; template routes require **admin**.

### 6.1 Spaces (integration-driven)
- `POST /v1/provisioning/spaces` — the **integration endpoint**: apply an **inline
  blueprint** (create/reconcile a space: folders + ACLs + metadata **+ automation**).
  Body: `{ tenant, external_id, version, blueprint:{...}, params:{...}, parent_uid?,
  mode?: "create"|"reconcile"|"enforce", dry_run?: bool }`.
  - **`blueprint`** is the full JSON document (§5) — passed inline, not stored/
    referenced by id. **`version`** is the integrator's version, stamped onto the
    root metadata (§5.6). **`params`** supplies the per-space customization declared
    by the blueprint schema (scalars, webhook **context data map**, notify
    recipients/template/fields, classifier refs, `secret`s — write-only, §5.4).
  - **Idempotent by `external_id`** (unique per tenant+integration): a repeat call
    reconciles the same space, never a duplicate. `parent_uid` defaults to the
    integration's scoped root (must be within `prov_roots`).
  - **`mode`:** `create` (fail if exists) · `reconcile` (default; create-missing,
    additive, non-destructive) · `enforce` (also correct drifted ACLs/metadata **and
    action config** to match the blueprint; never destructive to user content).
  - **`dry_run:true`** → return the **plan** (folders + actions) without applying.
  - **Preconditions:** tenant valid in LDAP (§3.2) else `409 tenant_not_initialized`;
    `tenant ∈ prov_tenants`; `parent_uid ∈ prov_roots`; each action `type ∈ prov_actions`;
    blueprint validates (§5.5).
  - **Stamps** `provision.*` version metadata on the created/updated root (§5.6).
  - Response: `{ space_uid, external_id, blueprint_name, version, status, nodes:[{path,
    uid, action}], actions:[{ref, folder_uid, binding_id, action}], warnings:[...] }`.
- `GET /v1/provisioning/spaces?tenant=&external_id=&blueprint_name=` — list within scope.
- `GET /v1/provisioning/spaces/{space_uid}` — inspect: node map, applied
  template+version, and **drift** vs the current template.
- `PATCH /v1/provisioning/spaces/{space_uid}` — re-apply / upgrade to a newer
  template version (reconcile|enforce), or update params.
- `POST /v1/provisioning/spaces/{space_uid}/grants` — bounded ACL adjustment
  (add/remove a permitted role/claim grant within `prov_principals`), for
  membership-shaped changes not warranting a full re-template.
- `DELETE /v1/provisioning/spaces/{space_uid}` — **soft-delete** (scope-checked;
  honors the core's recoverable-delete + versioning); optional, off by default
  (`PROV_ALLOW_SPACE_DELETE`).

### 6.2 Blueprint validation (no stored template library)
There is **no** template CRUD / stored template store / SPA template editor (§5.0):
blueprints live in the integrator's own system and are passed inline (§6.1). One
authoring aid only:
- `POST /v1/provisioning/blueprints/validate` — validate a blueprint document
  (schema + `${node}` resolution + scope checks, §5.5) and return the normalized
  plan, without a tenant/space. Equivalent to `POST /spaces` with `dry_run` but
  without creating a `provisioned_space` record. Handy in the integrator's CI.

### 6.3 Per-space configuration & setup API (rich, post-provision customization)
Beyond one-shot apply, a space's **automation config** is separately inspectable and
adjustable per project — so an integrator can set/rotate a webhook's context map, a
notify action's recipients, etc., without re-templating. All bounded by the token's
`prov_actions`/`prov_principals` and the space's blueprint (only its declared action
set may be tuned; adding arbitrary action types is not allowed unless the blueprint
declares them optional).
- `GET /v1/provisioning/spaces/{space_uid}/config` — the space's resolved
  automation config: each action `ref`, its folder, bound `binding_id`, `type`,
  `on_events`/`mime_types`, and current param **values with `secret`s redacted**.
- `PATCH /v1/provisioning/spaces/{space_uid}/config` — update per-space param values
  (e.g. `{ params: { webhook_context: {...}, notify_recipients: [...] } }`);
  re-renders the affected bindings and pushes them to folder_actions. Non-destructive,
  idempotent, reported per action.
- `POST /v1/provisioning/spaces/{space_uid}/actions` — add a binding **from the
  blueprint's declared (optional) action set** with per-space config.
- `PATCH|DELETE /v1/provisioning/spaces/{space_uid}/actions/{ref}` — update / remove
  a space's binding (scope-checked).
- `POST /v1/provisioning/spaces/{space_uid}/secrets/{ref}` — set/rotate a `secret`
  param for an action; forwarded to folder_actions' encrypted store, never persisted
  raw here (§5.4).

### 6.4 Monitoring (loopback-only, unauthenticated)
`/healthz`, `/readyz`, `/poolz` bound to loopback / `PROV_MONITORING_ALLOW_IPS`,
per the platform monitoring convention. Not on the public surface.

---

## 7. Apply engine (`engine.py`) — semantics

- **Resolve tenant first (§3.2).** Validate the request tenant against LDAP and boot
  its metadata (reject `tenant_not_initialized` if the OU is absent); the core
  materializes the tenant lazily on the first write. Only then plan/apply.
- **Plan → apply.** Parse the inline `blueprint`, substitute `params`, and compute a
  **plan**: the desired node tree + ACL/metadata + actions. Diff against
  `provisioned_node`/`provisioned_binding` (existing maps) + live state to classify
  each item create/existing/updated. Stamp the `provision.*` version metadata (§5.6).
- **Idempotent + reconcilable.** First apply creates; later applies converge to the
  (possibly newer-`version`) blueprint the caller passes. Safe to call unconditionally
  on every "new project" event and safe to retry.
- **Never destructive by default.** `reconcile`/`enforce` only add folders and
  adjust ACLs/metadata; they never delete user folders/content. Removal is the
  explicit, separately-scoped `DELETE`.
- **Resumable, reported.** A multi-node apply persists the node map as it goes and
  returns a per-node report; a mid-apply failure leaves a consistent state that a
  re-apply completes. `dry_run` returns the plan without writing.
- **Drift** (inspect) surfaces divergence from the template so an operator or the
  integration can choose to `enforce`.
- **Core operations used** (via `core_client.py` → `python_interface`): `make_dir`,
  `grant_permission`/`revoke_permission`, `set_metadata` — each ACL-checked by the
  core for the acting integration principal.

### 7.1 folder_actions orchestration + node-reference resolution
Automation (`actions`) is applied **after** the folder tree, in two steps:
1. **Resolve `${node:<path>}` → real UUID.** Once the tree exists and the
   `path → uid` map is persisted (`provisioned_node`), every folder reference in each
   action's `config` — move/sorter destinations, webhook `move_to`, the binding's own
   target folder — is resolved to *this space's* freshly-minted UUID. This is why a
   blueprint can never clone a binding verbatim (§5.3): the UUIDs are space-specific.
2. **Create/reconcile the binding via folder_actions' API** (`PROV_FOLDER_ACTIONS_URL`,
   :8099) as the acting integration/admin token: `POST /actions` (create) or
   `PUT /actions/{id}` (update) with the resolved, param-substituted config;
   `PUT /actions/{id}/routes` for sorter routes (destinations resolved likewise).
   Provisioning records `ref → (folder_uid, binding_id, config_hash)` in
   `provisioned_binding` so reconcile is idempotent and drift is detectable.
- **Reconcile/enforce:** re-resolve refs against the stable node map and diff each
  binding's rendered config vs `config_hash`; `enforce` pushes corrections. Bindings
  are created disabled→enabled last, so a half-applied space never fires actions.
- **Secrets** in a binding config are handed to folder_actions (encrypted at rest,
  its `secrets.py`); provisioning keeps only a reference + hash (§5.4).
- **Cross-service failure** is reported per-action in the response; a folder_actions
  outage leaves folders created and actions pending — a re-apply completes them
  (idempotent). Folder writes and binding creation are **not** one transaction, so the
  engine is designed to converge on retry rather than roll back.
- **Boundary note:** configuring folder_actions here is the *programmatic
  config-service* path (admin/server-to-server), the counterpart of the official
  client's *interactive* folder_actions admin UI — both are admin surfaces; neither is
  the end-user embed kit.

---

## 8. Audit (`audit.py` → Redis audit stream)

Emit to the platform audit stream (drained by `audit_service`) — tamper-evident,
attributable. Events, each `{ integration_id, tenant, blueprint_name, version,
space_uid, external_id, mode, outcome, source_ip }`:
- `provisioning.space_applied`, `provisioning.space_reconciled`,
  `provisioning.space_deleted`, `provisioning.action_configured` (binding
  create/update, secret rotate — secrets redacted), and (admin)
  `provisioning.template_changed`.
Rejections (scope/authz failures) are emitted too, as security signals.

---

## 9. Configuration (env)

Shared platform keys (as the other lanes): `FILEENGINE_GRPC_HOST/PORT`,
`FILEENGINE_REDIS_HOST/PORT/PASSWORD/DB`, `FILEENGINE_JWT_SECRET`,
`FILEENGINE_LDAP_*`, `FILEENGINE_AUDIT_STREAM`.

Service-private `PROV_*`:
- HTTP: `PROV_HTTP_HOST` (default `127.0.0.1`), `PROV_HTTP_PORT` (default `8100`),
  `PROV_CORS_ORIGINS` (normally empty — this is a server-to-server API, not browser
  CORS-exposed).
- DB: `PROV_PG_HOST/PORT/USER/PASSWORD/DATABASE` (+ `PROV_PG_REPLICA_*`),
  `PROV_DB_STATEMENT_TIMEOUT_MS`.
- Auth: `PROV_BRIDGE_URL` (+ `PROV_BRIDGE_INTROSPECT_TTL`) for the introspection
  fallback; `PROV_PROVISIONING_ROLE` (the role name used when acting as an
  integration principal on the core).
- Orchestration: `PROV_FOLDER_ACTIONS_URL` (:8099) + `PROV_FOLDER_ACTIONS_TIMEOUT_S`
  for applying per-space automation bindings (§7.1).
- Limits: `PROV_MAX_TREE_NODES`, `PROV_MAX_TREE_DEPTH`, `PROV_APPLY_RATE_PER_MIN`
  (per `integration_id`), `PROV_ALLOW_SPACE_DELETE` (default false).
- Monitoring: `PROV_MONITORING_ALLOW_IPS`.

A `.env.example` ships the full surface (copy to `.env`; never commit `.env`).

---

## 10. Repository layout (planned `src/` — mirrors the lane)

```
commercial_provisioning/
├── pyproject.toml                # name "provisioning-service"; AGPL; console script
├── .env.example
├── README.md
├── SPECIFICATIONS.md             # this file
└── src/provisioning_service/
    ├── __init__.py
    ├── config.py                 # Config from env (FILEENGINE_* + PROV_*)
    ├── app.py                    # FastAPI factory + CORS + monitoring + main()
    ├── deps.py                   # require_integration(scope) / require_admin
    ├── api.py                    # space + template routers (§6)
    ├── jwt_verify.py             # local HS256 verify (shared secret)
    ├── bridge_auth.py            # introspection fallback
    ├── http_auth.py             # bearer extraction + token-type checks
    ├── ldap_auth.py              # identity/roles helpers
    ├── _client.py                # python_interface bootstrap (as folder_actions)
    ├── core_client.py            # CoreClient acting as the integration principal
    ├── schema.py                 # per-tenant DDL (§4) + ensure_tenant_schema
    ├── db.py / stores.py         # Postgres access (templates, spaces, node map, runs)
    ├── templates.py              # template parse/validate + param substitution
    ├── engine.py                 # plan → apply/reconcile/enforce + drift
    ├── audit.py                  # provisioning.* Redis emit
    ├── reconcile.py              # optional periodic drift sweep (console script)
    └── netutil.py / failover.py  # request IP + replica helpers
```

Console scripts (`pyproject`): `provisioning-service = provisioning_service.app:main`
(API :8100) and `provisioning-service-reconcile = provisioning_service.reconcile:main`
(optional drift sweep).

---

## 11. Deployment

- **Dev launcher:** add to `scripts/start_backend_services.sh` as a new numbered
  step (API :8100, loopback monitoring), mirroring folder_actions (run from the
  service dir so it loads `./.env`, `PYTHONPATH=src`, prefer a `.venv` then system
  python; stop entry first since it starts late). Pidfile `/tmp/provisioning_service.pid`.
- **Unified stack:** a `provisioning` container in `docker_unified` (behind nginx if
  it ever needs external reachability; by default internal, called by the
  integrator's backend server-to-server). Reachable by other services on the compose
  network. Shares `FILEENGINE_JWT_SECRET` + Redis + Postgres.
- **Deploy (prod):** Ansible podman-container role in the scripts repo, wired into
  `app_plane.yml` (per the centralized-deploy convention).

---

## 12. Testing

- **Unit:** template validation + param substitution; plan/diff engine
  (create/reconcile/enforce, drift); scope enforcement (deny out-of-scope
  root/template/principal); idempotency (repeat `external_id` → same space).
- **Auth:** integration-service vs admin token gating; introspection fallback.
- **Integration:** against a running dev stack (`scripts/start_backend_services.sh`)
  — apply a template, assert the folder tree + ACLs + metadata in the core, re-apply
  (noop), upgrade a version (enforce), dry-run (no writes), audit events emitted.
- **Contract:** pin the core ops + audit event shapes; smoke test flags drift from
  the FileEngine gRPC API.

---

## 13. Milestones

1. **P0 — Skeleton.** `pyproject`, config, app factory, auth stack (verify
   integration/admin tokens), `python_interface` core client, per-tenant schema,
   `/healthz`. Boots against the dev stack.
2. **P1 — Blueprint validation.** inline blueprint parse/validate (§5.5) +
   `/blueprints/validate`; version stamping on the root metadata (§5.6). No stored
   template library.
3. **P2 — Apply engine (structure).** `POST /spaces` create + `reconcile`,
   idempotency by `external_id`, node map, core mkdir/grant/metadata as the
   integration principal, `provisioning.*` audit. First usable release (folders +
   ACL + metadata).
4. **P3 — Automation & setup (the rich part).** `actions` in the blueprint;
   `${node:...}` resolution; folder_actions orchestration (§7.1) incl. sorter routes
   + secrets; the per-space configuration/setup API (§6.3); `provisioned_binding`
   store + reconcile. This is what makes provisioning more than a folder clone.
5. **P4 — Enforce + drift + dry-run + grants + soft-delete** (over structure *and*
   actions).
6. **P5 — Hardening.** rate-limits, reconcile sweep, docs/examples (blueprint
   authoring + onboarding), unified-stack + Ansible packaging.

Aligns with the embedding kit's **M-U (V1)**: this service is the concrete home of
the §14.7 provisioning surface.

---

## 14. Open decisions

1. **Acting identity** (§3): per-integration principal (default, least-privilege) vs
   a single provisioning service principal. Confirm the per-integration model is
   acceptable given it requires the registry to create/grant a service identity on
   the scoped root at integration registration.
2. **Version semantics**: `version` is opaque/integrator-owned (default — stamped &
   echoed, not interpreted). Confirm provisioning need not enforce monotonic upgrade
   (reject a lower version) — or add an optional guard.
3. **Role-binding convenience** (§2/§14.7.6): keep role/membership strictly in the
   shared directory (v1), or add an optional "ensure these role bindings exist" hook
   later.
4. **Space deletion**: ship `DELETE` (soft) in v1 or defer (default off).
5. **Automation ownership model**: bindings a space owns are created/reconciled by
   provisioning (default). Confirm whether the official folder_actions admin UI may
   *also* edit those provisioned bindings (dual-writer → drift on next `enforce`), or
   whether provisioned bindings are marked read-only/managed to avoid conflicting
   edits.
6. **Blueprint action set flexibility**: fixed per template (default) vs allowing the
   setup API (§6.3) to add bindings from an "optional actions" allow-list the
   blueprint declares. Affects how much post-provision customization is permitted.
