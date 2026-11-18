/**
 * The strings of the Connections screen, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the screen's chunk, not with the first visit. Each module of the screen that
 * writes one of them imports this module, so they are in place before the screen renders and
 * no string shows untranslated.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./connections.en.json";

i18next.addResourceBundle("en", "translation", { connections: strings }, true, false);
