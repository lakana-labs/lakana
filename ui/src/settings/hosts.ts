/**
 * What the Connect a repository page knows about a git host from a repository's URL: its SSH
 * form, a proposed name and the host's own pages for deploy keys and SSH fingerprints.
 *
 * @remarks
 * A host is recognised only by an exact match of the parsed host name, so
 * `github.com.evil.example` or `evil.example/github.com` never gets GitHub's fingerprints page
 * for another host's keys. Every path part of a link it builds is encoded.
 * @packageDocumentation
 */

/** The hosts the page knows by name. */
type KnownHost = "github" | "gitlab" | "bitbucket";

/** What the page knows about a repository's host. */
export interface RecognisedHost {
  /** The host, or `other` for one the page does not know. */
  readonly host: KnownHost | "other";
  /** The URL to register over SSH, or null when there is none (another host's HTTPS URL). */
  readonly sshUrl: string | null;
  /** The proposed repository name: the last path part without `.git`; empty without one. */
  readonly name: string;
  /** The host's page to add a deploy key on, or null. */
  readonly deployKeyPage: string | null;
  /** The host's published SSH fingerprints, or null. */
  readonly fingerprintsPage: string | null;
}

/** A known host's name, its pages, and how many path parts a repository's path has there. */
interface HostFacts {
  /** The host name. */
  readonly hostname: string;
  /** The page that lists the host's SSH fingerprints. */
  readonly fingerprints: string;
  /** The deploy-key page of a repository, from its encoded path parts; null for a bad path. */
  readonly deployKeys: (parts: readonly string[]) => string | null;
  /** The repository's own path parts, from those of an address the browser shows. */
  readonly repository: (parts: readonly string[]) => readonly string[];
}

/**
 * The first two path parts: a repository of GitHub or Bitbucket is `owner/repo`, and what
 * follows (`tree/main`, `blob/…`, `src/…`) is a page of it.
 *
 * @param parts - The path parts.
 * @returns The repository's parts.
 */
function firstTwo(parts: readonly string[]): readonly string[] {
  return parts.slice(0, 2);
}

/**
 * The deploy-key page of a host whose repositories are `owner/repo`.
 *
 * @param base - The host's address.
 * @param suffix - The page below the repository.
 * @returns The builder.
 */
function ownerRepo(base: string, suffix: string): (parts: readonly string[]) => string | null {
  return (parts) => (parts.length === 2 ? `${base}/${parts.join("/")}/${suffix}` : null);
}

const HOSTS: Readonly<Record<KnownHost, HostFacts>> = {
  github: {
    hostname: "github.com",
    fingerprints:
      "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints",
    deployKeys: ownerRepo("https://github.com", "settings/keys/new"),
    repository: firstTwo,
  },
  gitlab: {
    hostname: "gitlab.com",
    fingerprints: "https://docs.gitlab.com/user/gitlab_com/#ssh-host-keys-fingerprints",
    deployKeys: (parts) =>
      parts.length >= 2 ? `https://gitlab.com/${parts.join("/")}/-/settings/repository` : null,
    // A GitLab page of a repository starts with `/-/` (`/-/tree/main`).
    repository: (parts) => (parts.includes("-") ? parts.slice(0, parts.indexOf("-")) : parts),
  },
  bitbucket: {
    hostname: "bitbucket.org",
    fingerprints:
      "https://support.atlassian.com/bitbucket-cloud/docs/configure-ssh-and-two-step-verification/",
    deployKeys: ownerRepo("https://bitbucket.org", "admin/access-keys/"),
    repository: firstTwo,
  },
};

/** A URL as the page reads it. */
interface Parsed {
  /** `https` or `ssh`. */
  readonly scheme: "https" | "ssh";
  /** The host name, lower case. */
  readonly hostname: string;
  /** The path parts, decoded, `.git` removed from the last. */
  readonly parts: readonly string[];
}

/** `[user@]host:path`, the short SSH form git accepts. */
const SCP_FORM = /^(?:[^@/\s]+@)?([^@:/\s]+):(?!\/)(\S+)$/;

