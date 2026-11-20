"""OIDC ID-token verification shared by CI OIDC and the people's OIDC sign-in (#32).

- `errors` names every refusal with a stable code.
- `keys` finds, caches and refreshes an issuer's discovery document and signing keys, over the
  guarded fetcher (`lakana_core.net.https_json`) only.
- `id_token` checks one ID token, in a fixed order, against the rules of its caller.

`lakana_core.ci_oidc` binds them to the CI settings (`LAKANA_CI_OIDC__*`) with its own messages;
the sign-in binds them to `LAKANA_OIDC__*` (`config`, `provider`).
"""
