import { describe, expect, it } from "vitest";
import { makeEventsKey } from "./queryCache";

describe("makeEventsKey", () => {
  it("keys by window only", () => {
    expect(makeEventsKey(1000, 2000)).toBe("events|1000|2000");
    expect(makeEventsKey(1000, 2000)).not.toBe(makeEventsKey(1000, 2001));
  });
});
