/**
 * Where each step of Get started goes, for Home's list and the tour's last step.
 *
 * @packageDocumentation
 */
import type { GetStartedStepId } from "../api";
import { DOCS_URL, TUTORIAL_URL } from "../shell/docs";

/** Where a step goes: a place in the interface, or a page of the documentation site. */
type Way = { readonly to: string } | { readonly href: string; readonly command?: string };

/** The way of each step. */
export const STEP_WAYS: Record<GetStartedStepId, Way> = {
  connect_repository: { to: "/settings/repositories/new" },
  invite_team: { to: "/access/invites" },
  add_passkey: { to: "/account" },
  deploy_project: { href: TUTORIAL_URL, command: "lakana deploy --env dev" },
  deploy_project_local: {
    href: TUTORIAL_URL,
    command: 'lakana repo add analytics --kind local --url "$PWD"',
  },
  ci_deploy: { href: `${DOCS_URL}how-to/deploy-from-github-actions/` },
  open_table: { to: "/catalog" },
  run_query: { to: "/queries" },
};
