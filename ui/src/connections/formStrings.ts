/**
 * The strings of the connection form, registered with i18next when the module runs.
 *
 * @remarks
 * They load with the form's chunk, not with the list's. They join the screen's strings under
 * `connections.form`; each module of the form imports this one, after `connectionStrings`, so
 * that the kinds' words are in place too.
 * @packageDocumentation
 */
import i18next from "i18next";

import "./connectionStrings";
import strings from "./form.en.json";

i18next.addResourceBundle("en", "translation", { connections: { form: strings } }, true, false);
