/**
 * The Jobs list's strings, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the list's chunk, not with the Jobs screen's: the job, run and task pages do not
 * need them. The words the screen shows before the list's chunk arrives ("Loading the jobs…")
 * and those of the controls in a row stay in `jobs.en.json`. Each module of the list that writes
 * one of them imports this module, so they are in place before the list renders.
 * @packageDocumentation
 */
import i18next from "i18next";

import "./jobStrings";
import strings from "./list.en.json";

i18next.addResourceBundle("en", "translation", { jobs: { list: strings } }, true, false);
