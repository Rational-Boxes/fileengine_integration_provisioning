# FileEngine Provisioning Service

Server-to-server API for standing up standardized **project/space directory
structures** (folder trees + ACLs + metadata) in a FileEngine instance on behalf of
an embedding application. It is the concrete home of the embedding kit's
provisioning surface (§14.7 of `fileengine_commercial_integration_components`).

- **What it is:** a FastAPI service lane (like `folder_actions`, `discussion`,
  `convert_search_ai`, `bcf_service`) that verifies an **integration-service token**
  (§14.2), then applies declarative, versioned **space templates** to the core via
  gRPC (`../python_interface`), idempotently and within a bounded scope, emitting
  `provisioning.*` audit events.
- **What it is not:** an end-user surface (the browser embed kit never calls it), an
  identity authority (roles/membership stay in the shared LDAP), or a token minter.
- **License:** AGPL-3.0-or-later. **Port:** `8100`. **Package:** `provisioning_service`.

## Status
Specification stage. See **[SPECIFICATIONS.md](./SPECIFICATIONS.md)** for the full
design: auth & acting identity, data model, template model, API surface, apply
engine semantics, audit, config, repo layout, deployment, and milestones.

## Position in the stack
```
Integrator backend ──(integration-service token)──▶ Provisioning Service :8100
                                                       ├─ own Postgres (templates + idempotency)
                                                       ├─ gRPC → core (mkdir / grant / metadata)
                                                       └─ Redis → provisioning.* audit
Official SPA (admin) ──▶ template editor (System config → Provisioning)
```

## Related
- Embedding kit + `§14` upstream proposal: `fileengine_commercial_integration_components`.
- Registry + credential UI (§14.1) and token exchange (§14.2): `ldap_manager` + `http_bridge`.
