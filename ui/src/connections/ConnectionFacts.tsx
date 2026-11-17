/**
 * What a connection's page says of it: its kind, where it points, how it is reached, its secret's
 * name, who made it, and the assets that use it.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ConnectionOut } from "../api";
import { markedText, VisibleText } from "../shell/VisibleText";
import { useWhen } from "../shell/useWhen";
import words from "./connections.en.json";
import "./connectionStrings";

/** Properties of {@link Fact}. */
interface FactProps {
  /** The fact's name. */
  readonly term: ReactNode;
  /** What it says. */
  readonly children: ReactNode;
}

/**
 * One fact: its name and what it says.
 *
 * @param props - The name and the answer.
 * @param props.term - The fact's name.
 * @param props.children - What it says.
 * @returns The fact.
 */
function Fact({ term, children }: FactProps): JSX.Element {
  return (
    <div className="connection-fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * The fingerprints a `ca_certificate` setting holds: the server answers each as `sha256:…`,
 * one string or a list.
 *
 * @param value - The setting's value.
 * @returns The fingerprints, as text.
 */
function fingerprints(value: unknown): readonly string[] {
  return (Array.isArray(value) ? (value as unknown[]) : [value]).map(String);
}

/**
 * A setting's value as text.
 *
 * @param value - The value.
 * @returns The value, or its JSON when it is not a string or a number.
 */
function valueText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? String(value)
    : JSON.stringify(value);
}

/** Properties of {@link Setting}. */
interface SettingProps {
  /** The setting's key, as the server names it. */
  readonly name: string;
  /** Its value. */
  readonly value: unknown;
}

/**
 * One address part or setting. `tls` and `auth` are said in words when the value is known, and
 * a value this build does not know is shown as itself, marked. The CA certificate is its
 * fingerprints, which may break anywhere a long one meets the edge.
 *
 * @param props - The key and its value.
 * @param props.name - The key.
 * @param props.value - The value.
 * @returns The fact.
 */
function Setting({ name, value }: SettingProps): JSX.Element {
  const { t } = useTranslation();
  if (name === "ca_certificate") {
    return (
      <Fact term={t("connections.page.caCertificate")}>
        {fingerprints(value).map((print) => (
          <span key={print} className="connection-print">
            <VisibleText text={print} />
          </span>
        ))}
      </Fact>
    );
  }
  const group = name === "tls" ? words.tls : name === "auth" ? words.auth : null;
  const known = group !== null && typeof value === "string" && Object.hasOwn(group, value);
  return (
    <Fact term={<VisibleText text={name} />}>
      {known ? t(`connections.${name}.${value}`) : <VisibleText text={valueText(value)} />}
    </Fact>
  );
}

/** Properties of {@link ConnectionFacts}. */
export interface ConnectionFactsProps {
  /** The connection. */
  readonly connection: ConnectionOut;
}

/**
 * Renders two panels: the connection's facts, and the assets that declare it. The assets have
 * no page to open, so each is plain text in the text colour, never a link.
 *
 * @param props - The connection.
 * @param props.connection - The connection.
 * @returns The panels.
 */
export function ConnectionFacts({ connection }: ConnectionFactsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const { kind, address, settings, secret, declared_by: declaredBy } = connection;
  return (
    <>
      <section className="flat-panel" aria-labelledby="connection-facts">
        <h2 className="connection-heading" id="connection-facts">
          {t("connections.page.factsHeading")}
        </h2>
        <dl className="connection-facts">
          <Fact term={t("connections.page.kind")}>
            {t(`connections.kinds.${kind}.name`)}
            <span className="second">{t(`connections.kinds.${kind}.line`)}</span>
          </Fact>
          {Object.entries(address).map(([key, value]) => (
            <Setting key={`address ${key}`} name={key} value={value} />
          ))}
          {Object.entries(settings).map(([key, value]) => (
            <Setting key={`settings ${key}`} name={key} value={value} />
          ))}
          <Fact term={t("connections.page.secret")}>
            {secret === null
              ? t("connections.page.secretNone")
              : t("connections.page.secretNamed", { secret: markedText(secret) })}
          </Fact>
          <Fact term={t("connections.page.created")}>
            {t("connections.page.createdBy", {
              who: markedText(connection.created_by),
              when: when(connection.created_at),
            })}
          </Fact>
          <Fact term={t("connections.page.updated")}>{when(connection.updated_at)}</Fact>
        </dl>
      </section>
      <section className="flat-panel" aria-labelledby="connection-used-by">
        <h2 className="connection-heading" id="connection-used-by">
          {t("connections.page.usedByHeading")}
        </h2>
        {declaredBy.length === 0 ? (
          <p>{t("connections.page.notDeclared")}</p>
        ) : (
          <ul className="connection-list">
            {declaredBy.map(({ repository, asset }) => (
              <li key={`${repository}\u0000${asset}`}>
                <span className="row-name">
                  {t("connections.page.declared", {
                    repository: markedText(repository),
                    asset: markedText(asset),
                  })}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
