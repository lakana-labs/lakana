"""The declarative table of platform API routes.

A contract test compares this table against the running server's OpenAPI
schema in both directions, so every SDK method has a corresponding route
here and every platform route (outside Iceberg REST) has an SDK method.
Path templates and path-parameter names are copied verbatim from the
server's own route definitions in `lakana_server.api.platform`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    """One platform API route.

    Parameters
    ----------
    method
        The HTTP method: `"GET"`, `"POST"`, `"PUT"`, `"PATCH"`, or `"DELETE"`.
    path
        The server's path template, e.g. `"/api/v1/environments/{name}"`.
    """

    method: str
    path: str


ROUTES: dict[str, Route] = {
    "ci_oidc.exchange": Route("POST", "/api/v1/auth/oidc/token"),
    "ci_trust_rules.create": Route("POST", "/api/v1/ci-trust-rules"),
    "ci_trust_rules.list": Route("GET", "/api/v1/ci-trust-rules"),
    "ci_trust_rules.explain": Route("POST", "/api/v1/ci-trust-rules/explain"),
    "ci_trust_rules.get": Route("GET", "/api/v1/ci-trust-rules/{rule_id}"),
    "ci_trust_rules.update": Route("PATCH", "/api/v1/ci-trust-rules/{rule_id}"),
    "ci_trust_rules.enable": Route("POST", "/api/v1/ci-trust-rules/{rule_id}/enable"),
    "ci_trust_rules.disable": Route("POST", "/api/v1/ci-trust-rules/{rule_id}/disable"),
    "ci_trust_rules.delete": Route("DELETE", "/api/v1/ci-trust-rules/{rule_id}"),
    "deploys.create": Route("POST", "/api/v1/environments/{env}/deploys"),
    "deploys.list": Route("GET", "/api/v1/environments/{env}/deploys"),
    "deploys.get": Route("GET", "/api/v1/environments/{env}/deploys/{deploy_id}"),
    "deployments.list": Route("GET", "/api/v1/environments/{env}/deployments"),
    "deployments.set_run_as": Route("PUT", "/api/v1/environments/{env}/deployments/{repo}/run-as"),
    "deployments.clear_run_as": Route(
        "DELETE", "/api/v1/environments/{env}/deployments/{repo}/run-as"
    ),
    "deployments.undeploy": Route("DELETE", "/api/v1/environments/{env}/deployments/{repo}"),
    "project_versions.get": Route(
        "GET", "/api/v1/environments/{env}/project-versions/{project_version_id}"
    ),
    "orphans.list": Route("GET", "/api/v1/environments/{env}/orphans"),
    "runs.start": Route("POST", "/api/v1/environments/{env}/runs"),
    "runs.list": Route("GET", "/api/v1/environments/{env}/runs"),
    "runs.get": Route("GET", "/api/v1/environments/{env}/runs/{run_id}"),
    "runs.cancel": Route("POST", "/api/v1/environments/{env}/runs/{run_id}/cancel"),
    "runs.resume": Route("POST", "/api/v1/environments/{env}/runs/{run_id}/resume"),
    "runs.log": Route("GET", "/api/v1/environments/{env}/runs/{run_id}/tasks/{asset}/log"),
    "jobs.list": Route("GET", "/api/v1/environments/{env}/jobs"),
    "jobs.get": Route("GET", "/api/v1/environments/{env}/jobs/{job}"),
    "jobs.start": Route("POST", "/api/v1/environments/{env}/jobs/{job}/runs"),
    "jobs.pause": Route("POST", "/api/v1/environments/{env}/jobs/{job}/pause"),
    "jobs.resume": Route("POST", "/api/v1/environments/{env}/jobs/{job}/resume"),
    "jobs.notify_test": Route("POST", "/api/v1/environments/{env}/jobs/{job}/notify-test"),
    "jobs.delivery": Route("GET", "/api/v1/environments/{env}/notifications/{delivery_id}"),
    "checks.for_run": Route("GET", "/api/v1/environments/{env}/runs/{run_id}/checks"),
    "checks.list": Route("GET", "/api/v1/environments/{env}/checks"),
    "checks.rows": Route("POST", "/api/v1/environments/{env}/runs/{run_id}/checks/rows"),
    "scheduler.status": Route("GET", "/api/v1/scheduler"),
    "workers.list": Route("GET", "/api/v1/workers"),
    "environments.create": Route("POST", "/api/v1/environments"),
    "environments.get": Route("GET", "/api/v1/environments/{name}"),
    "environments.list": Route("GET", "/api/v1/environments"),
    "environments.delete": Route("DELETE", "/api/v1/environments/{name}"),
    "environments.restore": Route("POST", "/api/v1/environments/{name}/restore"),
    "environments.update_storage": Route("PATCH", "/api/v1/environments/{name}/storage"),
    "environments.set_protection": Route("PATCH", "/api/v1/environments/{name}"),
    "environments.set_retention": Route("PUT", "/api/v1/environments/{name}/retention"),
    "environments.usage": Route("GET", "/api/v1/environments/{name}/usage"),
    "environments.orphaned_locations": Route(
        "GET", "/api/v1/environments/{name}/orphaned-locations"
    ),
    "environments.delete_orphaned_locations": Route(
        "POST", "/api/v1/environments/{name}/orphaned-locations/delete"
    ),
    "namespaces.create": Route("POST", "/api/v1/environments/{env}/namespaces"),
    "namespaces.get": Route("GET", "/api/v1/environments/{env}/namespaces/{path}"),
    "namespaces.list": Route("GET", "/api/v1/environments/{env}/namespaces"),
    "namespaces.delete": Route("DELETE", "/api/v1/environments/{env}/namespaces/{path}"),
    "tables.create": Route("POST", "/api/v1/environments/{env}/namespaces/{path}/tables"),
    "tables.list": Route("GET", "/api/v1/environments/{env}/namespaces/{path}/tables"),
    "tables.get": Route("GET", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}"),
    "tables.delete": Route("DELETE", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}"),
    "tables.retention": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/retention"
    ),
    "tables.set_retention": Route(
        "PUT", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/retention"
    ),
    "tables.expiring": Route("GET", "/api/v1/environments/{env}/expiring-tables"),
    "tables.undrop": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/undrop"
    ),
    "tables.list_dropped": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/dropped-tables"
    ),
    "tables.health": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/health"
    ),
    "tables.describe": Route("PATCH", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}"),
    "tables.describe_column": Route(
        "PUT",
        "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/columns/{column}/description",
    ),
    "tables.create_external": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/external"
    ),
    "tables.append": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/data"
    ),
    "tables.replace": Route(
        "PUT", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/data"
    ),
    "tables.merge": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/merge"
    ),
    "tables.lineage": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/lineage"
    ),
    "tables.maintain": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/maintenance"
    ),
    "artifacts.begin_upload": Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/uploads"
    ),
    "artifacts.put_blob": Route(
        "PUT",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}"
        "/uploads/{upload_id}/blobs/{sha256}",
    ),
    "artifacts.commit_upload": Route(
        "POST",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/uploads/{upload_id}/commit",
    ),
    "artifacts.abort_upload": Route(
        "DELETE",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/uploads/{upload_id}",
    ),
    "artifacts.delete_version": Route(
        "DELETE",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/versions/{version}",
    ),
    "artifacts.drop": Route(
        "DELETE", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}"
    ),
    "artifacts.list": Route("GET", "/api/v1/environments/{env}/artifacts"),
    "artifacts.list_uploads": Route("GET", "/api/v1/environments/{env}/artifacts/uploads"),
    "artifacts.get": Route("GET", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}"),
    "artifacts.versions": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/versions"
    ),
    "artifacts.version": Route(
        "GET", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/versions/{version}"
    ),
    "artifacts.blob": Route(
        "GET",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}"
        "/versions/{version}/blobs/{sha256}",
    ),
    "queries.submit": Route("POST", "/api/v1/environments/{env}/queries"),
    "queries.list": Route("GET", "/api/v1/environments/{env}/queries"),
    "queries.get": Route("GET", "/api/v1/environments/{env}/queries/{query_id}"),
    "queries.result": Route("GET", "/api/v1/environments/{env}/queries/{query_id}/result"),
    "queries.cancel": Route("POST", "/api/v1/environments/{env}/queries/{query_id}/cancel"),
    "principals.create": Route("POST", "/api/v1/principals"),
    "principals.get": Route("GET", "/api/v1/principals/{name}"),
    "principals.list": Route("GET", "/api/v1/principals"),
    "principals.permissions": Route("GET", "/api/v1/principals/{name}/permissions"),
    "principals.deactivate": Route("DELETE", "/api/v1/principals/{name}"),
    "groups.create": Route("POST", "/api/v1/groups"),
    "groups.list": Route("GET", "/api/v1/groups"),
    "groups.delete": Route("DELETE", "/api/v1/groups/{name}"),
    "groups.list_members": Route("GET", "/api/v1/groups/{name}/members"),
    "groups.add_member": Route("PUT", "/api/v1/groups/{name}/members/{principal_name}"),
    "groups.remove_member": Route("DELETE", "/api/v1/groups/{name}/members/{principal_name}"),
    "tokens.issue": Route("POST", "/api/v1/principals/{name}/tokens"),
    "tokens.list": Route("GET", "/api/v1/principals/{name}/tokens"),
    "tokens.revoke": Route("DELETE", "/api/v1/principals/{name}/tokens/{key_id}"),
    "task_tokens.mint": Route("POST", "/api/v1/task-tokens"),
    "task_tokens.revoke": Route("DELETE", "/api/v1/task-tokens/{key_id}"),
    "grants.create": Route("POST", "/api/v1/grants"),
    "grants.list": Route("GET", "/api/v1/grants"),
    "grants.revoke": Route("DELETE", "/api/v1/grants/{grant_id}"),
    "row_filters.create": Route("POST", "/api/v1/row-filters"),
    "row_filters.list": Route("GET", "/api/v1/row-filters"),
    "row_filters.revoke": Route("DELETE", "/api/v1/row-filters/{row_filter_id}"),
    "column_masks.create": Route("POST", "/api/v1/column-masks"),
    "column_masks.list": Route("GET", "/api/v1/column-masks"),
    "column_masks.revoke": Route("DELETE", "/api/v1/column-masks/{column_mask_id}"),
    "secrets.create": Route("POST", "/api/v1/environments/{environment_name}/secrets"),
    "secrets.list": Route("GET", "/api/v1/environments/{environment_name}/secrets"),
    "secrets.rotate": Route(
        "PATCH", "/api/v1/environments/{environment_name}/secrets/{secret_name}"
    ),
    "secrets.revoke": Route(
        "DELETE", "/api/v1/environments/{environment_name}/secrets/{secret_name}"
    ),
    "secrets.bind": Route(
        "PUT", "/api/v1/environments/{environment_name}/secrets/{secret_name}/use"
    ),
    "connections.create": Route("POST", "/api/v1/environments/{environment_name}/connections"),
    "connections.list": Route("GET", "/api/v1/environments/{environment_name}/connections"),
    "connections.get": Route(
        "GET", "/api/v1/environments/{environment_name}/connections/{connection_name}"
    ),
    "connections.update": Route(
        "PATCH", "/api/v1/environments/{environment_name}/connections/{connection_name}"
    ),
    "connections.delete": Route(
        "DELETE", "/api/v1/environments/{environment_name}/connections/{connection_name}"
    ),
    "task_connections.get": Route("GET", "/api/v1/tasks/self/connections/{name}"),
    "repositories.create": Route("POST", "/api/v1/repositories"),
    "repositories.list": Route("GET", "/api/v1/repositories"),
    "repositories.get": Route("GET", "/api/v1/repositories/{name}"),
    "repositories.update": Route("PATCH", "/api/v1/repositories/{name}"),
    "repositories.delete": Route("DELETE", "/api/v1/repositories/{name}"),
    "package_indexes.create": Route("POST", "/api/v1/package-indexes"),
    "package_indexes.list": Route("GET", "/api/v1/package-indexes"),
    "package_indexes.get": Route("GET", "/api/v1/package-indexes/{name}"),
    "package_indexes.update": Route("PATCH", "/api/v1/package-indexes/{name}"),
    "package_indexes.delete": Route("DELETE", "/api/v1/package-indexes/{name}"),
    "audit_events.list": Route("GET", "/api/v1/audit-events"),
    "whoami": Route("GET", "/api/v1/whoami"),
    "search": Route("GET", "/api/v1/environments/{env}/search"),
    "info": Route("GET", "/api/v1/info"),
}
