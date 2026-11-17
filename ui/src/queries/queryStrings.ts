/**
 * The Queries screen's strings, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the screen's chunk, never with the first visit. Each module of the screen that
 * writes one of them imports this module, so they are in place before the screen renders.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./queries.en.json";

i18next.addResourceBundle("en", "translation", { queries: strings }, true, false);
