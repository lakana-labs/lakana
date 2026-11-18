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

/**
 * A list's header row: the columns, then the unnamed column of Revoke.
 *
 * @param props - The columns.
 * @param props.columns - The columns' names, in order.
 * @returns The row.
 */
function HeadRow({ columns }: { readonly columns: readonly string[] }): JSX.Element {
  const { t } = useTranslation();
  return (
    <tr>
      {columns.map((column) => (
        <th scope="col" key={column}>
          {column}
        </th>
      ))}
      <th scope="col">
        <span className="visually-hidden">{t("list.actions")}</span>
      </th>
    </tr>
  );
}

/** Properties of {@link RevokeCell}. */
interface RevokeCellProps {
  /** The button's accessible name, naming the token. */
  readonly label: string;
  /** Asks to revoke the token. */
  readonly onPress: () => void;
}

/**
 * A row's Revoke.
 *
 * @param props - The name and the handler.
 * @param props.label - The button's accessible name.
 * @param props.onPress - Asks to revoke the token.
 * @returns The cell.
 */
function RevokeCell({ label, onPress }: RevokeCellProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <td>
      <Button className="button" aria-label={label} onPress={onPress}>
        {t("tokens.revoke")}
      </Button>
    </td>
  );
}

/** Properties of {@link RevokeDialog}. */
interface RevokeDialogProps {
  /** Revokes the token. */
  readonly onConfirm: () => Promise<void>;
  /** Closes the dialog. */
  readonly onClose: () => void;
  /** Called once the token is revoked, after the dialog is closed. */
  readonly onDone: () => void;
  /** What is revoked, in a sentence. */
  readonly what: string;
  /** What else stops with it, if anything. */
  readonly also?: string;
}

/**
 * The question before a revoke, which needs no passkey touch.
 *
 * @param props - The handlers and the sentences.
 * @param props.onConfirm - Revokes the token.
 * @param props.onClose - Closes the dialog.
 * @param props.onDone - Called once the token is revoked.
 * @param props.what - What is revoked.
 * @param props.also - What else stops with it.
 * @returns The dialog.
 */
function RevokeDialog({ onConfirm, onClose, onDone, what, also }: RevokeDialogProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <ConfirmDialog
      title={t("tokens.confirm.title")}
      confirmLabel={t("tokens.confirm.action")}
      onConfirm={onConfirm}
      onCancel={onClose}
      onDone={() => {
        onClose();
        onDone();
      }}
    >
      <p>{what}</p>
      <p>{t("tokens.confirm.effect")}</p>
      {also !== undefined && <p>{also}</p>}
    </ConfirmDialog>
  );
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
          <HeadRow
            columns={[
              t("tokens.owner"),
              t("tokens.name"),
              t("tokens.created"),
              t("tokens.lastUsed"),
              t("tokens.expires"),
            ]}
          />
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
            <RevokeCell
              label={t("tokens.revokeOf", {
                name: markedText(token.name),
                owner: markedText(token.owner),
              })}
              onPress={() => {
                setRevoking(token);
              }}
            />
          </tr>
        ))}
      </ViewList>
      {revoking !== null && (
        <RevokeDialog
          onConfirm={() => revokeToken(revoking.owner, revoking.key_id)}
          onClose={() => {
            setRevoking(null);
          }}
          onDone={() => {
            arm();
            pages.reload();
            onRevoked();
          }}
          what={t("tokens.confirm.personal", {
            name: markedText(revoking.name),
            owner: markedText(revoking.owner),
          })}
          also={t("tokens.confirm.cascade")}
        />
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
          <HeadRow
            columns={[
              t("tokens.task.environment"),
              t("tokens.task.for"),
              t("tokens.task.principal"),
              t("tokens.task.issued"),
              t("tokens.expires"),
            ]}
          />
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
            <RevokeCell
              label={t("tokens.revokeTask", words(token))}
              onPress={() => {
                setRevoking(token);
              }}
            />
          </tr>
        ))}
      </ViewList>
      {revoking !== null && (
        <RevokeDialog
          onConfirm={() => revokeTaskToken(revoking.key_id)}
          onClose={() => {
            setRevoking(null);
          }}
          onDone={() => {
            arm();
            pages.reload();
          }}
          what={t("tokens.confirm.task", words(revoking))}
        />
      )}
    </>
  );
}
