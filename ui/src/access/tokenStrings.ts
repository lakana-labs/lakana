/**
 * The strings of tokens (Settings › Access › Tokens and one's own API tokens), registered with
 * i18next when the module runs.
 *
 * @remarks
 * They load with those screens, never with the first visit. Each module that writes one of them
 * imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./tokens.en.json";

i18next.addResourceBundle("en", "translation", { tokens: strings }, true, false);
