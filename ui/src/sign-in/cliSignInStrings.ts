/**
 * The strings of the command-line sign-in pages, registered with i18next when the module runs.
 *
 * @remarks
 * They load with those pages, never with the first visit.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./cliSignIn.en.json";

i18next.addResourceBundle("en", "translation", { cliSignIn: strings }, true, false);
