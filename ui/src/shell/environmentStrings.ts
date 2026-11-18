/**
 * The environment picker's strings, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the picker, which only screens loaded on demand use, never with the first
 * visit. The picker imports this module, so they are in place before it renders.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./environment.en.json";

i18next.addResourceBundle("en", "translation", { environment: strings }, true, false);
