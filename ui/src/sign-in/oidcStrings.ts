/**
 * The strings of the identity provider's return pages and the profile's Sign-in section,
 * registered with i18next when the module runs.
 *
 * @remarks
 * They load with those pages, never with the first visit.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./oidc.en.json";

i18next.addResourceBundle("en", "translation", { oidc: strings }, true, false);
