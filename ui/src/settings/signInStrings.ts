/**
 * The strings Settings › Sign-in, Invites and Recovery links add, registered with i18next when
 * the module runs.
 *
 * @remarks
 * They load with those places, never with the first visit. They are deep-merged into
 * `settings.signIn`, beside the keys that stay in `en.json`.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./signIn.en.json";

i18next.addResourceBundle("en", "translation", { settings: { signIn: strings } }, true, false);
