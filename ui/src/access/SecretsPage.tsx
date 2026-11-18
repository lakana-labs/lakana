/**
 * Settings › Access › Secrets: an environment's secrets, their names and uses, never values.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { listSecrets } from "../api/secrets";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { useSettingsViewer } from "../settings/viewer";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { readRemembered, writeRemembered } from "../shell/remembered";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./secrets.css";
import { SECRETS_ENVIRONMENT_KEY, secretUseText } from "./secretUse";
import "./secretStrings";

/** Properties of {@link SecretList}. */
interface SecretListProps {
  /** The environment whose secrets are listed. */
  readonly environment: string;
  /** The page's heading, which takes the focus once a failed list has been read again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * The environment's secrets in their own scrolling region: each name whole, its use, when it
 * was created and last rotated. A secret has no page, so the rows open nothing.
 *
 * @param props - The environment and the page's heading.
 * @param props.environment - The environment.
 * @param props.heading - The page's heading.
 * @returns The list.
 */
function SecretList({ environment, heading }: SecretListProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback(() => listSecrets(environment), [environment]);
  const secrets = useResource(load);
  // Try again unmounts with the focus on it: the focus goes to the heading once the list is in.
  const retried = useRef(false);
  useEffect(() => {
    if (retried.current && secrets.data !== undefined) {
      retried.current = false;
      heading.current?.focus();
    }
  }, [heading, secrets.data]);
  if (secrets.data === undefined) {
    return secrets.error === null ? (
      <p className="step-status" role="status">
        {t("secrets.reading")}
      </p>
    ) : (
      <ReadFailed
        message={secrets.error}
        onRetry={() => {
          retried.current = true;
          secrets.reload();
        }}
      />
    );
  }
  if (secrets.data.length === 0) {
    return (
      <div className="empty">
        <p>{t("secrets.none", { environment })}</p>
        <p>{t("secrets.cli")}</p>
        <CopyLine text={t("secrets.createCommand")} copyLabel={t("secrets.copyCommand")} />
      </div>
    );
  }
  return (
    <div
      className="list-scroll secrets-scroll"
      role="region"
      aria-label={t("secrets.region", { environment })}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list list-static">
        <thead>
          <tr>
            <th scope="col">{t("secrets.name")}</th>
            <th scope="col">{t("secrets.use")}</th>
            <th scope="col">{t("secrets.created")}</th>
            <th scope="col">{t("secrets.rotated")}</th>
          </tr>
        </thead>
        <tbody>
          {secrets.data.map((secret) => (
            <tr key={secret.id}>
              <td>
                <span className="row-name">
                  <VisibleText text={secret.name} />
                </span>
              </td>
              <td className={secret.use === null ? "secret-unused" : undefined}>
                {secretUseText(t, secret)}
              </td>
              <td>{when(secret.created_at)}</td>
              <td>{secret.rotated_at === null ? t("secrets.notYet") : when(secret.rotated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Renders the place: the environment (one of those where the person manages secrets, with each
 * one's protected mark where the environments can be read), then its secrets. The environment
 * is the address's `?environment=` when it is one of them, else the one remembered, else the
 * first; a choice writes both. An address naming an environment the person cannot manage says
 * so above the list and reads nothing of it.
 *
 * @returns The place.
 */
export function SecretsPage(): JSX.Element {
  const { t } = useTranslation();
  const viewer = useSettingsViewer();
  const [params, setParams] = useSearchParams();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const heading = useRef<HTMLHeadingElement>(null);
  // The page is shown only once the viewer names at least one environment.
  const allowed = viewer.secretEnvironments ?? [];
  const linked = params.get("environment");
  const remembered = readRemembered(SECRETS_ENVIRONMENT_KEY);
  const chosen =
    [linked, remembered].find(
      (candidate): candidate is string => candidate !== null && allowed.includes(candidate),
    ) ??
    allowed[0] ??
    "";
  const choices = allowed.map((name) => ({
    name,
    protected:
      environments.data?.find((environment) => environment.name === name)?.protected ?? false,
  }));
  return (
    <SettingsFrame title={t("settings.places.secrets")} headingRef={heading}>
      <EnvironmentPicker
        environments={choices}
        value={chosen}
        label={t("secrets.environment")}
        onChange={(next) => {
          writeRemembered(SECRETS_ENVIRONMENT_KEY, next);
          setParams({ environment: next }, { replace: true });
        }}
      />
      {linked !== null && !allowed.includes(linked) && (
        <p className="page-notice">{t("secrets.refused", { environment: linked })}</p>
      )}
      <SecretList key={chosen} environment={chosen} heading={heading} />
    </SettingsFrame>
  );
}
