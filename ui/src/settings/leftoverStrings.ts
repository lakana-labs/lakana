/**
 * The strings of an environment's Leftover directories, registered with i18next when the module
 * runs.
 *
 * @remarks
 * They load with the panel, which loads on its own inside an environment's page.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./leftover.en.json";

i18next.addResourceBundle("en", "translation", { settings: { leftover: strings } }, true, false);
