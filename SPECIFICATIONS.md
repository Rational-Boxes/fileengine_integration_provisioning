# FileEngine Provisioning Service — Specification

A standalone **AGPL** FastAPI service lane that implements the **embedding
provisioning API surface** (§14.7 of the commercial embedding kit): a
server-to-server API for an embedding application to **stand up and maintain the
project spaces it needs** — standardized "project"/space folder trees with owners,
roles, ACLs, metadata, **per-space automation** (folder_actions bindings: webhooks,
notify, sorter, review chains — each customized per project), **and the tenant-scoped
configs those depend on** (classifier sets, notify templates — extensible) already
applied — so end users then simply operate within correctly-permissioned,
correctly-wired spaces. It is a **rich, self-contained setup API**, not a folder clone.

Follows the established FileEngine FastAPI service-lane conventions (as
`folder_actions`, `discussion`, `convert_search_ai`, `bcf_service`): `src/` layout,
shared `FILEENGINE_*` config + service-private `PROV_*` config, HS256 bridge-JWT
auth with introspection fallback, gRPC core access via `../python_interface`, its
own per-tenant Postgres, Redis audit emission, loopback monitoring endpoints, and a
`pyproject` console-script entrypoint.

- **License:** AGPL-3.0-or-later (matches the platform service lanes).
- **Port:** `8100` (next free after folder_actions :8099).
- **Package:** `provisioning_service` (dir `commercial_provisioning/`).

> **Cross-reference convention:** `§14.1/§14.2/§14.6/§14.7` (registry, exchange, edge
> CORS, provisioning proposal) refer to the **embedding-kit** spec
> (`fileengine_commercial_integration_components`). This document's own numbered
> sections are §1–§14 (Decisions) + §14a; there is no §14.1/§14.2/§14.7 *here*.

---

## 1. Role in the stack

```
Integrator backend  ──(integration-service token, §14.2)──▶  Provisioning Service (:8100)
  "provision project X"                                         │
                                                                ├─ own Postgres: templates + provisioned space/node/binding records (idempotency)
                                                                ├─ gRPC to core as the integration principal (mkdir / grant / metadata)
                                                                ├─ REST to folder_actions (:8099): per-space automation bindings (§7.1)
                                                                └─ Redis: emit provisioning.* audit events
(No stored templates: the integrator passes a rich inline blueprint per call, see 5.0.)
```

- **Callers.** The integrator's *backend* (or a trusted orchestrator), never the
  browser or the embed kit. Requests carry an **integration-service token** (§14.2:
  `sub = integration_id`, `amr:["integration"]`, `provisioning` capability, scope
  claims). Blueprints are authored in the integrator's own system and passed inline (§5.0); no admin authoring surface here.
- **Depends on** (upstream, embedding-kit §14): the integration **registry**
  (deployment-level **config files** — public keys + scopes — allocated by a
  deployment/cluster **management CLI**, not a tenant UI; §14.1) and the **exchange
  endpoint** (§14.2, http_bridge) that mints the integration-service token from it.
  This service **consumes** those tokens; it does not mint them.
- **Writes go through the core** (gRPC via `python_interface`), so all existing
  ACL, versioning, tenancy, and audit invariants hold unchanged. This service adds
  orchestration + a blueprint-snapshot / idempotency store, not a new write path.

---

## 2. Scope boundary (what this service does / does not do)

**Does:** blueprint validation; declarative space **apply/reconcile** (folders + ACL +
metadata + automation); creation of **tenant-scoped dependent resources** (classifier
sets, notify templates, extensible; §5.8); idempotent project provisioning keyed by the
integrator's `external_id`; drift inspection; scope enforcement; `provisioning.*` audit.

