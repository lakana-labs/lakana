/**
 * Settings → Sign-in: how long a browser session lasts, edited in place.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useState } from "react";
import { Button, Form, Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { changeSignInSettings, getSignInSettings, type SignInSettingsOut } from "../api";
import { Loaded } from "../shell/Loaded";
import { Page } from "../shell/Page";
import { SectionTabs } from "../shell/SectionTabs";
import { useResource } from "../shell/useResource";
import { useAttempt } from "../sign-in/Attempt";

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
 * The two lengths and Save. A value outside its bounds is refused before anything is sent, with
 * the bounds named.
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
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const idleBounds = { min: bounds.idle_timeout_minutes_min, max: bounds.idle_timeout_minutes_max };
  const absoluteBounds = {
    min: bounds.absolute_lifetime_minutes_min,
    max: bounds.absolute_lifetime_minutes_max,
  };

  const check = (idleMinutes: number | null, absoluteMinutes: number | null): string | null => {
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
        const refused = check(idleMinutes, absoluteMinutes);
        setProblem(refused);
        if (refused !== null) {
          return;
        }
        attempt.run(async () => {
          const changed = await changeSignInSettings({
            idle_timeout_minutes: idleMinutes as number,
            absolute_lifetime_minutes: absoluteMinutes as number,
          });
          setIdle(String(changed.idle_timeout_minutes));
          setAbsolute(String(changed.absolute_lifetime_minutes));
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
      <p>{t("settings.signIn.appliesLater")}</p>
      <div className="step-actions">
        <Button type="submit" className="button button-primary" isDisabled={attempt.busy}>
          {t("settings.signIn.save")}
        </Button>
      </div>
      <p className="step-status" role="status">
        {saved ? t("settings.signIn.saved") : ""}
      </p>
      {error !== null && (
        <p className="step-error" role="alert">
          {error}
        </p>
      )}
    </Form>
  );
}

/**
 * Renders Settings with its Sign-in tab.
 *
 * @returns The place.
 */
export function SignInSettings(): JSX.Element {
  const { t } = useTranslation();
  const settings = useResource(getSignInSettings);
  return (
    <Page title={t("areas.settings.name")}>
      <SectionTabs
        label={t("settings.tabs")}
        tabs={[{ to: "/settings/sign-in", label: t("settings.signIn.tab") }]}
      />
      <p className="page-purpose">{t("settings.signIn.intro")}</p>
      <Loaded resource={settings}>{(current) => <LengthsForm current={current} />}</Loaded>
    </Page>
  );
}
