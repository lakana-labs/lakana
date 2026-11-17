/**
 * The Settings frame's strings, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the Settings chunk, never with the first visit. Each module of the frame that
 * writes one of them imports this module, so they are in place before the frame renders.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./settings.en.json";

i18next.addResourceBundle("en", "translation", { settings: { places: strings } }, true, false);
