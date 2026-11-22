import { afterEach, expect, it, vi } from "vitest";
import { readHealth } from "../src/api/health";

afterEach(() => vi.unstubAllGlobals());

it("reads the backend v1 contract through the same origin", async () => {
  const body = { status: "ok", api_version: "v1" };
  const request = vi.fn().mockResolvedValue(Response.json(body));
  vi.stubGlobal("fetch", request);
  await expect(readHealth()).resolves.toEqual(body);
  expect(request).toHaveBeenCalledWith("/api/v1/health", { signal: undefined, cache: "no-store" });
});

it("rejects an incompatible response", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "ok" })));
  await expect(readHealth()).rejects.toThrow("格式不兼容");
});
