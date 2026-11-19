/**
 * A repository's page in Settings › Repositories: its URL, kind and default ref, its credential
 * with the host keys it trusts and its deploy keys, and for those who manage repositories Test
 * access.
 *
 * @remarks
 * A reader sees what the server answers them, read-only: the secret's name, the host keys'
 * fingerprints and the public deploy keys of the environments they read. No private key and no
 * secret's value is ever part of an answer.
 * @packageDocumentation
 */
import {
  type ComponentType,
  type JSX,
  lazy,
  type RefObject,
  Suspense,
  useCallback,
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import { getRepositoryPage, type RepositoryOut } from "../api";
import { listEnvironments } from "../api/environments";
import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { Page } from "../shell/Page";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import { credentialText } from "./credentialText";
import { httpsForm, recogniseHost } from "./hosts";
import { mayManageRepositories } from "./places";
import "./repositories.css";
import { REPOSITORIES_PATH } from "./repositoryPaths";
import "./repositoryPageStrings";
import { RepositoryTestAccess } from "./RepositoryTestAccess";
import "./settingsStrings";
import type { RepositoryDialogProps } from "./RepositoryDialogs";
import { useSettingsViewer } from "./viewer";

const P = "settings.repositoryPages";

/**
 * One of the page's dialogs, loaded when it is first opened.
 *
 * @param name - The dialog's export.
 * @returns The lazy dialog.
 */
function dialogOf(
  name: "EditRepositoryDialog" | "MissingKeysDialog" | "DeleteRepositoryDialog",
): ComponentType<RepositoryDialogProps> {
  return lazy(async () => ({ default: (await import("./RepositoryDialogs"))[name] }));
}

const DIALOGS = {
  edit: dialogOf("EditRepositoryDialog"),
  missing: dialogOf("MissingKeysDialog"),
  delete: dialogOf("DeleteRepositoryDialog"),
} as const;

const ReplaceKeyDialog: ComponentType<RepositoryDialogProps & { readonly environment: string }> =
  lazy(async () => ({ default: (await import("./RepositoryDialogs")).ReplaceKeyDialog }));

/** The dialog open on the page. */
type Open =
  | { readonly kind: "edit" | "missing" | "delete" }
  | { readonly kind: "replace"; readonly environment: string };

/** Properties of the page's blocks. */
interface BlockProps {
  /** The repository. */
  readonly repo: RepositoryOut;
}

/**
 * The URL with the way to the host's own page, the kind, the default ref and when it was
 * registered and last changed.
 *
 * @param props - The repository.
 * @param props.repo - The repository.
 * @returns The block.
 */
function Overview({ repo }: BlockProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const web = recogniseHost(repo.url).host === "other" ? null : httpsForm(repo.url);
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.overview`)}</h2>
      <dl className="repository-facts">
        <dt>{t(`${P}.url`)}</dt>
        <dd className="repository-url">
          <VisibleText text={repo.url} />
          {web !== null && (
            <>
              {" "}
              <a href={web} target="_blank" rel="noreferrer">
                {t(`${P}.openHost`)}
                <span aria-hidden="true"> ›</span>
              </a>
            </>
          )}
        </dd>
        <dt>{t(`${P}.kind`)}</dt>
        <dd>{t(`${P}.kinds.${repo.kind}`)}</dd>
        <dt>{t(`${P}.defaultRef`)}</dt>
        <dd>
          {repo.default_ref === null ? (
            t(`${P}.remoteDefault`)
          ) : (
            <VisibleText text={repo.default_ref} />
          )}
        </dd>
        <dt>{t(`${P}.created`)}</dt>
        <dd>{when(repo.created_at)}</dd>
        <dt>{t(`${P}.updated`)}</dt>
        <dd>{when(repo.updated_at)}</dd>
      </dl>
    </section>
  );
}

/** Properties of {@link SshKeys}. */
interface SshKeysProps extends BlockProps {
  /** The Deploy keys heading, focused after a key is replaced. */
  readonly keysHeading: RefObject<HTMLHeadingElement | null>;
  /** The environments whose keys the person may replace or make; none for a reader. */
  readonly keyEnvironments: ReadonlySet<string>;
  /** The environments the person reads, whose keys the page sees. */
  readonly readable: ReadonlySet<string>;
  /** Opens a dialog. */
  readonly onOpen: (open: Open) => void;
}

/**
 * The host keys an SSH repository trusts, by type and fingerprint, and its public deploy keys
 * per environment.
 *
 * @param props - The repository, the heading and the person's environments.
 * @param props.repo - The repository.
 * @param props.keysHeading - The Deploy keys heading.
 * @param props.keyEnvironments - The environments whose keys the person may replace or make.
 * @param props.readable - The environments the person reads, whose keys the page sees.
 * @param props.onOpen - Opens a dialog.
 * @returns The two lists, with Replace key and Make the missing keys where the person may.
 */
function SshKeys({
  repo,
  keysHeading,
  keyEnvironments,
  readable,
  onOpen,
}: SshKeysProps): JSX.Element {
  const { t } = useTranslation();
  const hostKeys = repo.host_keys ?? [];
  const keyed = new Set(repo.deploy_keys.map((key) => key.environment));
  return (
    <>
      <h3>{t(`${P}.hostKeys`)}</h3>
      {hostKeys.length === 0 ? (
        <p>{t(`${P}.noHostKey`)}</p>
      ) : (
        <ul className="repository-keys">
          {hostKeys.map((key) => (
            <li key={`${key.type} ${key.fingerprint}`}>
              <VisibleText text={key.type} />{" "}
              <code>
                <VisibleText text={key.fingerprint} />
              </code>
              {key.marker != null && <> {t(`${P}.markers.${key.marker}`)}</>}
            </li>
          ))}
        </ul>
      )}
      <h3 ref={keysHeading} tabIndex={-1}>
        {t(`${P}.deployKeys`)}
      </h3>
      {repo.deploy_keys.length === 0 ? (
        <p>{t(`${P}.noDeployKey`)}</p>
      ) : (
        <ul className="repository-keys">
          {repo.deploy_keys.map((key) => (
            <li key={key.environment}>
              <EnvironmentLabel environment={key.environment} />{" "}
              <code>
                <VisibleText text={key.fingerprint} />
              </code>
              <CopyLine text={key.public_key} copyLabel={t(`${P}.copyPublicKey`)} />
              {keyEnvironments.has(key.environment) && (
                <Button
                  className="button"
                  aria-label={t(`${P}.replaceKeyOf`, { environment: markedText(key.environment) })}
                  onPress={() => {
                    onOpen({ kind: "replace", environment: key.environment });
                  }}
                >
                  {t(`${P}.replaceKey`)}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {[...keyEnvironments].some(
        (environment) => readable.has(environment) && !keyed.has(environment),
      ) && (
        <Button
          className="button"
          onPress={() => {
            onOpen({ kind: "missing" });
          }}
        >
          {t(`${P}.makeMissing`)}
        </Button>
      )}
    </>
  );
}

/**
 * How the repository authenticates: the secret and username of an HTTPS one, the host keys
 * and deploy keys of an SSH one, nothing for a local folder.
 *
 * @param props - The repository, the heading and the person's environments.
 * @param props.repo - The repository.
 * @param props.keysHeading - The Deploy keys heading.
 * @param props.keyEnvironments - The environments whose keys the person may replace or make.
 * @param props.readable - The environments the person reads, whose keys the page sees.
 * @param props.onOpen - Opens a dialog.
 * @returns The block.
 */
function Credential({
  repo,
  keysHeading,
  keyEnvironments,
  readable,
  onOpen,
}: SshKeysProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.credentialTitle`)}</h2>
      <p>{credentialText(t, repo)}</p>
      {repo.kind === "https" && (
        <dl className="repository-facts">
          <dt>{t(`${P}.secret`)}</dt>
          <dd>
            {repo.credential_secret === null ? (
              t(`${P}.noSecret`)
            ) : (
              <VisibleText text={repo.credential_secret} />
            )}
          </dd>
          <dt>{t(`${P}.username`)}</dt>
          <dd>
            {repo.credential_username === null ? (
              t(`${P}.oauth2`)
            ) : (
              <VisibleText text={repo.credential_username} />
            )}
          </dd>
        </dl>
      )}
      {repo.kind === "ssh" && (
        <SshKeys
          repo={repo}
          keysHeading={keysHeading}
          keyEnvironments={keyEnvironments}
          readable={readable}
          onOpen={onOpen}
        />
      )}
      {repo.kind === "local" && <p>{t(`${P}.localNote`)}</p>}
    </section>
  );
}