**Does not:** manage identity or tenant lifecycle. Creating roles/groups and
managing membership stays in the shared LDAP source-of-truth (the integrator writes
to the common directory, or uses ldap_manager admin) — templates *bind to* existing
roles/claims, they don't create them. **Tenants** are likewise defined by their LDAP
OU structure (§3.2), initialized by the embedding application *before* FileEngine is
touched; provisioning only validates + adopts them. No end-user surface (the embed
kit never calls this). No ACL-editing UI. No token minting (that's §14.2). **No
browser-facing session/OAuth support** — the embed kit's session handshake needs no
integrator server (embedding-kit §6): its browser-facing pieces (OAuth callback page,
`session/config`) are public and live at the **FileEngine edge / http_bridge**, *not*
here. This service is **server-to-server only** (called by the integrator's backend
with an integration-service token); it is not browser-exposed, so it is the wrong home
for the public callback. Server-to-server integration helpers *could* co-locate here,
but the session handshake needs none.

---

## 3. Authentication & acting identity

Mirrors the lane's auth stack (`jwt_verify.py` + `bridge_auth.py` + `http_auth.py`).
The integration-service token this service consumes is minted from the integration's
**one asymmetric keypair** — the *same* credential that authorizes session hand-off
(embedding-kit §14); there are **no separate/symmetric service credentials** for
provisioning. This service just verifies the resulting bridge JWT:

- **Local HS256 verify** of the bearer against the shared `FILEENGINE_JWT_SECRET`
  (alg-pinned, `exp` enforced, roles map scoped by `X-Tenant`), with a **bridge
  introspection fallback** (`PROV_BRIDGE_URL` → `GET /v1/auth/introspect`) when the
  shared secret is absent — identical to how folder_actions/csai/discussion accept
  the bridge token.
- **Two token types:**
  - **Integration-service token** (space ops) — must carry `amr:["integration"]`,
    the `provisioning` capability, and the **scope claims** (§3.1). Enforced by
    `require_integration(scope=...)`.
  - **Admin bridge JWT** (manual ops / validate; optional) — normal tenant-admin, enforced by
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

### 3.0 Deployment model — bespoke per external SaaS, deployment-wide integration
**Policy:** FileEngine embedding is **not offered on shared, generic multi-tenant
instances** — each external SaaS leveraging FileEngine gets its **own bespoke
deployment**, dedicated to that integrator and itself multi-tenant across *that SaaS's*
tenants. A deployment therefore normally serves **one integration**, which is
**deployment-wide** (not scoped to a single tenant) and is responsible for **spinning
up many tenants dynamically** — a *fully-integrated tenant provision* per tenant.
- **New tenant = LDAP OU (external app) → adopt (FileEngine).** The external app
  creates the tenant's OU structure in the **shared LDAP** (Posture B); provisioning
  then validates + adopts it (§3.2) and applies that tenant's blueprints/resources.
  Tenant *identity* is the external app's job; tenant *content* is provisioning's.
- **Scope is deployment-wide by default.** `prov_tenants` defaults to `"*"` (any
  tenant in the deployment) because tenants are created on the fly; each request's
  tenant is still LDAP-validated (§3.2). `prov_roots`/`prov_namespace` scope *within*
  each tenant.
- **Normally one integration per deployment.** The deployment is bespoke to a single
  external SaaS, so there is typically **one** integration; the per-integration
  `prov_namespace` is kept for robustness (and any secondary integration) but rarely
  contends.

### 3.1 Scope claims (carried by the integration-service token)
Minted by the exchange endpoint from the integration registry (deployment config,
embedding-kit §14.1) so this service enforces without reading the registry itself:
- `prov_tenants: [pattern, ..] | "*"` — tenants the integration may operate in.
  **Because integrations are deployment-wide and create tenants dynamically (§3.0),
  the default is `"*"` (any tenant in the deployment, still LDAP-validated, §3.2);**
  set a pattern (e.g. a tenant-name prefix) only to sub-scope.
- `prov_roots: [uid|path, ..]` — root prefix(es) under which spaces may be created.
- `prov_principals: [pattern, ..]` — role/claim patterns it may grant (e.g.
  `role:project:*`), never `system_admin`.
- `prov_actions: [machine_name, ..] | "*"` — **optional** restriction on which
  installed folder_action machine names it may configure. **Default `"*"` (any
  installed action, §5.3);** set a list only to narrow a specific integration. Also
  gates whether it may set `secret`s.
- `prov_resources: [type, ..] | "*"` — **optional** restriction on which tenant-scoped
  resource types it may create (`classifier_set`, `notify_template`, …; §5.8). Default
  `"*"` (any registered resource handler).
- `prov_namespace: <prefix>` — the **namespace prefix bound to this integration
  credential** at registration (§14.1). Provisioning prefixes every tenant-scoped
  resource it creates with it (`<prefix>/<name>`) and confines the integration to its
  own namespace, so integrations never collide on a shared resource name. Authoritative
  (from the registry), not settable by the caller.
- `aip: [ip|cidr, ..]` — the integration's **source-IP allow-list** (embedding-kit
  §14.2a), stamped into the token by the exchange endpoint. This service **re-enforces**
  it (§3.3): a valid token used from an off-list host is rejected, so a leaked token or a
  compromised key still cannot reach provisioning from an unknown network.
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

