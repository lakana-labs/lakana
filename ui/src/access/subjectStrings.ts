/**
 * The strings of who a grant or a policy is for, registered with i18next when the module runs.
 *
 * @remarks
 * Their own small bundle: the table's Access tab and the Access places both name a subject, and
 * the tab should not carry every string of Access.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./subject.en.json";

i18next.addResourceBundle("en", "translation", { access: { subject: strings } }, true, false);
