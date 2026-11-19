/**
 * Where a value typed into a form went, outside the request that carries it.
 *
 * @packageDocumentation
 */

/**
 * Says where the sentinel is outside the request bodies: the page's HTML as it is (text and
 * every attribute, an input's `value` attribute included), the address, the storages and the
 * console. A still-open input's live value is not in the HTML.
 *
 * @param sentinel - The value looked for.
 * @param spies - The console spies.
 * @returns The places found.
 */
export function whereIs(
  sentinel: string,
  spies: readonly { readonly mock: { readonly calls: readonly unknown[] } }[],
): string[] {
  const found: string[] = [];
  if (document.documentElement.outerHTML.includes(sentinel)) found.push("html");
  if (document.body.textContent.includes(sentinel)) found.push("text");
  for (const element of document.querySelectorAll("*")) {
    for (const attribute of element.attributes) {
      if (attribute.value.includes(sentinel)) found.push(`attribute ${attribute.name}`);
    }
  }
  if (window.location.href.includes(sentinel)) found.push("address");
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index) ?? "";
      if (`${key}${storage.getItem(key) ?? ""}`.includes(sentinel)) found.push("storage");
    }
  }
  for (const spy of spies) {
    if (JSON.stringify(spy.mock.calls).includes(sentinel)) found.push("console");
  }
  return found;
}
