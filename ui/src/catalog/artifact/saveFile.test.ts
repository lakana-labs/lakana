import { afterEach, describe, expect, it, vi } from "vitest";

import { fileName, REVOKE_AFTER_MS, saveBlob } from "./saveFile";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("fileName", () => {
  it("saves under the last segment of the path", () => {
    expect(fileName("models/v1/model.pkl")).toBe("model.pkl");
    expect(fileName("model.pkl")).toBe("model.pkl");
  });

  it("drops a backslash's path parts and every invisible or control character", () => {
    expect(fileName("models\\v1\\model.pkl")).toBe("model.pkl");
    expect(fileName("models/evil\u202Egnp.exe")).toBe("evilgnp.exe");
    expect(fileName("models/a\u0000b\u001F\u007Fc\t\n.txt")).toBe("abc.txt");
  });

  it("names a file that has no name of its own after the download", () => {
    expect(fileName("models/")).toBe("download");
    expect(fileName("models/..")).toBe("download");
    expect(fileName("models/.")).toBe("download");
    expect(fileName("\u202E")).toBe("download");
  });
});

describe("saveBlob", () => {
  it("revokes the object URL after the click", () => {
    vi.useFakeTimers();
    const blob = new Blob(["weights"]);
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:http://localhost/1");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this);
    });

    saveBlob(blob, "model.pkl");

    expect(create).toHaveBeenCalledWith(blob);
    expect(clicked).toHaveLength(1);
    const [link] = clicked as [HTMLAnchorElement];
    expect(link.href).toBe("blob:http://localhost/1");
    expect(link.download).toBe("model.pkl");
    // Detached: the page never holds the link.
    expect(link.isConnected).toBe(false);
    expect(revoke).not.toHaveBeenCalled();

    vi.advanceTimersByTime(REVOKE_AFTER_MS);

    expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:http://localhost/1");
  });
});
