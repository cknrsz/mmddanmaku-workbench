import { expect, it } from "vitest";
import { formatDuration } from "../src/formatDuration";

it("rounds up partial seconds and carries into the next minute", () => {
  expect(formatDuration(0)).toBe("0分0秒");
  expect(formatDuration(59_001)).toBe("1分0秒");
  expect(formatDuration(322_000)).toBe("5分22秒");
  expect(formatDuration(322_235)).toBe("5分23秒");
  expect(formatDuration(322_300)).toBe("5分23秒");
});
