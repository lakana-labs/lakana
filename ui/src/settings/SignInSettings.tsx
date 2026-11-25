/**
 * Settings → Sign-in: how long a browser session lasts, edited in place in its own section, and
 * with an identity provider its "Single sign-on" section and the token window.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useId, useState } from "react";
import { Form, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getSignInSettings, type SignInSettingsOut } from "../api";
import type { SignInProviderOut } from "../api/settings";
import { saveSignInSettings } from "../api/signInAdmin";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { useAttempt } from "../sign-in/Attempt";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";
import "./signInStrings";
import { PendingButton } from "../shell/PendingButton";
import { VisibleText } from "../shell/VisibleText";
import { StatusGlyph } from "../status/StatusGlyph";

const HOUR = 60;
const DAY = 24 * HOUR;

/**
 * A number of minutes in the largest whole unit up to days: "15 minutes", "24 hours", "30 days".
 *
 * @param t - The translation function.
 * @param minutes - The length.
 * @returns The words.
 */
function duration(t: TFunction, minutes: number): string {
  if (minutes > DAY && minutes % DAY === 0) {
    return t("duration.days", { count: minutes / DAY });
  }
  if (minutes % HOUR === 0) {
    return t("duration.hours", { count: minutes / HOUR });
  }
  return t("duration.minutes", { count: minutes });
}

/**
 * A field's text as a whole number of minutes.
 *
 * @param text - What the person typed.
 * @returns The number, or null when it is not a whole number.
 */
function whole(text: string): number | null {
  return /^\d+$/.test(text.trim()) ? Number(text) : null;
}

/**
 * Whether a value is a whole number between two bounds.
 *
 * @param value - The value, or null.
 * @param min - The lowest allowed.
 * @param max - The highest allowed.
 * @returns True within the bounds.
 */
function within(value: number | null, min: number, max: number): value is number {
  return value !== null && value >= min && value <= max;
}

/** Properties of {@link LengthsForm}. */
interface LengthsFormProps {
  /** The settings as read. */
  readonly current: SignInSettingsOut;
}

/**
 * The two lengths, with an identity provider the token window, and Save. A value outside its bounds is refused before anything is sent, with
 * the bounds named; Save asks for the passkey touch first when the session's is stale, and a
 * cancelled touch saves nothing and keeps what was typed.
 *
 * @param props - The settings as read.
 * @param props.current - The settings.
 * @returns The form.
 */
function LengthsForm({ current }: LengthsFormProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const bounds = current.bounds;
  const [idle, setIdle] = useState(String(current.idle_timeout_minutes));
  const [absolute, setAbsolute] = useState(String(current.absolute_lifetime_minutes));
  // The token window is only for an instance with an identity provider; otherwise it is not sent.
  const windowed = current.provider != null;
  const [days, setDays] = useState(String(current.token_provider_window_days ?? 30));
  const windowBounds = {
    min: bounds.token_provider_window_days_min ?? 1,
    max: bounds.token_provider_window_days_max ?? 365,
  };
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const idleBounds = { min: bounds.idle_timeout_minutes_min, max: bounds.idle_timeout_minutes_max };
  const absoluteBounds = {
    min: bounds.absolute_lifetime_minutes_min,
    max: bounds.absolute_lifetime_minutes_max,
  };

  const check = (
    idleMinutes: number | null,
    absoluteMinutes: number | null,
    windowDays: number | null,
  ): string | null => {
    if (windowed && !within(windowDays, windowBounds.min, windowBounds.max)) {
      return t("settings.signIn.window.outOfBounds", windowBounds);
    }
    if (!within(idleMinutes, idleBounds.min, idleBounds.max)) {
      return t("settings.signIn.idleOutOfBounds", idleBounds);
    }
    if (!within(absoluteMinutes, absoluteBounds.min, absoluteBounds.max)) {
      return t("settings.signIn.absoluteOutOfBounds", absoluteBounds);
    }
    return absoluteMinutes < idleMinutes
      ? t("settings.signIn.shorterThanIdle", { idle: idleMinutes })
      : null;
  };

  const error = problem ?? attempt.error;
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        setSaved(false);
        const idleMinutes = whole(idle);
        const absoluteMinutes = whole(absolute);
        const windowDays = whole(days);
        const refused = check(idleMinutes, absoluteMinutes, windowDays);
        setProblem(refused);
        if (refused !== null) {
          return;
        }
        attempt.run(async () => {
          const changed = await saveSignInSettings({
            idle_timeout_minutes: idleMinutes as number,
            absolute_lifetime_minutes: absoluteMinutes as number,
            ...(windowed ? { token_provider_window_days: windowDays as number } : {}),
          });
          setIdle(String(changed.idle_timeout_minutes));
          setAbsolute(String(changed.absolute_lifetime_minutes));
          setDays(String(changed.token_provider_window_days ?? windowDays));
          setSaved(true);
        });
      }}
    >
      <TextField className="field" value={idle} onChange={setIdle}>
        <Label>{t("settings.signIn.idle")}</Label>
        <Input inputMode="numeric" />
        <Text slot="description">
          {t("settings.signIn.idleHelp", {
            ...idleBounds,
            minText: duration(t, idleBounds.min),
            maxText: duration(t, idleBounds.max),
          })}
        </Text>
      </TextField>
      <TextField className="field" value={absolute} onChange={setAbsolute}>
        <Label>{t("settings.signIn.absolute")}</Label>
        <Input inputMode="numeric" />
        <Text slot="description">
          {t("settings.signIn.absoluteHelp", {
            ...absoluteBounds,
            minText: duration(t, absoluteBounds.min),
            maxText: duration(t, absoluteBounds.max),
          })}
        </Text>
      </TextField>
      {windowed && (
        <TextField className="field" value={days} onChange={setDays}>
          <Label>{t("settings.signIn.window.label")}</Label>
          <Input inputMode="numeric" />
          <Text slot="description">{t("settings.signIn.window.help", windowBounds)}</Text>
        </TextField>
      )}
      <p>{t("settings.signIn.appliesLater")}</p>
      <div className="step-actions">
        <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
          {t("settings.signIn.save")}
        </PendingButton>
      </div>
      <p className="step-status" role="status">
        {saved ? t("settings.signIn.saved") : ""}
      </p>
      {error !== null && (
        <p className="step-error" role="alert">
          {error}
          {problem === null && attempt.addPasskey && (
            <>
              {" "}
              <Link to="/account">{t("passkey.add")}</Link>
            </>
          )}
        </p>
      )}
    </Form>
  );
}

