/**
 * An environment's deployments on its page in Settings › Environments: each repository
 * deployed there, its commit, and the service account its runs write as; for those who manage
 * the environment, Set run-as, Change and Clear.
 *
 * @remarks
 * Binding an account asks for the passkey touch first, as the server does: every run of the
 * deployment then writes with that account's rights. Clearing removes rights and asks for none.
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useRef, useState } from "react";
import { Button, Form, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { clearRunAs, setRunAs } from "../api/ciTrust";
import type { DeploymentOut } from "../api/jobs";
import { listDeployments } from "../api/jobs";
import { FormFoot } from "../access/FormFoot";
import { usePrincipalSearch } from "../access/suggest";
import { ReadFailed } from "../catalog/ReadFailed";
import { ActionDialog } from "../shell/ActionDialog";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { NamePicker } from "../shell/NamePicker";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "./environmentPageStrings";

const P = "settings.environmentPage.runAs";

/** Properties of {@link SetRunAsDialog}. */
interface SetRunAsDialogProps {
  /** The environment. */
  readonly environment: string;
  /** The deployment. */
  readonly deployment: DeploymentOut;
  /** Called with the account once it is bound. */
  readonly onDone: (principal: string) => void;
  /** Closes the dialog without binding. */
  readonly onClose: () => void;
}

/**
 * Asks for the service account and binds it, after the passkey touch; the server's refusal
 * (a name that is no active service account) stays in the dialog with the name kept.
 *
 * @param props - The environment, the deployment and the handlers.
 * @param props.environment - The environment.
 * @param props.deployment - The deployment.
 * @param props.onDone - Called once the account is bound.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
function SetRunAsDialog({
  environment,
  deployment,
  onDone,
  onClose,
}: SetRunAsDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const suggest = usePrincipalSearch();
  const [principal, setPrincipal] = useState(deployment.run_as ?? "");
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  const name = principal.trim();
  const held =
    name === ""
      ? t(`${P}.principalNeeded`)
      : name === deployment.run_as
        ? t(`${P}.nothingChanged`)
        : null;
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          // Sent again while the change waits, the form changes nothing more.
          if (attempt.busy) return;
          attempt.run(async () => {
            await setRunAs(environment, deployment.repository, name);
            onDone(name);
          });
        }}
      >
        <Heading slot="title">
          {t(`${P}.dialogTitle`, { repository: deployment.repository, name: environment })}
        </Heading>
        <NamePicker
          label={t(`${P}.principal`)}
          help={t(`${P}.principalHelp`)}
          value={principal}
          onChange={setPrincipal}
          search={suggest}
          noMatch={(text) => t("picker.noPrincipal", { name: text })}
          isRequired
        />
        <FormFoot
          attempt={attempt}
          submit={t(`${P}.save`)}
          held={held}
          blocked={false}
          onCancel={close}
        />
      </Form>
    </ActionDialog>
  );
}

/** Properties of {@link RunAsPanel}. */
export interface RunAsPanelProps {
  /** The environment's name. */
  readonly environment: string;
  /** Whether it is protected, which decides what an unbound deployment does. */
  readonly isProtected: boolean;
  /** Whether the person manages it, and so may bind and clear. */
  readonly manages: boolean;
}

/** What is open: a deployment's dialog to set, or to clear. */
type Open = { readonly kind: "set" | "clear"; readonly deployment: DeploymentOut };

/**
 * Renders the deployments in their own scrolling region, each with who its runs write as (or
 * what an unbound one does here), and for a manager its buttons. After a change the list is
 * read again, it says what changed, and the focus goes to that deployment's button.
 *
 * @param props - The environment, its protection and whether the person manages it.
 * @param props.environment - The environment's name.
 * @param props.isProtected - Whether it is protected.
 * @param props.manages - Whether the person manages it.
 * @returns The panel.
 */
