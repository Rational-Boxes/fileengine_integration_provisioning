# FileEngine Provisioning Service — Specification

A standalone **AGPL** FastAPI service lane that implements the **embedding
provisioning API surface** (§14.7 of the commercial embedding kit): a
server-to-server API for an embedding application to **stand up and maintain the
directory structures it needs** — standardized "project"/space folder trees with
owners, roles, ACLs, and metadata already applied — so end users then simply
operate within correctly-permissioned spaces.

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
                                                                ├─ own Postgres: templates + provisioned-space records (idempotency)
                                                                ├─ gRPC to core as the integration principal (mkdir / grant / metadata)
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

**Does not:** manage identity. Creating roles/groups and managing membership stays
in the shared LDAP source-of-truth (the integrator writes to the common directory,
or uses ldap_manager admin) — templates *bind to* existing roles/claims, they don't
create them. No end-user surface (the embed kit never calls this). No ACL-editing
UI. No token minting (that's §14.2).

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
- `prov_templates: [template_id, ..] | "*"` — templates it may apply.
- `prov_principals: [pattern, ..]` — role/claim patterns it may grant (e.g.
  `role:project:*`), never `system_admin`.
Every request is checked against these ceilings **before** any core call; the core
ACL check is the second, authoritative gate.

---

## 4. Data model (own Postgres, per-tenant schemas)

Owns its own database (`PROV_PG_*`, default DB `provisioning`), per-tenant schemas
`tenant_<slug>` (as folder_actions), idempotent DDL via `ensure_tenant_schema`.

- **`space_template`** — `template_id`, `name`, `description`, `enabled`, timestamps.
- **`space_template_version`** — `template_id`, `version` (int), `body` JSONB (the
  declarative template, §5), `params` (declared inputs), `created_by`, `created_at`.
  Templates are **immutable per version**; edits create a new version. Applied
  spaces record the version they were built from.
- **`provisioned_space`** — the idempotency + provenance record:
  `external_id` (integrator's key, **unique per (tenant, integration_id)**),
  `integration_id`, `tenant`, `template_id`, `version`, `space_uid` (core root uid),
  `params` JSONB, `status`, `created_at`, `last_applied_at`.
- **`provisioned_node`** — template-path → core `uid` map for a space (so reconcile
  is stable across runs and drift can be computed): `space_id`, `path`, `uid`,
  `kind`, `created_at`.
- **`apply_run`** — per-apply log (audit companion): `space_id`, `mode`, `dry_run`,
  `outcome`, `report` JSONB (per-node actions/warnings), `actor` (integration_id),
  `ts`.

Replica/failover envs mirror the lane (`PROV_PG_REPLICA_*`), read-mostly ops can use
the replica.

---

## 5. Template model (declarative, versioned)

As specified in embedding-kit §14.7.1 — an admin-authored, versioned, parameterized
description of a desired subtree, binding to **existing** roles/claims:

```jsonc
{
  "template_id": "project-standard",
  "version": 3,
  "params": ["project_code", "manager_role", "member_role"],
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
}
```
- **Inheritance:** a child with no `acls` inherits the parent's (mirrors the core's
  `ACL_INHERIT`); a child may add/override. Permission keys reuse the platform ACL
  letter vocabulary (`r w d l u v b s m i …`).
- **Validation** (`templates.py`): params referenced exist; principals match the
  token's `prov_principals` at apply time; no cyclic/oversized trees (bounded depth
  + node count); permission letters known.

---

## 6. API surface

Base path `/v1/provisioning`. All routes require auth (§3). Space routes require an
**integration-service token**; template routes require **admin**.

### 6.1 Spaces (integration-driven)
- `POST /v1/provisioning/spaces` — apply a template (create/reconcile a space).
  Body: `{ template_id, version?, tenant, parent_uid?, params:{...}, external_id,
  mode?: "create"|"reconcile"|"enforce", dry_run?: bool }`.
  - **Idempotent by `external_id`** (unique per tenant+integration): a repeat call
    returns the same space, never a duplicate. `parent_uid` defaults to the
    integration's scoped root (must be within `prov_roots`).
  - **`mode`:** `create` (fail if exists) · `reconcile` (default; create-missing,
    additive, non-destructive) · `enforce` (also correct drifted ACLs/metadata to
    match the template; never destructive to user content).
  - **`dry_run:true`** → return the **plan** without applying.
  - Response: `{ space_uid, external_id, template_id, version, status:"created"|
    "reconciled"|"noop", nodes:[{path, uid, action:"created"|"existing"|"updated"}],
    warnings:[...] }`.
- `GET /v1/provisioning/spaces?tenant=&external_id=&template_id=` — list within scope.
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

### 6.2 Templates (admin — official SPA *Provisioning* editor)
- `GET /v1/provisioning/templates` — list (an integration token sees only the
  subset its `prov_templates` permits).
- `GET /v1/provisioning/templates/{id}` — fetch (+ versions).
- `POST /v1/provisioning/templates` · `PUT /v1/provisioning/templates/{id}` — create
  / new immutable version (admin).
- `DELETE /v1/provisioning/templates/{id}` — retire (existing spaces keep their
  applied version).

### 6.3 Monitoring (loopback-only, unauthenticated)
`/healthz`, `/readyz`, `/poolz` bound to loopback / `PROV_MONITORING_ALLOW_IPS`,
per the platform monitoring convention. Not on the public surface.

---

## 7. Apply engine (`engine.py`) — semantics

- **Plan → apply.** Resolve the template version, substitute `params`, and compute
  a **plan**: the desired node tree + ACL/metadata. Diff against `provisioned_node`
  (existing map) + live core state to classify each node create/existing/updated.
- **Idempotent + reconcilable.** First apply creates; later applies converge to the
  (possibly upgraded) template. Safe to call unconditionally on every "new project"
  event and safe to retry.
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

---

## 8. Audit (`audit.py` → Redis audit stream)

Emit to the platform audit stream (drained by `audit_service`) — tamper-evident,
attributable. Events, each `{ integration_id, tenant, template_id, version,
space_uid, external_id, mode, outcome, source_ip }`:
- `provisioning.space_applied`, `provisioning.space_reconciled`,
  `provisioning.space_deleted`, and (admin) `provisioning.template_changed`.
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
2. **P1 — Templates.** template CRUD (admin) + validation + versioning store.
3. **P2 — Apply engine.** `POST /spaces` create + `reconcile`, idempotency by
   `external_id`, node map, core mkdir/grant/metadata as the integration principal,
   `provisioning.*` audit. The first usable release.
4. **P3 — Enforce + drift + dry-run + grants + soft-delete.**
5. **P4 — Hardening.** rate-limits, reconcile sweep, docs/examples (template
   authoring + onboarding), unified-stack + Ansible packaging.

Aligns with the embedding kit's **M-U (V1)**: this service is the concrete home of
the §14.7 provisioning surface.

---

## 14. Open decisions

1. **Acting identity** (§3): per-integration principal (default, least-privilege) vs
   a single provisioning service principal. Confirm the per-integration model is
   acceptable given it requires the registry to create/grant a service identity on
   the scoped root at integration registration.
2. **Template scope**: tenant-scoped templates (default, per-tenant schema) vs a
   global template library shared across tenants. 
3. **Role-binding convenience** (§2/§14.7.6): keep role/membership strictly in the
   shared directory (v1), or add an optional "ensure these role bindings exist" hook
   later.
4. **Space deletion**: ship `DELETE` (soft) in v1 or defer (default off).
