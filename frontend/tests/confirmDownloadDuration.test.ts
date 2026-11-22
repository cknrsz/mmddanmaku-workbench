import { expect, it, vi } from "vitest";
import { confirmDownloadDuration } from "../src/confirmDownloadDuration";

it.each([0, 999, 1000, -1000])("allows a difference of %i ms without confirmation", (difference) => {
  const confirm = vi.fn();
  expect(confirmDownloadDuration(60_000 + difference, 60_000, confirm)).toBe(true);
  expect(confirm).not.toHaveBeenCalled();
});

it.each([[1001, true], [-1001, false]] as const)("uses the confirmation result for a difference of %i ms",
  (difference, accepted) => {
    const confirm = vi.fn(() => accepted);
    expect(confirmDownloadDuration(60_000 + difference, 60_000, confirm)).toBe(accepted);
    expect(confirm).toHaveBeenCalledOnce();
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("相差 1.001 秒。\n是否继续下载弹幕？"));
  });
