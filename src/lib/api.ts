export async function api<T = unknown>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (response.status === 401 && path !== "/signin") {
    window.location.assign("/signin");
    throw new Error("登录已过期，请重新登录");
  }
  const data = await response.json();
  if (!response.ok || data.error)
    throw new Error(
      typeof data.error === "string"
        ? data.error
        : data.error?.message || `请求失败 (${response.status})`,
    );
  return data;
}
export const message = (error: unknown) =>
  error instanceof Error ? error.message : "操作失败，请重试";
export async function copy(text: string) {
  await navigator.clipboard.writeText(text);
}
export type Usage = {
  input_tokens: number;
  output_tokens: number;
  thinking_tokens: number;
  cache_read_tokens: number;
  total_tokens: number;
};
export type Model = {
  model: string;
  label?: string;
  requests: number;
  usage: Usage;
};
export type Key = {
  id: string;
  name: string;
  masked: string;
  requests: number;
  usage: Usage;
  created_at: number;
  last_used_at?: number;
};
export type Recent = {
  ts: number;
  model: string;
  key_name: string;
  status: string;
  duration_ms: number;
  usage: Usage;
};
export type Dashboard = {
  totals: { requests: number; usage: Usage };
  models: Model[];
  keys: Key[];
  recent: Recent[];
  recent_total: number;
  model_count: number;
};
export const zero: Usage = {
  input_tokens: 0,
  output_tokens: 0,
  thinking_tokens: 0,
  cache_read_tokens: 0,
  total_tokens: 0,
};
export const number = (n = 0) =>
  new Intl.NumberFormat("zh-CN", {
    notation: n >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(n);
export const date = (n?: number) =>
  n
    ? new Date(n * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "尚未使用";
