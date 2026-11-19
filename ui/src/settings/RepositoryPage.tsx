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
import { type JSX, type RefObject, useCallback, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getRepositoryPage, type RepositoryOut } from "../api";
import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { Page } from "../shell/Page";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { credentialText } from "./credentialText";
import { httpsForm, recogniseHost } from "./hosts";
import { mayManageRepositories } from "./places";
import "./repositories.css";
import { REPOSITORIES_PATH } from "./repositoryPaths";
import "./repositoryStrings";
import { RepositoryTestAccess } from "./RepositoryTestAccess";
import "./settingsStrings";
import { useSettingsViewer } from "./viewer";

const P = "settings.repositoryPages";

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
}

/**
 * The host keys an SSH repository trusts, by type and fingerprint, and its public deploy keys
 * per environment.
 *
 * @param props - The repository and the heading.
 * @param props.repo - The repository.
 * @param props.keysHeading - The Deploy keys heading.
 * @returns The two lists.
 */
function SshKeys({ repo, keysHeading }: SshKeysProps): JSX.Element {
  const { t } = useTranslation();
  const hostKeys = repo.host_keys ?? [];
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
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * How the repository authenticates: the secret and username of an HTTPS one, the host keys
 * and deploy keys of an SSH one, nothing for a local folder.
 *
 * @param props - The repository and the heading.
 * @param props.repo - The repository.
 * @param props.keysHeading - The Deploy keys heading.
 * @returns The block.
 */
function Credential({ repo, keysHeading }: SshKeysProps): JSX.Element {
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
      {repo.kind === "ssh" && <SshKeys repo={repo} keysHeading={keysHeading} />}
      {repo.kind === "local" && <p>{t(`${P}.localNote`)}</p>}
    </section>
  );
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
  const manages = mayManageRepositories(useSettingsViewer()) === true;
  const heading = useRef<HTMLHeadingElement>(null);
  const keysHeading = useRef<HTMLHeadingElement>(null);
  const load = useCallback(() => getRepositoryPage(name), [name]);
  const read = useResource(load);
  const repo = read.data;
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
          <Credential repo={repo} keysHeading={keysHeading} />
          {manages && (
            <section className="flat-panel settings-panel">
              <h2>{t(`${P}.testTitle`)}</h2>
              <RepositoryTestAccess name={repo.name} />
            </section>
          )}
        </>
      )}
    </Page>
  );
}
