/**
 * The strings of a new service account and of a deactivation (People and a person's page),
 * registered with i18next when the module runs.
 *
 * @remarks
 * They load with People, never with the first visit, and are merged into the `access` strings.
 * Each module that writes one of them imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./principals.en.json";

i18next.addResourceBundle("en", "translation", { access: strings }, true, false);
