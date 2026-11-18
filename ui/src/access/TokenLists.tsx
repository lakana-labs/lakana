/**
 * The two lists of Settings › Access › Tokens: everyone's live personal tokens and the live task
 * tokens, each a page at a time, each token revocable.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { type TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  listAllTokens,
  listTaskTokens,
  revokeTaskToken,
  revokeToken,
  type TaskTokenOut,
  type TokenOut,
} from "../api/tokens";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { jobRunPath } from "../jobs/paths";
import { Avatar } from "../profile/Avatar";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import { personPath } from "./personPath";
import "./tokens.css";
import "./tokenStrings";

/**
 * Moves the focus to a heading once the list has been read again after a revoke.
 *
 * @param heading - The section's heading.
 * @param items - The list's rows; a new value means the read is in.
 * @returns Arms the move, before the list is read again.
 */
function useFocusOnRead(heading: RefObject<HTMLHeadingElement | null>, items: unknown): () => void {
  const armed = useRef(false);
  useEffect(() => {
    if (armed.current) {
      armed.current = false;
      heading.current?.focus();
    }
  }, [heading, items]);
  return useCallback(() => {
    armed.current = true;
  }, []);
}

/** Properties of {@link PersonalTokens}. */
export interface PersonalTokensProps {
  /** Only this principal's tokens; null for everyone's. */
  readonly owner: string | null;
  /** The section's heading, which takes the focus after a revoke. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
  /** Called after a token was revoked, as the task tokens it minted ended with it. */
  readonly onRevoked: () => void;
}

/**
 * Everyone's live personal tokens (or one owner's), newest first: the owner, the token's name,
 * when it was made, last used and ends, and Revoke. Revoking asks first, needs no passkey touch,
 * reads the list again from its first page and puts the focus on the section's heading.
 *
 * @param props - The owner, the heading and the revoke handler.
 * @param props.owner - Only this principal's tokens; null for everyone's.
 * @param props.heading - The section's heading.
 * @param props.onRevoked - Called after a token was revoked.
 * @returns The list.
 */