/** Properties of {@link SingleSignOn}. */
interface SingleSignOnProps {
  /** The identity provider and its counts. */
  readonly provider: SignInProviderOut;
}

/**
 * The identity provider, read-only: its name, issuer and counts, with a warning (the word and
 * the glyph) when one unlinked administrator or none is left.
 *
 * @param props - The provider.
 * @param props.provider - The identity provider and its counts.
 * @returns The section.
 */
function SingleSignOn({ provider }: SingleSignOnProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const left = provider.unlinked_admins;
  return (
    <section className="flat-panel settings-panel" aria-labelledby={heading}>
      <h2 id={heading}>{t("settings.signIn.provider.title")}</h2>
      <p className="page-purpose">{t("settings.signIn.provider.intro")}</p>
      <dl className="fact-list">
        <dt>{t("settings.signIn.provider.name")}</dt>
        <dd>
          <VisibleText text={provider.display_name} />
        </dd>
        <dt>{t("settings.signIn.provider.issuer")}</dt>
        <dd>
          <code className="sso-issuer">
            <VisibleText text={provider.issuer} breakable />
          </code>
        </dd>
        <dt>{t("settings.signIn.provider.linked")}</dt>
        <dd className="number">{provider.linked_people}</dd>
        <dt>{t("settings.signIn.provider.unlinked")}</dt>
        <dd className="number">{left}</dd>
      </dl>
      {left <= 1 && (
        <p className="step-warning sso-warning">
          <StatusGlyph kind="warning" />
          <span>
            <span className="sso-warning-word">{t("settings.signIn.provider.warningWord")}</span>{" "}
            {left === 1
              ? t("settings.signIn.provider.oneLeft")
              : t("settings.signIn.provider.noneLeft")}
          </span>
        </p>
      )}
    </section>
  );
}

/**
 * Renders Settings → Sign-in: the purpose, the identity provider's section when there is one,
 * then the session lengths (and the token window) in a section of their own.
 *
 * @returns The place.
 */
export function SignInSettings(): JSX.Element {
  const { t } = useTranslation();
  const settings = useResource(getSignInSettings);
  const lengthsHeading = useId();
  return (
    <SettingsFrame title={t("settings.places.signIn")}>
      <p className="page-purpose">{t("settings.signIn.intro")}</p>
      {settings.data?.provider != null && <SingleSignOn provider={settings.data.provider} />}
      <section className="flat-panel settings-panel" aria-labelledby={lengthsHeading}>
        <h2 id={lengthsHeading}>{t("settings.signIn.lengths")}</h2>
        <Loaded resource={settings}>{(current) => <LengthsForm current={current} />}</Loaded>
      </section>
    </SettingsFrame>
  );
}