/** Properties of {@link OpenDialog}. */
interface OpenDialogProps extends RepositoryDialogProps {
  /** Which dialog. */
  readonly kind: keyof typeof DIALOGS;
}

/**
 * The open dialog but Replace key, which names its environment.
 *
 * @param props - Which dialog, the repository and what to call.
 * @param props.kind - Which dialog.
 * @returns The dialog.
 */
function OpenDialog({ kind, ...props }: OpenDialogProps): JSX.Element {
  const Dialog = DIALOGS[kind];
  return <Dialog {...props} />;
}

/** Properties of {@link RepositoryPage}. */
export interface RepositoryPageProps {
  /** The repository's name, from the address. */
  readonly name: string;
}

/**
 * Renders the repository's page under Settings › Repositories. It is read once, and again after
 * every change made here; an unknown repository says so with the way back to the list.
 *
 * @param props - The repository's name.
 * @param props.name - The repository's name, from the address.
 * @returns The page.
 */
export function RepositoryPage({ name }: RepositoryPageProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const viewer = useSettingsViewer();
  const manages = mayManageRepositories(viewer) === true;
  const heading = useRef<HTMLHeadingElement>(null);
  const load = useCallback(() => getRepositoryPage(name), [name]);
  const read = useResource(load);
  const repo = read.data;
  // The focus moves once the page is read again, after the dialog has given it back.
  const { target: keysHeading, arm: armKeys } = useFocusAfter<HTMLHeadingElement>(repo);
  const { target: editButton, arm: armEdit } = useFocusAfter<HTMLButtonElement>(repo);
  const [open, setOpen] = useState<Open | null>(null);
  const environments = useResource(listEnvironments);
  // Only where the person reads the environment does the page know whether it has a key.
  const readable = new Set(environments.data?.map((environment) => environment.name));
  const keyEnvironments = new Set(manages ? viewer.secretEnvironments : null);
  const close = (): void => {
    setOpen(null);
  };
  const done = (kind: Open["kind"]): void => {
    setOpen(null);
    if (kind === "delete") {
      void navigate(REPOSITORIES_PATH, { replace: true });
      return;
    }
    if (kind === "edit") armEdit();
    else armKeys();
    read.reload();
  };
  return (
    <Page
      title={name}
      headingRef={heading}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: REPOSITORIES_PATH, label: t("settings.places.repositories") },
      ]}
    >
      {read.failure?.status === 404 ? (
        <div className="empty">
          <p role="alert">{read.error}</p>
          <p>
            <Link to={REPOSITORIES_PATH}>{t(`${P}.back`)}</Link>
          </p>
        </div>
      ) : repo === undefined ? (
        read.error === null ? (
          <p className="step-status" role="status">
            {t("session.loading")}
          </p>
        ) : (
          <ReadFailed
            message={read.error}
            onRetry={() => {
              heading.current?.focus();
              read.reload();
            }}
          />
        )
      ) : (
        <>
          <Overview repo={repo} />
          <Credential
            repo={repo}
            keysHeading={keysHeading}
            keyEnvironments={keyEnvironments}
            readable={readable}
            onOpen={setOpen}
          />
          {manages && (
            <>
              {repo.kind !== "local" && (
                <section className="flat-panel settings-panel">
                  <h2>{t(`${P}.testTitle`)}</h2>
                  <RepositoryTestAccess name={repo.name} environments={environments} />
                </section>
              )}
              <div className="step-actions">
                <Button
                  ref={editButton}
                  className="button"
                  aria-label={t(`${P}.editOf`, { name: markedText(repo.name) })}
                  onPress={() => {
                    setOpen({ kind: "edit" });
                  }}
                >
                  {t(`${P}.edit`)}
                </Button>
                <Button
                  className="button"
                  aria-label={t(`${P}.deleteOf`, { name: markedText(repo.name) })}
                  onPress={() => {
                    setOpen({ kind: "delete" });
                  }}
                >
                  {t(`${P}.delete`)}
                </Button>
              </div>
            </>
          )}
          {open !== null && (
            <Suspense fallback={null}>
              {open.kind === "replace" ? (
                <ReplaceKeyDialog
                  repository={repo}
                  environment={open.environment}
                  onDone={() => {
                    done("replace");
                  }}
                  onClose={close}
                />
              ) : (
                <OpenDialog
                  kind={open.kind}
                  repository={repo}
                  onDone={() => {
                    done(open.kind);
                  }}
                  onClose={close}
                />
              )}
            </Suspense>
          )}
        </>
      )}
    </Page>
  );
}
