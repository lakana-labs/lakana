/**
 * Whether an environment is protected, read once for the dialogs that ask for a reason.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";

import { isProtected } from "../../api/manage";

/**
 * Reads whether `environment` is protected. Only a failure to read it leaves the answer open:
 * it is null then, so no dialog asks for a reason that may not be needed, and the server's own
 * refusal says what to do.
 *
 * @param environment - The environment.
 * @returns True or false when known; null when the person may not read the environment or the
 *   read failed; undefined while it is being read.
 */
export function useProtected(environment: string): boolean | null | undefined {
  const [known, setKnown] = useState<{ environment: string; value: boolean | null } | null>(null);
  useEffect(() => {
    let current = true;
    isProtected(environment).then(
      (value) => {
        if (current) {
          setKnown({ environment, value });
        }
      },
      () => {
        if (current) {
          setKnown({ environment, value: null });
        }
      },
    );
    return () => {
      current = false;
    };
  }, [environment]);
  return known?.environment === environment ? known.value : undefined;
}
