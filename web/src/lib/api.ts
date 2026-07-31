export interface ApiErrorBody {
  error?: { code: string; message: string };
}

export class ApiError extends Error {
  code: string;
  status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type Listener = () => void;

const unauthorizedListeners: Listener[] = [];
const passwordChangeListeners: Listener[] = [];

export function onUnauthorized(fn: Listener): () => void {
  unauthorizedListeners.push(fn);
  return () => unauthorizedListeners.splice(unauthorizedListeners.indexOf(fn), 1);
}

export function onPasswordChangeRequired(fn: Listener): () => void {
  passwordChangeListeners.push(fn);
  return () => passwordChangeListeners.splice(passwordChangeListeners.indexOf(fn), 1);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (response.status === 204) {
    return undefined as T;
  }

  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    const errBody = (body as ApiErrorBody) ?? {};
    const code = errBody.error?.code ?? "error";
    const message = errBody.error?.message ?? response.statusText ?? "Request failed";

    if (response.status === 401) {
      unauthorizedListeners.forEach((fn) => fn());
    } else if (response.status === 409 && code === "password_change_required") {
      passwordChangeListeners.forEach((fn) => fn());
    }
    throw new ApiError(response.status, code, message);
  }

  return body as T;
}

function get<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}
function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined });
}
function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body: body !== undefined ? JSON.stringify(body) : undefined });
}
function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PUT", body: body !== undefined ? JSON.stringify(body) : undefined });
}
function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

// ------------------------------------------------------------------ //
// Types
// ------------------------------------------------------------------ //

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  workspace_slug: string;
  is_admin: boolean;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
}

