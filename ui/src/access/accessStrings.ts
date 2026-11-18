/**
 * The strings of Access's places (Groups, Grants, What I may do), registered with i18next when
 * the module runs.
 *
 * @remarks
 * They load with those places, never with the first visit, and are merged into the `access`
 * strings the first load already has. Each module that writes one of them imports this module.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./access.en.json";

i18next.addResourceBundle("en", "translation", { access: strings }, true, false);
