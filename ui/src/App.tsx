/**
 * The root component of the interface.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * The whole interface: for now, the product name as the page heading.
 *
 * @returns The page's main landmark.
 */
export function App(): JSX.Element {
  return (
    <main>
      <h1>Lakana</h1>
    </main>
  );
}