export function PersonalTokens({ owner, heading, onRevoked }: PersonalTokensProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback((cursor: string | null) => listAllTokens(owner, cursor), [owner]);
  const pages = useCursorPages(owner ?? "", load);
  const arm = useFocusOnRead(heading, pages.items);
  const [revoking, setRevoking] = useState<TokenOut | null>(null);
  const rows = pages.items ?? [];
  return (
    <>
      <p className="step-status" role="status">
        {pages.busy && pages.items === undefined ? t("tokens.personal.reading") : ""}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={
          owner === null
            ? t("tokens.personal.none")
            : t("tokens.personal.noneOf", { owner: markedText(owner) })
        }
        region={t("tokens.personal.region")}
        scroll="tokens-scroll"
        head={
          <tr>
            <th scope="col">{t("tokens.owner")}</th>
            <th scope="col">{t("tokens.name")}</th>
            <th scope="col">{t("tokens.created")}</th>
            <th scope="col">{t("tokens.lastUsed")}</th>
            <th scope="col">{t("tokens.expires")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((token) => (
          <tr key={token.key_id}>
            <td>
              <div className="person-cell">
                <Avatar name={token.owner} displayName={null} etag={null} size={24} />
                <Link className="token-owner" to={personPath(token.owner)}>
                  <VisibleText text={token.owner} />
                </Link>
              </div>
            </td>
            <td>
              <span className="row-name">
                <VisibleText text={token.name} />
              </span>
            </td>
            <td>{when(token.created_at)}</td>
            <td>{token.last_used_at === null ? t("tokens.never") : when(token.last_used_at)}</td>
            <td>{token.expires_at === null ? t("tokens.never") : when(token.expires_at)}</td>
            <td>
              <Button
                className="button"
                aria-label={t("tokens.revokeOf", {
                  name: markedText(token.name),
                  owner: markedText(token.owner),
                })}
                onPress={() => {
                  setRevoking(token);
                }}
              >
                {t("tokens.revoke")}
              </Button>
            </td>
          </tr>
        ))}
      </ViewList>
      {revoking !== null && (
        <ConfirmDialog
          title={t("tokens.confirm.title")}
          confirmLabel={t("tokens.confirm.action")}
          onConfirm={() => revokeToken(revoking.owner, revoking.key_id)}
          onCancel={() => {
            setRevoking(null);
          }}
          onDone={() => {
            setRevoking(null);
            arm();
            pages.reload();
            onRevoked();
          }}
        >
          <p>
            {t("tokens.confirm.personal", {
              name: markedText(revoking.name),
              owner: markedText(revoking.owner),
            })}
          </p>
          <p>{t("tokens.confirm.effect")}</p>
          <p>{t("tokens.confirm.cascade")}</p>
        </ConfirmDialog>
      )}
    </>
  );
}

/**
 * Says what a task token is for: its job, else its asset, else that it was minted by hand.
 *
 * @param t - The translator.
 * @param token - The task token.
 * @returns The words.
 */
function taskPurpose(t: TFunction, token: TaskTokenOut): string {
  if (token.job !== null) return t("tokens.task.job", { job: token.job });
  if (token.asset !== null) return t("tokens.task.asset", { asset: token.asset });
  return t("tokens.task.byHand");
}

/** Properties of {@link TaskPurpose}. */
interface TaskPurposeProps {
  /** The task token. */
  readonly token: TaskTokenOut;
}

/**
 * Shows what a task token is for: its job or asset, a link to its run when it names one, or
 * "minted by hand".
 *
 * @param props - The token.
 * @param props.token - The task token.
 * @returns The cell's content.
 */
function TaskPurpose({ token }: TaskPurposeProps): JSX.Element {
  const { t } = useTranslation();
  const name = token.job ?? token.asset;
  if (name === null) return <>{t("tokens.task.byHand")}</>;
  const text = <VisibleText text={name} />;
  return token.run_id === null ? (
    text
  ) : (
    <Link className="token-owner" to={jobRunPath(token.environment, token.run_id)}>
      {text}
    </Link>
  );
}

/** Properties of {@link TaskTokens}. */
export interface TaskTokensProps {
  /** Only this environment's task tokens; null for every environment's. */
  readonly environment: string | null;
  /** The section's heading, which takes the focus after a revoke. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * The live task tokens (or one environment's), newest first: the environment, what it is for,
 * the principal it acts as, when it was issued and ends, and Revoke. Revoking asks first, needs
 * no passkey touch, reads the list again from its first page and puts the focus on the
 * section's heading.
 *
 * @param props - The environment and the heading.
 * @param props.environment - Only this environment's; null for every environment's.
 * @param props.heading - The section's heading.
 * @returns The list.
 */
export function TaskTokens({ environment, heading }: TaskTokensProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback(
    (cursor: string | null) => listTaskTokens(environment, cursor),
    [environment],
  );
  const pages = useCursorPages(environment ?? "", load);
  const arm = useFocusOnRead(heading, pages.items);
  const [revoking, setRevoking] = useState<TaskTokenOut | null>(null);
  const rows = pages.items ?? [];
  const words = (token: TaskTokenOut): Record<string, string> => ({
    principal: markedText(token.principal),
    environment: markedText(token.environment),
    purpose: taskPurpose(t, token),
  });
  return (
    <>
      <p className="step-status" role="status">
        {pages.busy && pages.items === undefined ? t("tokens.task.reading") : ""}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("tokens.task.none")}
        region={t("tokens.task.region")}
        scroll="tokens-scroll"
        head={
          <tr>
            <th scope="col">{t("tokens.task.environment")}</th>
            <th scope="col">{t("tokens.task.for")}</th>
            <th scope="col">{t("tokens.task.principal")}</th>
            <th scope="col">{t("tokens.task.issued")}</th>
            <th scope="col">{t("tokens.expires")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((token) => (
          <tr key={token.key_id}>
            <td>
              <EnvironmentLabel environment={token.environment} />
            </td>
            <td>
              <TaskPurpose token={token} />
            </td>
            <td>
              <VisibleText text={token.principal} />
            </td>
            <td>{when(token.issued_at)}</td>
            <td>{when(token.expires_at)}</td>
            <td>
              <Button
                className="button"
                aria-label={t("tokens.revokeTask", words(token))}
                onPress={() => {
                  setRevoking(token);
                }}
              >
                {t("tokens.revoke")}
              </Button>
            </td>
          </tr>
        ))}
      </ViewList>
      {revoking !== null && (
        <ConfirmDialog
          title={t("tokens.confirm.title")}
          confirmLabel={t("tokens.confirm.action")}
          onConfirm={() => revokeTaskToken(revoking.key_id)}
          onCancel={() => {
            setRevoking(null);
          }}
          onDone={() => {
            setRevoking(null);
            arm();
            pages.reload();
          }}
        >
          <p>{t("tokens.confirm.task", words(revoking))}</p>
          <p>{t("tokens.confirm.effect")}</p>
        </ConfirmDialog>
      )}
    </>
  );
}
