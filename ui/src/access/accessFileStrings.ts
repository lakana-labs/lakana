/**
 * The strings of Access file, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the place, never with the first visit. Each module that writes one of them
 * imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./accessFile.en.json";

i18next.addResourceBundle("en", "translation", { accessFile: strings }, true, false);
