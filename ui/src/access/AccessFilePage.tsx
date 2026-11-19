/**
 * Settings › Access › Access file: the scopes access files manage, the changes made by hand
 * since their last apply, the commands that plan and apply a file, and Download access file.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { lazy, Suspense, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";

import type { AccessOverrideOut, ManagedScopeOut } from "../api/accessFile";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusGlyph } from "../status/StatusGlyph";
import "./access.css";
import "./accessFile.css";
import "./accessFileStrings";
import "./accessStrings";
import { INSTANCE_SCOPE, managedScopeOf } from "./managed";
import { useAccessStatus } from "./useAccessStatus";

// Download access file's form loads when it is opened, not with the page.
const DownloadAccessFileDialog = lazy(() =>
  import("./DownloadAccessFileDialog").then((module) => ({
    default: module.DownloadAccessFileDialog,
  })),
);

/** The file the commands name when nothing names one. */
const DEFAULT_FILE = "access.yaml";

/** How many characters of a hash or a commit a row shows. */
const SHORT = 12;

/**
 * Whether a managed scope is as its last apply left it, or was changed by hand since: in a
 * managed scope the only drift is an open override, so its open overrides are counted.
 *
 * @param scope - The managed scope.
 * @param overrides - Every open override.
 * @returns The glyph's kind and the number of the scope's open overrides.
 */
export function scopeState(
  scope: ManagedScopeOut,
  overrides: readonly AccessOverrideOut[],
): { kind: "succeeded" | "warning"; count: number } {
  const count = overrides.filter((override) => override.scope === scope.scope).length;
  return { kind: count === 0 ? "succeeded" : "warning", count };
}

/**
 * A scope by name: the whole instance, or the environment's label.
 *
 * @param props - The scope.
 * @param props.scope - `instance` or an environment's name.
 * @returns The scope's name.
 */
function ScopeName({ scope }: { readonly scope: string }): JSX.Element {
  const { t } = useTranslation();
  return scope === INSTANCE_SCOPE ? (
    <>{t("accessFile.scopes.instance")}</>
  ) : (
    <EnvironmentLabel environment={scope} />
  );
}

/**
 * A table in its own scrolling region, reachable by keyboard.
 *
 * @param props - The region's name, the header row and the rows.
 * @param props.region - The region's name.
 * @param props.head - The header row.
 * @param props.children - The rows.
 * @returns The region.
 */
