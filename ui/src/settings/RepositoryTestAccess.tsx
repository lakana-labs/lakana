/**
 * Test access on a repository's page: a worker reads the repository with one environment's
 * credential, and the page says what it found, with the cause and the next step.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import { type EnvironmentChoice, listEnvironments } from "../api/environments";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { Loaded } from "../shell/Loaded";
import { PendingButton } from "../shell/PendingButton";
import { readRemembered, writeRemembered } from "../shell/remembered";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { CheckOutcome, useCheck } from "./repositoryCheck";
import "./repositoryStrings";

/** Where the last environment chosen for Test access is remembered. */
const TEST_ENVIRONMENT_KEY = "lakana.repositories.testEnvironment";

/** Properties of {@link RepositoryTestAccess}. */
export interface RepositoryTestAccessProps {
  /** The repository's name. */
  readonly name: string;
}

/** Properties of {@link TestWith}. */
interface TestWithProps {
  /** The repository's name. */
  readonly name: string;
  /** The environments to test with. */
  readonly choices: readonly EnvironmentChoice[];
}

/**
 * The picker, Test, and what the worker found; or the sentence that there is no environment.
 *
 * @param props - The repository and the environments.
 * @param props.name - The repository's name.
 * @param props.choices - The environments to test with.
 * @returns The block.
 */
function TestWith({ name, choices }: TestWithProps): JSX.Element {
  const { t } = useTranslation();
  const [picked, setPicked] = useState(() => readRemembered(TEST_ENVIRONMENT_KEY));
  const check = useCheck();
  const p = "settings.repositoryPages";
  const first = choices[0];
  if (first === undefined) {
    return <p>{t(`${p}.testNoEnvironment`)}</p>;
  }
  const chosen = choices.find((choice) => choice.name === picked)?.name ?? first.name;
  const ok = check.probe?.outcome === "ok" ? check.probe : null;
  return (
    <>
      <p>{t(`${p}.testIntro`)}</p>
      <EnvironmentPicker
        environments={choices}
        value={chosen}
        label={t(`${p}.testEnvironment`)}
        onChange={(next) => {
          writeRemembered(TEST_ENVIRONMENT_KEY, next);
          setPicked(next);
          check.reset();
        }}
      />
      <PendingButton
        className="button"
        pending={check.busy}
        aria-label={t(`${p}.testOf`, { name: markedText(name) })}
        onPress={() => {
          check.run({ kind: "access", repository: name, environment: chosen });
        }}
      >
        {t(`${p}.test`)}
      </PendingButton>
      <CheckOutcome
        check={check}
        waiting={t(`${p}.testWaiting`)}
        done={markedText(ok?.message ?? "")}
      />
    </>
  );
}

/**
 * Test access, once the environments are read. Leaving the page stops the wait.
 *
 * @param props - The repository.
 * @param props.name - The repository's name.
 * @returns The block.
 */
export function RepositoryTestAccess({ name }: RepositoryTestAccessProps): JSX.Element {
  const environments = useResource(listEnvironments);
  return (
    <Loaded resource={environments}>
      {(choices) => <TestWith name={name} choices={choices} />}
    </Loaded>
  );
}
