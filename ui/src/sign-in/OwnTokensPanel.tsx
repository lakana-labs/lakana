/**
 * One's own API tokens on the account page: list them, create one after the passkey touch and
 * see it once, revoke one.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useState } from "react";
import {
  Button,
  FieldError,
  Form,
  Heading,
  Input,
  Label,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  createOwnToken,
  listOwnTokens,
  revokeToken,
  type TokenIssueOut,
  type TokenOut,
} from "../api/tokens";
import { ShownOnce } from "../access/ShownOnce";
import "../access/tokens.css";
import "../access/tokenStrings";
import { ActionDialog } from "../shell/ActionDialog";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { CopyLine } from "../shell/CopyLine";
import { Loaded } from "../shell/Loaded";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "./Attempt";

/** Properties of {@link OwnTokensPanel}. */
export interface OwnTokensPanelProps {
  /** The person's own principal name. */
  readonly name: string;
}

/**
 * Whether a token no longer works: revoked, or past its expiry.
 *
 * @param token - The token.
 * @param now - The time to judge expiry by.
 * @returns True once it has ended.
 */
function tokenEnded(token: TokenOut, now: number): boolean {
  return (
    token.revoked_at !== null || (token.expires_at !== null && Date.parse(token.expires_at) <= now)
  );
}

/**
 * Orders tokens as the panel lists them: the live ones first, then the ended ones, each newest
 * first.
 *
 * @param tokens - The tokens.
 * @param now - The time to judge expiry by.
 * @returns The tokens in that order.
 */
function inListOrder(tokens: readonly TokenOut[], now: number): readonly TokenOut[] {
  const newest = (a: TokenOut, b: TokenOut): number => b.created_at.localeCompare(a.created_at);
  return [
    ...tokens.filter((token) => !tokenEnded(token, now)).sort(newest),
    ...tokens.filter((token) => tokenEnded(token, now)).sort(newest),
  ];
}

/** Properties of {@link CreateTokenDialog}. */
interface CreateTokenDialogProps {
  /** The person's own principal name. */
  readonly owner: string;
  /** Closes the dialog; `made` says whether a token was made. */
  readonly onClose: (made: boolean) => void;
}

/**
 * Asks for the token's name, makes it after the passkey touch and shows it once with the
 * `lakana login` command. The token lives only in this dialog's state: Done and Escape unmount
 * the dialog, and the token with it.
 *
 * @param props - The owner and the close handler.
 * @param props.owner - The person's own principal name.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
function CreateTokenDialog({ owner, onClose }: CreateTokenDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<TokenIssueOut | null>(null);
  const close = (): void => {
    setIssued(null);
    onClose(issued !== null);
  };
  return (
    <ActionDialog onClose={close}>
      {issued === null ? (
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            attempt.run(async () => {
              setIssued(await createOwnToken(owner, label.trim()));
            });
          }}
        >
          <Heading slot="title">{t("tokens.own.create")}</Heading>
          <TextField className="field" isRequired value={label} onChange={setLabel}>
            <Label>{t("tokens.name")}</Label>
            <Input autoComplete="off" />
            <Text slot="description">{t("tokens.own.nameHelp")}</Text>
            <FieldError />
          </TextField>
          <div className="step-actions">
            <PendingButton type="submit" className="button button-primary" pending={attempt.busy}>
              {t("tokens.own.submit")}
            </PendingButton>
            <Button className="button" onPress={close}>
              {t("dialog.cancel")}
            </Button>
          </div>
          <AttemptStatus attempt={attempt} waiting={t("common.working")} />
        </Form>
      ) : (
        <ShownOnce
          title={t("tokens.shown.title", { name: markedText(issued.name) })}
          value={issued.token}
          copyLabel={t("tokens.shown.copy")}
          onDone={close}
        >
          <p>{t("tokens.shown.login")}</p>
          <CopyLine
            text={t("tokens.shown.command", { origin: window.location.origin })}
            copyLabel={t("tokens.copyCommand")}
          />
        </ShownOnce>
      )}
    </ActionDialog>
  );
}

/**
 * Renders the panel "API tokens": the purpose, every token of the person's with its state and
 * Revoke on the live ones, and Create a token. After a token is made or revoked the list is read
 * again and the focus moves to the panel's heading.
 *
 * @param props - The person.
 * @param props.name - The person's own principal name.
 * @returns The panel.
 */