export interface McpToken {
  id: number;
  user_id: number;
  token_prefix: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface Integration {
  id: number;
  user_id: number;
  provider: "gmail" | "zoho" | "imap";
  account_key: string;
  account_label: string | null;
  cache_account_key: string;
  enabled: boolean;
  triage_enabled: boolean;
  archive_enabled: boolean;
  auth_type: "oauth" | "password";
  config: Record<string, unknown>;
  scopes: string | null;
  token_expires_at: string | null;
  status: "unverified" | "ok" | "reauth_required" | "error";
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_error: string | null;
  created_at: string;
  updated_at: string;
  has_secret: boolean;
  secret_preview?: string | null;
}

export interface ProviderSpec {
  id: string;
  label: string;
  auth_type: "oauth" | "password";
  available: boolean;
  unavailable_reason: string | null;
}

export interface SettingEntry {
  value: unknown;
  type: "str" | "int" | "float" | "bool" | "json";
  is_secret: boolean;
  set: boolean;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

// ------------------------------------------------------------------ //
// Auth
// ------------------------------------------------------------------ //

export const auth = {
  login: (username: string, password: string) =>
    post<{ user: User; must_change_password: boolean }>("/api/auth/login", { username, password }),
  logout: () => post<{ ok: true }>("/api/auth/logout"),
  me: () => get<User>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    post<{ ok: true; revoked_sessions: number }>("/api/auth/change-password", { current_password, new_password }),
};

// ------------------------------------------------------------------ //
// Self-service MCP tokens
// ------------------------------------------------------------------ //

export const mcpTokens = {
  list: () => get<{ tokens: McpToken[] }>("/api/me/mcp-tokens"),
  create: (label?: string) => post<McpToken & { token: string }>("/api/me/mcp-tokens", { label }),
  revoke: (id: number) => del<{ ok: true }>(`/api/me/mcp-tokens/${id}`),
};

// ------------------------------------------------------------------ //
// Integrations
// ------------------------------------------------------------------ //

export const integrations = {
  providers: () => get<{ providers: ProviderSpec[] }>("/api/integrations/providers"),
  list: (userId?: number) =>
    get<{ integrations: Integration[] }>(userId ? `/api/integrations?user_id=${userId}` : "/api/integrations"),
  createImap: (payload: {
    account_key: string;
    account_label?: string;
    config: Record<string, unknown>;
    secret: Record<string, unknown>;
  }) => post<Integration>("/api/integrations", { provider: "imap", ...payload }),
  update: (id: number, payload: Partial<Pick<Integration, "account_label" | "enabled" | "triage_enabled" | "archive_enabled">> & {
    config?: Record<string, unknown>;
    secret?: Record<string, unknown>;
  }) => patch<Integration>(`/api/integrations/${id}`, payload),
  remove: (id: number) => del<{ ok: true }>(`/api/integrations/${id}`),
  test: (id: number) => post<{ ok: boolean; error: string | null }>(`/api/integrations/${id}/test`),
  oauthStartUrl: (provider: string) => `/api/integrations/oauth/${provider}/start`,
  startOAuth: (provider: string) => get<{ authorize_url: string }>(`/api/integrations/oauth/${provider}/start`),
};

// ------------------------------------------------------------------ //
// Admin: users
// ------------------------------------------------------------------ //

export const users = {
  list: (opts?: { includeInactive?: boolean; page?: number; pageSize?: number }) => {
    const params = new URLSearchParams();
    if (opts?.includeInactive) params.set("include_inactive", "1");
    if (opts?.page) params.set("page", String(opts.page));
    if (opts?.pageSize) params.set("page_size", String(opts.pageSize));
    const qs = params.toString();
    return get<Page<User>>(`/api/users${qs ? `?${qs}` : ""}`);
  },
  create: (payload: { username: string; password: string; display_name?: string; is_admin?: boolean }) =>
    post<User>("/api/users", payload),
  update: (id: number, payload: Partial<Pick<User, "display_name" | "is_admin" | "is_active">>) =>
    patch<User>(`/api/users/${id}`, payload),
  resetPassword: (id: number, newPassword?: string) =>
    post<{ user: User; temporary_password: string }>(`/api/users/${id}/reset-password`, { new_password: newPassword }),
  remove: (id: number) => del<{ ok: true; deactivated: true }>(`/api/users/${id}`),
};

// ------------------------------------------------------------------ //
// Global settings
// ------------------------------------------------------------------ //

export const settings = {
  get: () => get<{ settings: Record<string, SettingEntry> }>("/api/settings"),
  put: (values: Record<string, unknown>) => put<{ ok: true; updated: string[] }>("/api/settings", { values }),
};

// ------------------------------------------------------------------ //
// Admin: LLM prompts
// ------------------------------------------------------------------ //

export interface PromptEntry {
  label: string;
  description: string;
  value: string;
  source: "database" | "prompts.yml" | "default";
}

export const prompts = {
  list: () => get<{ prompts: Record<string, PromptEntry> }>("/api/prompts"),
  update: (key: string, value: string) => put<PromptEntry>(`/api/prompts/${key}`, { value }),
  reset: (key: string) => post<PromptEntry>(`/api/prompts/${key}/reset`),
};

// ------------------------------------------------------------------ //
// Dashboard status / sync / logs
// ------------------------------------------------------------------ //

export interface DashboardStatus {
  scheduler: { enabled: boolean; interval: string; interval_seconds: number };
  download_all_scheduler: { enabled: boolean; interval: string; interval_seconds: number };
  token_stats: {
    tei_enabled: boolean;
    daily: { day: string; input_tokens: number; output_tokens: number; tei_saved_tokens: number }[];
  };
  profiles: Record<string, any>;
}

export const dashboard = {
  status: (all?: boolean) => get<DashboardStatus>(all ? "/api/status?all=1" : "/api/status"),
  syncStart: (profile: string) => post<{ status: string; profile: string }>(`/api/sync/start?profile=${encodeURIComponent(profile)}`),
  syncStop: (profile: string) => post<{ status: string; profile: string }>(`/api/sync/stop?profile=${encodeURIComponent(profile)}`),
  downloadAllStart: (profile: string) =>
    post<{ status: string; profile: string }>(`/api/download_all/start?profile=${encodeURIComponent(profile)}`),
};

export const logsApi = {
  since: (seq: number) => get<{ logs: { seq: number; line: string }[]; last_seq: number }>(`/api/logs?since=${seq}`),
};

// ------------------------------------------------------------------ //
// Admin: production quality check ("no-look" nightly audit)
// ------------------------------------------------------------------ //

export interface QualityTrendDay {
  day: string;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  summary_quality_avg: number | null;
  sample_size: number;
  run_count: number;
  error_count: number;
  no_data_count: number;
}

export interface QualityRun {
  id: number;
  user_id: number;
  username: string;
  account: string;
  window_start: string;
  window_end: string;
  sample_rate: number;
  population_size: number;
  sample_size: number;
  judge_model: string | null;
  level_precision: number | null;
  level_recall: number | null;
  level_f1: number | null;
  summary_quality_avg: number | null;
  summary_quality_count: number;
  status: "ok" | "error" | "no_data";
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export const quality = {
  trend: (days = 7) => get<{ days: QualityTrendDay[] }>(`/api/quality/trend?days=${days}`),
  runs: (days = 7) => get<{ runs: QualityRun[] }>(`/api/quality/runs?days=${days}`),
  runNow: () => post<{ status: string; reason?: string }>("/api/quality/run-now"),
};

export { get, post, patch, put, del };
