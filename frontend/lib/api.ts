export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const SERVICE_TOKEN = process.env.NEXT_PUBLIC_API_TOKEN;
export const TOKEN_KEY = "revenueos.token";

/** The signed-in user's session token, else the deployment's service token (single-company setups). */
export function authToken(): string | undefined {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? SERVICE_TOKEN;
  } catch {
    return SERVICE_TOKEN;
  }
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const token = authToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")
      && !window.location.pathname.startsWith("/embed")) {
    window.location.href = "/login";
  }
  if (!res.ok) throw new Error(data.detail ? String(data.detail) : `${res.status} ${res.statusText}`);
  return data as T;
}

export const post = <T = any>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

/** Download a generated .pptx/.docx (fetch + blob so the auth header is sent). */
export async function downloadDocument(docId: number, fileName: string) {
  const token = authToken();
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${API_URL}/api/documents/${docId}/file`, { headers });
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: fileName });
  a.click();
  URL.revokeObjectURL(url);
}

export type SalesDoc = {
  id: number;
  kind: string;
  title: string;
  file_name: string | null;
  created_at: string;
  content?: string;
};

/** Poll an agent run until it finishes. */
export async function waitForRun(runId: number, onTick?: (run: Run) => void): Promise<Run> {
  for (;;) {
    const run = await api<Run>(`/api/runs/${runId}`);
    onTick?.(run);
    if (run.status !== "running") return run;
    await new Promise((r) => setTimeout(r, 2500));
  }
}

export type Run = {
  id: number;
  agent: string;
  status: "running" | "succeeded" | "failed";
  input: string;
  output: string | null;
  error: string | null;
  cost_usd: number;
  tool_calls: { tool: string; input: unknown }[];
  children?: Run[];
};

export type Lead = {
  contact_id: number;
  name: string;
  title: string | null;
  email: string | null;
  status: string;
  score: number | null;
  tier: string | null;
  source: string;
  account_id: number;
  account: string;
  has_brief?: boolean;
};

export type Flag = { severity: "block" | "warn"; code: string; message: string };

export type Approval = {
  id: number;
  kind: string;
  status: string;
  content_type: string | null;
  title: string | null;
  campaign_id: number | null;
  subject: string | null;
  body: string;
  sequence_step: number;
  send_after_days: number;
  variant: string;
  rationale: string | null;
  guardrail_flags: Flag[];
  personalisation_score: number | null;
  claims: { text: string; source: string }[] | null;
  personalisation_notes: { specific_facts_used?: string[]; generic_phrases?: string[]; feedback?: string } | null;
  created_by_agent: string;
  review_note: string | null;
  contact: { id: number; name: string; title: string | null; email: string | null; account: string; score: number | null; tier: string | null } | null;
};
