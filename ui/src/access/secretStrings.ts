/**
 * The strings of Settings › Access › Secrets and its dialogs, registered with i18next when the
 * module runs.
 *
 * @remarks
 * They load with Secrets, never with the first visit. Each module that writes one of them
 * imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./secrets.en.json";

i18next.addResourceBundle("en", "translation", { secrets: strings }, true, false);