/**
 * Whether a URL has a `?` or `#`, which the server refuses in a repository URL: in the
 * short SSH form either can hide the real host (`git@github.com?@evil.example:o/r`).
 *
 * @param url - What the person entered.
 * @returns True when it has one.
 */
export function queryOrFragment(url: string): boolean {
  return /[?#]/.test(url);
}

/**
 * Reads a URL into its scheme, host and path parts.
 *
 * @param url - What the person entered.
 * @returns The parts, or null for anything but an `https://`, `ssh://` or `host:path` URL.
 */
function parse(url: string): Parsed | null {
  if (queryOrFragment(url)) {
    return null;
  }
  const scp = SCP_FORM.exec(url.trim());
  // The short form's host is read through URL too, so every host is compared the same way.
  let parsed: URL;
  try {
    parsed = new URL(scp === null ? url.trim() : `ssh://${String(scp[1])}`);
  } catch {
    return null;
  }
  const scheme = parsed.protocol === "https:" ? "https" : parsed.protocol === "ssh:" ? "ssh" : null;
  if (scheme === null || parsed.hostname === "") {
    return null;
  }
  const path = scp === null ? parsed.pathname.split("/").map(decoded) : String(scp[2]).split("/");
  // An SSH URL's host is not lower-cased by URL; a trailing dot names the same host.
  const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
  return { scheme, hostname, parts: withoutGit(path.filter((part) => part !== "")) };
}

/**
 * Path parts with `.git` taken off the last one; an emptied last part is dropped.
 *
 * @param parts - The path parts.
 * @returns The parts.
 */
function withoutGit(parts: readonly string[]): readonly string[] {
  const last = parts.at(-1);
  if (last === undefined) {
    return parts;
  }
  return [...parts.slice(0, -1), last.replace(/\.git$/, "")].filter((part) => part !== "");
}

/**
 * Decodes one path part of a parsed URL.
 *
 * @param part - The part as the URL has it.
 * @returns It decoded, or as it is when it is not valid percent-encoding.
 */
function decoded(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

/**
 * Which known host a host name is, by exact match.
 *
 * @param hostname - The parsed host name.
 * @returns The host, or undefined.
 */
function known(hostname: string): KnownHost | undefined {
  return (Object.keys(HOSTS) as KnownHost[]).find((id) => HOSTS[id].hostname === hostname);
}

/**
 * Recognises a repository's host from its URL.
 *
 * @param url - The repository's URL as the person entered it.
 * @returns What the page knows about the host; for GitHub, GitLab or Bitbucket the repository's
 * SSH form (also from an HTTPS URL or a page of it in the browser), for another host's HTTPS URL
 * and for a URL with `?` or `#` no SSH form.
 */
export function recogniseHost(url: string): RecognisedHost {
  const parsed = parse(url);
  const id = parsed === null ? undefined : known(parsed.hostname);
  const parts = parsed?.parts ?? [];
  const name = parts.at(-1) ?? "";
  if (parsed === null || id === undefined) {
    return {
      host: "other",
      sshUrl: parsed?.scheme === "ssh" && parts.length > 0 ? url.trim() : null,
      name,
      deployKeyPage: null,
      fingerprintsPage: null,
    };
  }
  const facts = HOSTS[id];
  const repository = withoutGit(facts.repository(parts));
  if (repository.length === 0) {
    return { host: id, sshUrl: null, name, deployKeyPage: null, fingerprintsPage: null };
  }
  return {
    host: id,
    sshUrl: `git@${facts.hostname}:${repository.join("/")}.git`,
    name: String(repository.at(-1)),
    deployKeyPage: facts.deployKeys(repository.map(encodeURIComponent)),
    fingerprintsPage: facts.fingerprints,
  };
}

/**
 * The HTTPS form of a repository's URL, for registering it with a token instead.
 *
 * @param url - The repository's URL.
 * @returns The URL itself when it is HTTPS, `https://host/path.git` for an SSH URL, or null.
 */
export function httpsForm(url: string): string | null {
  const parsed = parse(url);
  if (parsed === null) {
    return null;
  }
  if (parsed.scheme === "https") {
    return url.trim();
  }
  return `https://${parsed.hostname}/${parsed.parts.map(encodeURIComponent).join("/")}.git`;
}