function ScrollTable({
  region,
  head,
  children,
}: {
  readonly region: string;
  readonly head: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div
      className="list-scroll access-file-scroll"
      role="region"
      aria-label={region}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list list-static">
        <thead>{head}</thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/**
 * Renders the place: the managed scopes (each with its file, its last apply and whether it was
 * changed by hand since), the open changes by hand, and the commands. Read once, not polled.
 *
 * @returns The place.
 */
export function AccessFilePage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const status = useAccessStatus();
  const heading = useRef<HTMLHeadingElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [note, setNote] = useState("");
  const scopes = status.data?.scopes ?? [];
  const overrides = status.data?.overrides ?? [];
  const file = (managedScopeOf(scopes, null) ?? scopes[0])?.file_name ?? DEFAULT_FILE;
  return (
    <SettingsFrame
      title={t("settings.places.accessFile")}
      headingRef={heading}
      action={
        <Button
          className="button button-primary"
          onPress={() => {
            setDownloading(true);
          }}
        >
          {t("accessFile.download.action")}
        </Button>
      }
    >
      <p className="page-purpose">{t("accessFile.intro")}</p>
      <p className="step-status" role="status">
        {note}
      </p>
      {status.error !== null && (
        <ReadFailed
          message={status.error}
          onRetry={() => {
            heading.current?.focus();
            status.reload();
          }}
        />
      )}
      {status.data === undefined && status.error === null && (
        <p className="step-status">{t("accessFile.reading")}</p>
      )}
      {status.data !== undefined && (
        <section className="flat-panel access-section">
          <h2>{t("accessFile.scopes.title")}</h2>
          {scopes.length === 0 ? (
            <p>{t("accessFile.scopes.none")}</p>
          ) : (
            <ScrollTable
              region={t("accessFile.scopes.title")}
              head={
                <tr>
                  <th scope="col">{t("accessFile.scopes.scope")}</th>
                  <th scope="col">{t("accessFile.scopes.file")}</th>
                  <th scope="col">{t("accessFile.scopes.applied")}</th>
                  <th scope="col">{t("accessFile.scopes.state")}</th>
                </tr>
              }
            >
              {scopes.map((scope) => {
                const state = scopeState(scope, overrides);
                return (
                  <tr key={scope.scope}>
                    <td>
                      <ScopeName scope={scope.scope} />
                    </td>
                    <td>
                      <span className="row-name">
                        <VisibleText text={scope.file_name} />
                      </span>
                      <span className="second">
                        {t("accessFile.scopes.sha", { sha: scope.file_sha256.slice(0, SHORT) })}
                        {scope.source_commit !== null &&
                          t("accessFile.scopes.commit", {
                            commit: scope.source_commit.slice(0, SHORT),
                          })}
                      </span>
                    </td>
                    <td>
                      <time dateTime={scope.applied_at}>{when(scope.applied_at)}</time>
                      <span className="second">
                        {scope.applied_by === null ? (
                          t("accessFile.scopes.byDeleted")
                        ) : (
                          <Trans
                            t={t}
                            i18nKey="accessFile.scopes.by"
                            components={{ name: <VisibleText text={scope.applied_by} /> }}
                          />
                        )}
                      </span>
                    </td>
                    <td>
                      <span className="status-label">
                        <StatusGlyph kind={state.kind} />
                        <span>
                          {state.count === 0
                            ? t("accessFile.scopes.inSync")
                            : t("accessFile.scopes.changed", { count: state.count })}
                        </span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </ScrollTable>
          )}
        </section>
      )}
      {overrides.length > 0 && (
        <section className="flat-panel access-section">
          <h2>{t("accessFile.changes.title")}</h2>
          <ScrollTable
            region={t("accessFile.changes.region")}
            head={
              <tr>
                <th scope="col">{t("accessFile.changes.scope")}</th>
                <th scope="col">{t("accessFile.changes.change")}</th>
                <th scope="col">{t("accessFile.changes.why")}</th>
                <th scope="col">{t("accessFile.changes.by")}</th>
                <th scope="col">{t("accessFile.changes.when")}</th>
              </tr>
            }
          >
            {overrides.map((override, index) => (
              // An override has no id of its own; the list is read once and never reordered.
              <tr key={index}>
                <td>
                  <ScopeName scope={override.scope} />
                </td>
                <td>
                  <VisibleText text={override.change} breakable />
                </td>
                <td>
                  <VisibleText text={override.reason} />
                </td>
                <td>
                  {override.by === null ? (
                    t("accessFile.changes.deleted")
                  ) : (
                    <VisibleText text={override.by} />
                  )}
                </td>
                <td>
                  <time dateTime={override.at}>{when(override.at)}</time>
                </td>
              </tr>
            ))}
          </ScrollTable>
          <p>{t("accessFile.changes.revert")}</p>
        </section>
      )}
      <section className="flat-panel access-section">
        <h2>{t("accessFile.plan.title")}</h2>
        <p>
          <Trans t={t} i18nKey="accessFile.plan.text" components={{ code: <code /> }} />
        </p>
        {(["planCommand", "applyCommand", "exportCommand"] as const).map((command) => (
          <CopyLine
            key={command}
            text={t(`accessFile.plan.${command}`, { file })}
            copyLabel={t("accessFile.plan.copy")}
          />
        ))}
      </section>
      {downloading && (
        <Suspense fallback={null}>
          <DownloadAccessFileDialog
            scopes={scopes}
            onClose={() => {
              setDownloading(false);
            }}
            onDone={(name) => {
              setDownloading(false);
              setNote(t("accessFile.download.done", { name }));
            }}
          />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
