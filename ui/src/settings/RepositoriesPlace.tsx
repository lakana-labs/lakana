/**
 * Settings › Repositories: the repositories one may read, each opening its page, and for those
 * who manage repositories the way to connect one.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link as RouterLink } from "react-router";
import { Link } from "react-aria-components";

import { listRepositories } from "../api";
import { ViewList } from "../catalog/views/ViewList";
import { CopyLine } from "../shell/CopyLine";
import { useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import { credentialText, lacksCredential } from "./credentialText";
import { mayManageRepositories } from "./places";
import "./repositories.css";
import { repositoryPath } from "./repositoryPaths";
import "./repositoryStrings";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";
import { useSettingsViewer } from "./viewer";

/** Properties of {@link RepositoryList}. */
interface RepositoryListProps {
  /** Whether the person manages repositories. */
  readonly manages: boolean;
  /** The place's heading, focused after Try again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * The repositories in their own scrolling region, or the sentence that there is none.
 *
 * @param props - Whether the person manages repositories, and the heading.
 * @param props.manages - Whether the person manages repositories.
 * @param props.heading - The place's heading.
 * @returns The list.
 */
function RepositoryList({ manages, heading }: RepositoryListProps): JSX.Element {
  const { t } = useTranslation();
  const read = useResource(listRepositories);
  const p = "settings.repositoryPages";
  if (read.data?.length === 0) {
    return manages ? (
      <div className="empty">
        <p>{t(`${p}.noneManager`)}</p>
      </div>
    ) : (
      <div className="empty">
        <p>{t(`${p}.noneReader`)}</p>
        <CopyLine text={t(`${p}.listCommand`)} copyLabel={t(`${p}.copyCommand`)} />
      </div>
    );
  }
  const rows = read.data ?? [];
  return (
    <ViewList
      pages={{
        items: read.data,
        error: read.error,
        busy: read.data === undefined && read.error === null,
        hasMore: false,
        loadMore: () => Promise.resolve(),
        reload: () => {
          heading.current?.focus();
          read.reload();
        },
      }}
      rowCount={rows.length}
      empty=""
      region={t(`${p}.region`)}
      rowsOpen
      scroll="repositories-scroll"
      head={
        <tr>
          <th scope="col">{t("list.name")}</th>
          <th scope="col">{t(`${p}.url`)}</th>
          <th scope="col">{t(`${p}.kind`)}</th>
          <th scope="col">{t(`${p}.credentialHead`)}</th>
          <th scope="col">{t(`${p}.defaultRef`)}</th>
          <th scope="col">
            <span className="visually-hidden">{t("list.open")}</span>
          </th>
        </tr>
      }
    >
      {rows.map((repo) => (
        <tr key={repo.id}>
          <td>
            <RouterLink className="row-name" to={repositoryPath(repo.name)}>
              <VisibleText text={repo.name} />
            </RouterLink>
          </td>
          <td className="repository-url">
            <VisibleText text={repo.url} />
          </td>
          <td>{t(`${p}.kinds.${repo.kind}`)}</td>
          <td className={lacksCredential(repo) ? "repository-uncredentialed" : undefined}>
            {credentialText(t, repo)}
          </td>
          <td>
            {repo.default_ref === null ? (
              t(`${p}.remoteDefault`)
            ) : (
              <VisibleText text={repo.default_ref} />
            )}
          </td>
          <td>
            <span className="chevron" aria-hidden="true">
              ›
            </span>
          </td>
        </tr>
      ))}
    </ViewList>
  );
}

/**
 * Renders the purpose, the list, and for those who manage repositories the way to Connect a
 * repository.
 *
 * @returns The place.
 */
export function RepositoriesPlace(): JSX.Element {
  const { t } = useTranslation();
  const manages = mayManageRepositories(useSettingsViewer()) === true;
  const heading = useRef<HTMLHeadingElement>(null);
  return (
    <SettingsFrame
      title={t("settings.places.repositories")}
      headingRef={heading}
      action={
        manages && (
          <Link className="button button-primary" href="/settings/repositories/new">
            {t("settings.repositories.connect")}
          </Link>
        )
      }
    >
      <p className="page-purpose">{t("settings.repositories.intro")}</p>
      <RepositoryList manages={manages} heading={heading} />
    </SettingsFrame>
  );
}