### 3.3 Source-IP enforcement (defense-in-depth)
On top of verifying the bridge JWT, this service **re-enforces the integration's
source-IP allow-list** — a valid token used from an off-list host is rejected. This
means that even a **compromised private key or a leaked token** cannot reach
provisioning unless the caller is also on a whitelisted network (embedding-kit §14.2a).
- **Source of the list:** the token's `aip` claim (§3.1), stamped by the exchange
  endpoint from the registry `allowed_ips` — so this service needs no separate registry
  read. (A deployment may also pin an allow-list in config as a backstop.)
- **Trusted-proxy correctness:** the client IP is derived from `X-Forwarded-For` under a
  **trusted-proxy** config (`PROV_TRUSTED_PROXIES`), matching the platform's audit
  IP-derivation convention — the check uses the *derived* client IP, never the edge
  proxy's. Enforced by a request middleware ahead of the route handlers.
- **Reject + audit:** an off-list source is `403` and emits a `provisioning.*` rejection
  (security signal); the server-to-server nature means the allow-list is stable
  (the integration's backend egress IPs).

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
- **`provisioned_node`** — blueprint-path → core `uid` map for a space (so reconcile
  is stable across runs, drift can be computed, and `${node:<path>}` references
  resolve): `space_id`, `path`, `uid`, `kind`, `created_at`.
- **`provisioned_binding`** — the space's automation map: `space_id`, `ref`,
  `folder_uid`, `fa_binding_id` (folder_actions id), `type`, `config_hash`,
  `secret_refs`, `created/updated_at`. Backs idempotent reconcile of actions and
  drift detection; holds **no raw secrets** (§5.4).
- **`provisioned_resource`** — tenant-scoped dependent configs (§5.8), keyed by
  **(tenant, namespace, type, name)** (not per-space): `tenant`, `namespace` (the
  integration's bound prefix, §3.1), `type`, `name`, `owning_service`,
  `service_object_id` (e.g. classifier_set id / template id), `config_hash`,
  `managed_by`, `ref_count` (spaces referencing it), `created/updated_at`. Backs
  idempotent reconcile + independent lifecycle; the namespace prevents cross-integration
  name collisions.
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
- **`${node:<blueprint-path>}`** — a **symbolic folder reference** resolved at apply
  time to *this space's* freshly-minted folder UUID (§5.3). Any folder reference in
  an action's config (sorter route destinations, `move_review` on_approved/on_rejected,
  webhook `move_to`) MUST use this token — **never a literal UUID**, which would point
  at another space or nothing.
- **`${resource:<ref>}`** — a reference to a **tenant-scoped dependent resource** (a
  classifier set, notify template, …) declared in the blueprint's `resources` section
  (§5.8), resolved at apply time to that resource's id (e.g. a sorter's
  `classifier_set_id`, a notify action's `template`). Guarantees the referenced config
  exists before the action that needs it.

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
Each entry creates a folder_actions binding on a folder in the tree (by blueprint
`path`/`ref`), with **parameterized** `config`. `type` is **any installed
folder_action plug-in, referenced by its machine name** (`type_name`) — the
built-ins (`webhook · notify · sorter · move_review · raise_review`) *and* any
custom/future plug-in registered in folder_actions; validated against its live
`/action-types` (§5.5). Binding-level `on_events` + `mime_types` apply. Every field
is `${param}`-substitutable, so the same blueprint yields per-project-distinct
automation.

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

### 5.5 Validation (`blueprint.py`)
Referenced params exist and are typed; `principal` params ⊆ `prov_principals`
**and must already exist in the target tenant's directory** — every referenced
role/claim is validated against LDAP for the tenant and an **unknown role is
rejected** (provisioning binds to existing roles, never creates them); action
`type` is **any installed folder_action machine name** (validated against
folder_actions' live `/action-types`; optionally further restricted by
`prov_actions`); **every `${node:<path>}` reference resolves to a folder declared in
the tree** (else rejected — no dangling destinations); no cyclic/oversized trees
(bounded depth + node count); permission letters and event names known; each
action's `config` shape is validated against that action type's declared schema
from `/action-types`.

### 5.6 Versioning & trackability (stamped on the space root metadata)
Rather than a stored template version, the integrator passes a **`version`** (their
own version of the provisioning structure) on each apply. Provisioning **stamps it
onto the space root folder's metadata** in the core, so the version travels with the
space and any client can inspect it:
- Root metadata keys written on apply: `provision.name`, `provision.version`,
  `provision.integration_id`, `provision.external_id`, `provision.applied_at`, and the
  well-known **`managed_by`** key (§5.7).
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

### 5.7 `managed_by` — a well-known metadata key (externally-managed marker)

`managed_by` is a **reserved, well-known node-metadata key** — a platform convention,
not a provisioning- or folder_actions-specific field — that marks a resource as
**externally managed**, so any **administrative UI can flag it** and warn an operator
that they're editing configuration owned by an integration.

- **Written by provisioning** (via the core metadata API) on **every folder it
  creates** (root and children), value = the managing `integration_id` (a stable
  identifier; the `provision.*` keys carry the details — blueprint name, version,
  external_id). Cheap — provisioning is creating those folders anyway.
- **Honored by administrative UIs** (decision 5, §14a). The official client reads
  `managed_by` off a node's metadata and shows an **"externally managed by
  `<integration>` — changes may be overwritten on the next provisioning sync"** badge/
  warning — in the folder_actions binding editor (a binding lives on a folder that
  carries the key), the file-details drawer, and any config surface. Editing stays
  allowed (not hard-locked); the warning is advisory, and a later `enforce`
  reconciles.
- **Scope of the flag.** Because it sits on the folder(s), it naturally covers both
  the **structure** and the **automation** attached to those folders — no separate
  per-binding field is needed. A UI may check the node itself and, for robustness,
  walk ancestors (a child of a managed space is managed).
- **Reserved key.** `managed_by` (and the `provision.*` namespace) are reserved
  well-known keys; blueprints/users should not set them by hand — provisioning owns
  them. Clearing them (un-manage) is a provisioning operation (e.g. soft-delete /
  a future "release" op), not an ad-hoc metadata edit.

### 5.8 Tenant-scoped dependent resources (classifier sets, notify templates, …)

Automation bindings depend on **tenant-scoped configuration objects** that must exist
before a binding can reference them — a sorter's **classifier set**, a notify action's
**notify template**, and more to come. Provisioning therefore creates these too, so a
blueprint yields a **self-contained** project setup rather than assuming pre-existing
config.

**Declared in the blueprint `resources` section** (parameterizable like everything
else); actions reference them by `${resource:<ref>}` (§5.1):
```jsonc
"resources": [
  { "type": "classifier_set", "ref": "mfg",     "name": "${project_code}-mfg",
    "body": { /* classifier set def: classifiers/terms/weights (folder_actions YAML/JSON) */ } },
  { "type": "notify_template", "ref": "approved", "name": "${project_code}-approved",
    "body": { "subject": "${project_code} approved", "html": "…", "text": "…" } }
],
"actions": [
  { "ref": "auto-sort", "folder": "${project_code}", "type": "sorter",
    "config": { "classifier_set_id": "${resource:mfg}" } },
  { "ref": "notify-approved", "folder": "Approved", "type": "notify",
    "config": { "template": "${resource:approved}", "recipients": "${notify_recipients}" } }
]
```

- **Tenant-scoped + integration-namespaced.** Resources live at the **tenant** level
  and may be shared by many spaces of the **same integration**. Their identity is
  **(tenant, integration namespace, type, name)** — the **namespace** is assigned to
  the integration when its credentials are created (§14.1) and carried in the token as
  `prov_namespace` (§3.1); the integration cannot set or spoof it. This prevents
  cross-integration collisions on a shared name (`mfg`), scopes an integration to *its
  own* resources, and makes reconcile deterministic. The `name` an integration writes
  is implicitly within its namespace; the underlying folder_actions object is created
  namespaced (e.g. `<namespace>/<name>`), so two integrations' `mfg` sets never clash.
  (This differs from spaces, which key on `external_id` under `prov_roots`.)
- **Created before actions.** Apply order is **resources → folders → ACL/metadata →
  actions**, so `${resource:...}` always resolves to an existing id when a binding is
  wired (§7).
- **Owned by their service; created via that service's API.** Classifier sets and
  notify templates are **folder_actions** objects (`/classifier-sets`,
  `/notify-templates`) — provisioning orchestrates folder_actions (already in the data
  path, §7.1) to create/reconcile them. Because these are admin config objects,
  creating them is the *programmatic config-service* path (consistent with the
  boundary), the counterpart of the official client's classifier/template editors.
- **Marked managed.** Provisioned resources carry the `managed_by` marker so the admin
  editors warn (§5.7); since they are **service-owned objects, not nodes**, the marker
  is a `managed_by` **field on the object record** rather than node metadata — a small
  additive field on folder_actions' classifier-set / notify-template models (§14a).
- **Lifecycle is independent of spaces.** A shared tenant resource is **not** deleted
  when one referencing space is soft-deleted (others may use it); resource lifecycle is
  its own `POST/DELETE /resources` concern (§6) plus reference-counting.

**Extensible — a resource-handler registry (the extension point).** Provisioning does
not hardcode the config types. Each `type` maps to a **resource handler** that declares
its owning service and how to **create / reconcile / diff (hash) / delete** that object
via the owning service's API. Built-in handlers: `classifier_set`, `notify_template`
(→ folder_actions). **Future dependent-config types** (e.g. other services'
tenant-scoped configs) are added by **registering a new handler** — the blueprint schema
and apply engine stay generic. This mirrors "any installed folder_action by machine
name" (§5.3): the *set of resource types* is provisioning's extension surface, gated per
integration by `prov_resources` (§3.1).

---

## 6. API surface

Base path `/v1/provisioning`. All routes require auth (§3). Space routes require an
**integration-service token**; the /blueprints/validate aid (§6.2) accepts an integration or admin token.

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
  blueprint name+version, and **drift** vs the last-applied blueprint.
- `PATCH /v1/provisioning/spaces/{space_uid}` — re-apply / upgrade to a newer
  blueprint/version (reconcile|enforce), or update params.
- `POST /v1/provisioning/spaces/{space_uid}/grants` — bounded ACL adjustment
  (add/remove a permitted role/claim grant within `prov_principals`), for
  membership-shaped changes not warranting a full re-apply.
- `DELETE /v1/provisioning/spaces/{space_uid}` — **soft-delete** (supported in v1;
  scope-checked; honors the core's recoverable-delete + versioning). Removes the
  space's tree (recoverably) and its `managed` bindings; the `provisioned_space`
  record is marked deleted (retained for audit/idempotency).

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
notify action's recipients, etc., without re-applying the whole blueprint. Bounded by
the token's `prov_principals` and (optionally) `prov_actions`; bindings may target
**any installed folder_action by machine name** (§5.3), and referenced roles must
exist in the tenant (§5.5). Their folders carry the `managed_by` marker (§5.7), so
admin UIs flag them as externally managed.
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

### 6.4 Tenant resources (dependent configs — classifier sets, notify templates, …)
Tenant-scoped resources (§5.8) are normally created *inline* via a blueprint's
`resources` section on `POST /spaces`, but they also have a **standalone** API for
managing tenant-level config not tied to a single space (idempotent by
`(tenant, namespace, type, name)`, confined to the integration's `prov_namespace`
prefix and gated by `prov_resources`):
- `POST /v1/provisioning/resources` — create/reconcile a resource
  `{ tenant, type, name, body }` → `{ type, name, service_object_id, status }`.
- `GET /v1/provisioning/resources?tenant=&type=` — list within scope.
- `GET /v1/provisioning/resources/{type}/{name}` — inspect (+ drift vs `body`).
- `PATCH /v1/provisioning/resources/{type}/{name}` — reconcile/enforce to a new `body`.
- `DELETE /v1/provisioning/resources/{type}/{name}` — remove **only if `ref_count`=0**
  (no space still references it), unless `force`. Honors the owning service's delete.
All operations dispatch through the **resource-handler registry** (§5.8) to the owning
service (folder_actions for the built-ins), and mark the object `managed_by` (§14a).

### 6.5 Monitoring (loopback-only, unauthenticated)
`/healthz`, `/readyz`, `/poolz` bound to loopback / `PROV_MONITORING_ALLOW_IPS`,
per the platform monitoring convention. Not on the public surface.

---

## 7. Apply engine (`engine.py`) — semantics

- **Resolve tenant first (§3.2).** Validate the request tenant against LDAP and boot
  its metadata (reject `tenant_not_initialized` if the OU is absent); the core
  materializes the tenant lazily on the first write. Only then plan/apply.
- **Apply order: resources → folders → ACL/metadata → actions.** Tenant-scoped
  `resources` (§5.8) are created/reconciled **first** (via their handlers), yielding the
  ids that `${resource:...}` references resolve to; then the folder tree + ACL/metadata;
  then the automation bindings (which now have both `${node:...}` and `${resource:...}`
  resolved). This ordering guarantees no binding is wired to a not-yet-existing
  classifier set or template.
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
- **Drift** (inspect) surfaces divergence from the blueprint so an operator or the
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
   :8099) as the acting integration principal: `POST /actions` (create) or
   `PUT /actions/{id}` (update) with the resolved, param-substituted config;
   `PUT /actions/{id}/routes` for sorter routes (destinations resolved likewise).
   The binding lives on a folder that carries the well-known **`managed_by`** metadata
   key (§5.7), which is how admin UIs flag it as externally managed (§14a).
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
- **Managed marker + dual-writer (decision 5).** The provisioned folders carry the
  well-known **`managed_by`** metadata key (§5.7); the folder_actions admin UI (and
  other config surfaces) read it and **warn** on a direct edit ("externally managed by
  `<integration>` — may be overwritten on the next provisioning sync"). Manual edits
  are permitted but flagged, and a later `enforce` reconciles config back to the
  blueprint. Because the marker is node metadata on the folder, **no folder_actions
  schema change is needed** — only that admin UIs honor the key (§14a).
- **Boundary note:** configuring folder_actions here is the *programmatic
  config-service* path (server-to-server), the counterpart of the official client's
  *interactive* folder_actions admin UI — both are admin surfaces; neither is the
  end-user embed kit.

---

## 8. Audit (`audit.py` → Redis audit stream)

Emit to the platform audit stream (drained by `audit_service`) — tamper-evident,
attributable. Events, each `{ integration_id, tenant, blueprint_name, version,
space_uid, external_id, mode, outcome, source_ip }`:
- `provisioning.space_applied`, `provisioning.space_reconciled`,
  `provisioning.space_deleted`, `provisioning.action_configured` (binding
  create/update, secret rotate — secrets redacted), and
  `provisioning.resource_applied` (tenant-scoped classifier set / notify template
  create/reconcile/delete, §5.8).
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
- IP enforcement (§3.3): `PROV_TRUSTED_PROXIES` (CIDR list of edge proxies whose
  `X-Forwarded-For` is trusted for client-IP derivation); `PROV_ENFORCE_AIP` (default
  true — reject a token's request from outside its `aip` allow-list); optional
  `PROV_IP_ALLOWLIST` deployment backstop.
- Orchestration: `PROV_FOLDER_ACTIONS_URL` (:8099) + `PROV_FOLDER_ACTIONS_TIMEOUT_S`
  for applying per-space automation bindings (§7.1).
- Limits: `PROV_MAX_TREE_NODES`, `PROV_MAX_TREE_DEPTH`, `PROV_APPLY_RATE_PER_MIN`
  (per `integration_id`), `PROV_ALLOW_SPACE_DELETE` (default **true** — soft-delete
  is supported (decision 4); set false to disable the DELETE route for a deployment).
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
    ├── api.py                    # space + blueprint/setup routers (§6)
    ├── jwt_verify.py             # local HS256 verify (shared secret)
    ├── bridge_auth.py            # introspection fallback
    ├── http_auth.py             # bearer extraction + token-type checks
    ├── ldap_auth.py              # identity/roles helpers
    ├── _client.py                # python_interface bootstrap (as folder_actions)
    ├── core_client.py            # CoreClient acting as the integration principal
    ├── schema.py                 # per-tenant DDL (§4) + ensure_tenant_schema
    ├── db.py / stores.py         # Postgres access (templates, spaces, node map, runs)
    ├── blueprint.py               # blueprint parse/validate + param substitution
    ├── engine.py                 # plan → apply/reconcile/enforce + drift (order: resources→folders→actions)
    ├── resources.py              # tenant-resource handler registry (classifier_set, notify_template, …)
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

- **Unit:** blueprint validation + param substitution; plan/diff engine
  (create/reconcile/enforce, drift); scope enforcement (deny out-of-scope root/
  principal; reject a role not present in the tenant); idempotency (repeat
  `external_id` → same space).
- **Auth:** integration-service vs admin token gating; introspection fallback;
  **source-IP enforcement** (§3.3) — off-list source rejected `403`; trusted-proxy
  `X-Forwarded-For` derivation (not the proxy IP); `aip` claim honored.
- **Integration:** against a running dev stack (`scripts/start_backend_services.sh`)
  — apply a blueprint, assert the folder tree + ACLs + metadata + `managed`
  folder_actions bindings (`${node}` destinations resolved) in the core, re-apply
  (noop), bump version (enforce + re-stamp root metadata), dry-run (no writes),
  soft-delete, audit events emitted.
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
4. **P3 — Automation, resources & setup (the rich part).** `resources` (§5.8) via the
   resource-handler registry (classifier_set + notify_template → folder_actions),
   `${resource:...}` resolution, `provisioned_resource` store + `/resources` API;
   `actions` in the blueprint; `${node:...}` resolution; folder_actions orchestration
   (§7.1) incl. sorter routes + secrets; the per-space configuration/setup API (§6.3);
   `provisioned_binding` store + reconcile; the `managed_by` marker (§5.7/§14a). This is
   what makes provisioning a full, self-contained project setup, not a folder clone.
5. **P4 — Enforce + drift + dry-run + grants + soft-delete** (over structure *and*
   actions).
6. **P5 — Hardening.** rate-limits, reconcile sweep, docs/examples (blueprint
   authoring + onboarding), unified-stack + Ansible packaging.

Aligns with the embedding kit's **M-U (V1)**: this service is the concrete home of
the §14.7 provisioning surface.

---

## 14. Decisions (resolved)

1. **Acting identity — per-integration, least-privilege** (§3). Provisioning acts on
   the core as the integration's own service principal, bounded by the grants it
   holds on its scoped root (established at registration). Not a god-mode principal.
2. **Version — opaque, integrator-owned** (§5.6). Stamped on the root metadata and
   echoed; never interpreted. No monotonic guard — version discipline (incl. not
   regressing) is the integrating system's concern.
3. **Referenced roles must exist in the tenant** (§5.5). Blueprints bind only to
   roles/claims that **already exist in the target tenant's shared directory**;
   apply validates every referenced `principal` against LDAP for the tenant and
   **rejects unknown roles**. Provisioning never creates roles/membership.
4. **Soft-delete — supported** (§6.1). `DELETE /spaces/{uid}` performs a scope-checked
   **soft-delete** through the provisioning API (honoring the core's recoverable-
   delete + versioning). Available in v1.
5. **Managed marker via the well-known `managed_by` metadata key** (§5.7). Provisioned
   folders carry a reserved **`managed_by`** node-metadata key (value = `integration_id`);
   admin UIs read it and warn that the configuration is externally managed (a manual
   edit is allowed but flagged — the next `enforce` reconciles). It is a **platform
   metadata convention**, so **no folder_actions schema change** — only that admin UIs
   honor the key (§14a).
6. **Any installed folder_action by machine name** (§5.3). Blueprints may configure
   **any installed folder_action plug-in, referenced by its machine name**
   (`type_name`), validated against folder_actions' live `/action-types` set — not a
   fixed or blueprint-declared subset. `prov_actions` (§3.1) remains an *optional*
   per-integration restriction; default is "any installed".
7. **Tenant-scoped dependent resources** (§5.8) — resolved: provisioning creates them
   (classifier sets, notify templates) via an **extensible resource-handler registry**,
   referenced by `${resource:...}`, created before actions.
   - **Name-collision — resolved:** a **namespace prefix is bound to each integration
     credential** at registration (§14.1) and carried as `prov_namespace` (§3.1).
     Resource identity is `(tenant, namespace, type, name)`; the underlying object is
     created `<prefix>/<name>`, so integrations never collide and each is confined to
     its own namespace.
   - **GC — resolved (default):** shared resources are **ref-counted** and removed only
     by an **explicit** `DELETE /resources` (requires `ref_count`=0 unless `force`);
     **no auto-GC** when a referencing space is deleted (a sibling space may still use
     it). Provisioning may offer a later "prune orphaned (ref_count 0) namespace
     resources" op, but does not auto-delete.

### 14a. Upstream dependency (frontend) — honor the `managed_by` metadata key
Decision 5 is satisfied by a **frontend** convention, not a backend schema change:
- **Provisioning** stamps the well-known **`managed_by`** node-metadata key (§5.7) on
  the folders it creates — using the existing core metadata API (no new endpoint).
- **The official client** reads `managed_by` off a node's metadata (it already fetches
  node metadata for the details drawer) and shows an **"externally managed by
  `<integration>` — changes may be overwritten on the next provisioning sync"** badge/
  warning wherever an admin edits that node's configuration: the **folder_actions
  binding editor** (`BindingEditor`/`FolderActionsPanel`), the file-details drawer,
  and ACL/metadata panels. Advisory only — editing stays enabled.
- Reserve `managed_by` (+ the `provision.*` namespace) as well-known keys across the
  platform so the marker is honored consistently, not just for folder_actions.

**Service-owned config objects (not nodes).** Tenant-scoped resources (§5.8) —
classifier sets, notify templates — are **folder_actions records, not core nodes**, so
node metadata doesn't apply. For these, `managed_by` is a small additive **field on the
object model** (folder_actions `classifier_set` / `notify_template`), surfaced in its
admin API and honored by the classifier/template editors with the same "externally
managed — may be overwritten on next sync" warning. This is the one small backend
addition (a nullable `managed_by` column on those two config tables); bindings and
folders still use the node-metadata key (no binding schema change). Both future dependent
resource types should follow the same `managed_by`-field convention.
