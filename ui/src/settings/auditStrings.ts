/**
 * The strings of Settings › Audit log, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the audit log, never with the first visit.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./audit.en.json";

i18next.addResourceBundle("en", "translation", { settings: { audit: strings } }, true, false);
