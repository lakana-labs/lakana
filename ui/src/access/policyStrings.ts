/**
 * The strings of Row filters & masks, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the place, never with the first visit. Each module that writes one of them
 * imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./policies.en.json";

i18next.addResourceBundle("en", "translation", { policies: strings }, true, false);