export function RunAsPanel({ environment, isProtected, manages }: RunAsPanelProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const read = useResource(useCallback(() => listDeployments(environment), [environment]));
  const { target: after, arm } = useFocusAfter<HTMLButtonElement>(read.data);
  const [focusOn, setFocusOn] = useState<string | null>(null);
  const [open, setOpen] = useState<Open | null>(null);
  const [note, setNote] = useState("");
  const rows = read.data;
  const changed = (repository: string, sentence: string): void => {
    setOpen(null);
    setNote(sentence);
    setFocusOn(repository);
    arm();
    read.reload();
  };
  const names = (deployment: DeploymentOut): { repository: string; name: string } => ({
    repository: deployment.repository,
    name: environment,
  });
  return (
    <section className="flat-panel settings-panel" aria-labelledby={id}>
      <h2 id={id} ref={heading} tabIndex={-1}>
        {t(`${P}.title`)}
      </h2>
      <p className="page-purpose">{t(`${P}.intro`)}</p>
      <p className="step-status" role="status">
        {note}
      </p>
      {read.error !== null && (
        <ReadFailed
          message={read.error}
          onRetry={() => {
            heading.current?.focus();
            read.reload();
          }}
        />
      )}
      {rows === undefined
        ? read.error === null && <p className="step-status">{t("session.loading")}</p>
        : rows.length === 0 && <p className="empty">{t(`${P}.none`, { name: environment })}</p>}
      {rows !== undefined && rows.length > 0 && (
        <div
          className="list-scroll run-as-scroll"
          role="region"
          aria-label={t(`${P}.region`, { name: environment })}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <table className="list">
            <thead>
              <tr>
                <th scope="col">{t(`${P}.repository`)}</th>
                <th scope="col">{t(`${P}.commit`)}</th>
                <th scope="col">{t(`${P}.runsAs`)}</th>
                {manages && (
                  <th scope="col">
                    <span className="visually-hidden">{t("list.actions")}</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((deployment) => {
                const { repository } = deployment;
                const ref = focusOn === repository ? after : undefined;
                return (
                  <tr key={repository}>
                    <td>
                      <span className="row-name">
                        <VisibleText text={repository} />
                      </span>
                    </td>
                    <td>
                      <code>
                        <VisibleText text={deployment.commit_sha.slice(0, 7)} />
                      </code>
                    </td>
                    <td>
                      {deployment.run_as === null ? (
                        <span className="second">
                          {isProtected ? t(`${P}.unboundProtected`) : t(`${P}.unbound`)}
                        </span>
                      ) : (
                        <VisibleText text={deployment.run_as} />
                      )}
                    </td>
                    {manages && (
                      <td>
                        <div className="button-row">
                          <Button
                            ref={ref}
                            className="button"
                            aria-label={
                              deployment.run_as === null
                                ? t(`${P}.setOf`, { repository })
                                : t(`${P}.changeOf`, { repository })
                            }
                            onPress={() => {
                              setOpen({ kind: "set", deployment });
                            }}
                          >
                            {deployment.run_as === null ? t(`${P}.set`) : t(`${P}.change`)}
                          </Button>
                          {deployment.run_as !== null && (
                            <Button
                              className="button"
                              aria-label={t(`${P}.clearOf`, { repository })}
                              onPress={() => {
                                setOpen({ kind: "clear", deployment });
                              }}
                            >
                              {t(`${P}.clear`)}
                            </Button>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {open?.kind === "set" && (
        <SetRunAsDialog
          environment={environment}
          deployment={open.deployment}
          onClose={() => {
            setOpen(null);
          }}
          onDone={(principal) => {
            changed(
              open.deployment.repository,
              t(`${P}.bound`, { ...names(open.deployment), principal }),
            );
          }}
        />
      )}
      {open?.kind === "clear" && (
        <ConfirmDialog
          title={t(`${P}.clearTitle`, names(open.deployment))}
          confirmLabel={t(`${P}.clear`)}
          onConfirm={async () => {
            await clearRunAs(environment, open.deployment.repository);
          }}
          onCancel={() => {
            setOpen(null);
          }}
          onDone={() => {
            changed(open.deployment.repository, t(`${P}.cleared`, names(open.deployment)));
          }}
        >
          <p>
            {isProtected
              ? t(`${P}.clearProtected`, names(open.deployment))
              : t(`${P}.clearUnprotected`, names(open.deployment))}
          </p>
        </ConfirmDialog>
      )}
    </section>
  );
}
