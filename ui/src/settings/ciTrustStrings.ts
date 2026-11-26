/**
 * The strings of Settings › CI trust, registered with i18next when the module runs.
 *
 * @remarks
 * They load with CI trust, never with the first visit; each module of the place imports this one.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./ciTrust.en.json";

i18next.addResourceBundle("en", "translation", { settings: { ciTrust: strings } }, true, false);
