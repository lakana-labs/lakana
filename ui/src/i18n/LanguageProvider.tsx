/**
 * Gives React Aria the interface's language, so that its own words (a list's name, the count of
 * suggestions it announces) and what it formats follow the language i18next shows, not the
 * browser's.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { I18nProvider } from "react-aria-components";
import { useTranslation } from "react-i18next";

/** Properties of {@link LanguageProvider}. */
export interface LanguageProviderProps {
  /** The interface. */
  readonly children: ReactNode;
}

/**
 * Renders the interface in i18next's language for React Aria; it renders again when the
 * language changes.
 *
 * @param props - The interface.
 * @param props.children - The interface.
 * @returns The interface inside React Aria's locale.
 */
export function LanguageProvider({ children }: LanguageProviderProps): JSX.Element {
  const { i18n } = useTranslation();
  return <I18nProvider locale={i18n.language}>{children}</I18nProvider>;
}