export function OwnTokensPanel({ name }: OwnTokensPanelProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  // Expiry is judged at the time of the read, so a render never asks the clock.
  const load = useCallback(
    async () => ({ tokens: await listOwnTokens(name), now: Date.now() }),
    [name],
  );
  const tokens = useResource(load);
  const { target: headingRef, arm: armFocus } = useFocusAfter<HTMLHeadingElement>(tokens.data);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<TokenOut | null>(null);
  return (
    <section className="panel" aria-labelledby={heading}>
      <div className="page-head">
        <h2 id={heading} ref={headingRef} tabIndex={-1}>
          {t("tokens.own.title")}
        </h2>
        <Button
          className="button"
          onPress={() => {
            setCreating(true);
          }}
        >
          {t("tokens.own.create")}
        </Button>
      </div>
      <p className="page-purpose">{t("tokens.own.purpose")}</p>
      <Loaded resource={tokens}>
        {({ tokens: found, now }) =>
          found.length === 0 ? (
            <p className="empty">{t("tokens.own.none")}</p>
          ) : (
            <div
              className="list-scroll own-tokens-scroll"
              role="region"
              aria-label={t("tokens.own.region")}
              // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
              tabIndex={0}
            >
              <table className="list">
                <thead>
                  <tr>
                    <th scope="col">{t("tokens.name")}</th>
                    <th scope="col">{t("tokens.state")}</th>
                    <th scope="col">{t("tokens.created")}</th>
                    <th scope="col">{t("tokens.lastUsed")}</th>
                    <th scope="col">{t("tokens.expires")}</th>
                    <th scope="col">
                      <span className="visually-hidden">{t("list.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {inListOrder(found, now).map((token) => (
                    <tr key={token.key_id}>
                      <td>
                        <span className="row-name">
                          <VisibleText text={token.name} />
                        </span>
                      </td>
                      <td>
                        {token.revoked_at !== null
                          ? t("tokens.revokedAt", { when: when(token.revoked_at) })
                          : token.expires_at !== null && tokenEnded(token, now)
                            ? t("tokens.expiredAt", { when: when(token.expires_at) })
                            : t("tokens.active")}
                      </td>
                      <td>{when(token.created_at)}</td>
                      <td>
                        {token.last_used_at === null ? t("tokens.never") : when(token.last_used_at)}
                      </td>
                      <td>
                        {token.expires_at === null ? t("tokens.never") : when(token.expires_at)}
                      </td>
                      <td>
                        {!tokenEnded(token, now) && (
                          <Button
                            className="button"
                            aria-label={t("tokens.revokeOwn", { name: markedText(token.name) })}
                            onPress={() => {
                              setRevoking(token);
                            }}
                          >
                            {t("tokens.revoke")}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
      {creating && (
        <CreateTokenDialog
          owner={name}
          onClose={(made) => {
            setCreating(false);
            if (made) {
              armFocus();
              tokens.reload();
            }
          }}
        />
      )}
      {revoking !== null && (
        <ConfirmDialog
          title={t("tokens.confirm.title")}
          confirmLabel={t("tokens.confirm.action")}
          onConfirm={() => revokeToken(name, revoking.key_id)}
          onCancel={() => {
            setRevoking(null);
          }}
          onDone={() => {
            setRevoking(null);
            armFocus();
            tokens.reload();
          }}
        >
          <p>
            {t("tokens.confirm.personal", {
              name: markedText(revoking.name),
              owner: markedText(name),
            })}
          </p>
          <p>{t("tokens.confirm.effect")}</p>
          <p>{t("tokens.confirm.cascade")}</p>
        </ConfirmDialog>
      )}
    </section>
  );
}
