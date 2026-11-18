/**
 * The Deploys tab's strings, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the tab's chunk, not with the Jobs screen's; the rollback dialog and its note
 * use them too. Each module of the tab that writes one of them imports this module, so they are
 * in place before the tab renders.
 * @packageDocumentation
 */
import i18next from "i18next";

import strings from "./deploys.en.json";
import "./jobStrings";

i18next.addResourceBundle("en", "translation", { jobs: { deploys: strings } }, true, false);
