/**
 * A person's picture in a circle, or their initials without one.
 *
 * @packageDocumentation
 */
import "./profile.css";

import { type JSX, useEffect, useState, useSyncExternalStore } from "react";

import { changedPicture, pictureUrl, releasePicture, watchPictures } from "../api";
import { initials } from "./initials";

/**
 * The etag of a person's picture as this tab knows it: what this tab last set or removed, else
 * what the server answered.
 *
 * @param name - The principal name.
 * @param etag - The etag the server answered, or null without a picture.
 * @returns The current etag, or null without a picture.
 */
export function useCurrentPicture(name: string, etag: string | null): string | null {
  const changed = useSyncExternalStore(watchPictures, () => changedPicture(name));
  return changed === undefined ? etag : changed;
}

/** Properties of {@link Avatar}. */
export interface AvatarProps {
  /** The person's principal name. */
  readonly name: string;
  /** The name to show, when the person has one. */
  readonly displayName: string | null;
  /** The picture's etag, or null without one: then nothing is read. */
  readonly etag: string | null;
  /** The circle's size in pixels. */
  readonly size: 24 | 32 | 48;
  /**
   * The person's name, when no visible name stands beside the circle; without it the circle is
   * decorative and hidden from screen readers.
   */
  readonly label?: string;
}

/**
 * Renders the circle: the initials while the picture is read, when it cannot be shown and
 * without one; the picture once it arrived.
 *
 * @param props - The person, the etag, the size and the name to announce.
 * @param props.name - The principal name.
 * @param props.displayName - The name to show, if any.
 * @param props.etag - The picture's etag, or null.
 * @param props.size - The size in pixels.
 * @param props.label - The accessible name, when no visible name stands beside it.
 * @returns The circle.
 */
export function Avatar({ name, displayName, etag, size, label }: AvatarProps): JSX.Element {
  const current = useCurrentPicture(name, etag);
  const key = typeof current === "string" ? `${name}:${current}` : null;
  const [shown, setShown] = useState<{ key: string; url: string } | null>(null);
  useEffect(() => {
    if (typeof current !== "string") {
      return;
    }
    // An older read that answers after a newer one must not replace it.
    let live = true;
    pictureUrl(name, current).then(
      (url) => {
        if (live) {
          setShown({ key: `${name}:${current}`, url });
        }
      },
      // The initials stay: the circle is never an error.
      () => undefined,
    );
    return () => {
      live = false;
      releasePicture(name, current);
    };
  }, [name, current]);
  const url = shown !== null && shown.key === key ? shown.url : null;
  const naming =
    label === undefined ? { "aria-hidden": true } : { role: "img", "aria-label": label };
  return (
    <span className={`avatar avatar-${String(size)}`} {...naming}>
      {url === null ? (
        initials(name, displayName)
      ) : (
        <img
          src={url}
          alt=""
          onError={() => {
            setShown(null);
          }}
        />
      )}
    </span>
  );
}
