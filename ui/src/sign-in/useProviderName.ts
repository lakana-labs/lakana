/**
 * The name of the identity provider people sign in through, for the pages before sign-in.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";

import { getProvider } from "../api/oidc";

/**
 * Reads, once per visit, the name of the identity provider people sign in through.
 *
 * @param wanted - False while the page does not need it yet: nothing is read.
 * @returns The provider's name, or null while it is off, unknown or cannot be read.
 */
export function useProviderName(wanted = true): string | null {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    if (!wanted) {
      return;
    }
    let live = true;
    getProvider().then(
      (info) => {
        if (live && info.enabled && info.display_name !== null) {
          setName(info.display_name);
        }
      },
      () => {
        // Without the answer the page is the passkey page it always was.
      },
    );
    return () => {
      live = false;
    };
  }, [wanted]);
  return name;
}
