export type Health = { status: "ok"; api_version: "v1" };

export async function readHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch("/api/v1/health", { signal, cache: "no-store" });
  if (!response.ok) throw new Error("后端健康检查失败");
  const value = await response.json();
  if (value?.status !== "ok" || value?.api_version !== "v1") {
    throw new Error("健康检查响应格式不兼容");
  }
  return { status: "ok", api_version: "v1" };
}
