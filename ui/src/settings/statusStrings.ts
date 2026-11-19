/**
 * The strings of Settings › Status, registered with i18next when the module runs.
 *
 * @remarks
 * They load with Status, never with the first visit; the health line's own strings stay under
 * `home.status`, which Home's first load shows.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./status.en.json";

i18next.addResourceBundle("en", "translation", { settings: { statusPage: strings } }, true, false);
