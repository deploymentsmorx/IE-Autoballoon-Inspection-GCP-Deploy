const API = import.meta.env.VITE_API_URL || "";

function authToken(): string {
  return localStorage.getItem("smorx_token") || "";
}

function authHeaders(): HeadersInit {
  const token = authToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Preview/download URLs for <img> and window.open (Bearer header not available). */
function withAuthQuery(path: string): string {
  const token = authToken();
  const sep = path.includes("?") ? "&" : "?";
  return token ? `${API}${path}${sep}token=${encodeURIComponent(token)}` : `${API}${path}`;
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = {
    ...(init.headers || {}),
    ...authHeaders(),
  } as Record<string, string>;
  if (init.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${API}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || j.message || JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res as unknown as T;
}

export const api = {
  apiBase: API,
  login: (username: string, password: string) =>
    req<{ token: string; user: User }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  me: () => req<User>("/api/auth/me"),
  listOrgs: () => req<Org[]>("/api/admin/organizations"),
  createOrg: (name: string) =>
    req<Org>("/api/admin/organizations", { method: "POST", body: JSON.stringify({ name }) }),
  listEngineers: () => req<Engineer[]>("/api/admin/engineers"),
  addEngineer: (body: { full_name: string; email: string; org_id: string; password?: string }) =>
    req("/api/admin/engineers", { method: "POST", body: JSON.stringify(body) }),
  updateAccess: (body: {
    user_id: string;
    active: boolean;
    read: boolean;
    write: boolean;
    delete: boolean;
  }) => req("/api/admin/access", { method: "PUT", body: JSON.stringify(body) }),
  grantFull: (userId: string) =>
    req(`/api/admin/access/grant-full/${userId}`, { method: "POST" }),
  uploadPdfs: async (files: FileList | File[]) => {
    const fd = new FormData();
    Array.from(files).forEach((f) => fd.append("files", f));
    return req<{
      job_id: string;
      files: { index: number; name: string; page_count?: number }[];
      page_count?: number;
      message?: string;
    }>("/api/jobs/upload", {
      method: "POST",
      body: fd,
    });
  },
  runBalloon: (jobId: string, fileIndex = 0) =>
    req<{ count: number; balloons: Balloon[]; pass_log: unknown[]; page_count?: number }>(
      `/api/jobs/${jobId}/balloon?file_index=${fileIndex}&passes=3`,
      { method: "POST" },
    ),
  auditCrosscheck: (jobId: string, fileIndex = 0) =>
    req<{
      before: number;
      added: number;
      count: number;
      missing_count: number;
      missing: MissingDim[];
      balloons: Balloon[] | null;
      pass_log: unknown[];
    }>(`/api/jobs/${jobId}/audit?file_index=${fileIndex}&rounds=3&apply=true`, { method: "POST" }),
  addMissingDims: (jobId: string, items: MissingDim[]) =>
    req<{ count: number; balloons: Balloon[]; missing_count: number; missing: MissingDim[] }>(
      `/api/jobs/${jobId}/audit/add-missing`,
      { method: "POST", body: JSON.stringify({ items }) },
    ),
  getJob: (jobId: string) => req<Job>(`/api/jobs/${jobId}`),
  saveBalloons: (jobId: string, balloons: Balloon[]) =>
    req(`/api/jobs/${jobId}/balloons`, {
      method: "PUT",
      body: JSON.stringify({ balloons }),
    }),
  createBalloon: async (
    jobId: string,
    data: { page: number; text: string; x: number; y: number; width?: number; height?: number },
  ) => {
    const fd = new FormData();
    fd.append("page", String(data.page));
    fd.append("text", data.text);
    fd.append("x", String(data.x));
    fd.append("y", String(data.y));
    fd.append("width", String(data.width ?? 24));
    fd.append("height", String(data.height ?? 14));
    return req(`/api/jobs/${jobId}/balloons/create`, { method: "POST", body: fd });
  },
  deleteBalloon: (jobId: string, no: number) =>
    req(`/api/jobs/${jobId}/balloons/${no}`, { method: "DELETE" }),
  saveReport: (jobId: string, payload: ReportPayload) =>
    req(`/api/jobs/${jobId}/report`, { method: "PUT", body: JSON.stringify(payload) }),
  rebuildReport: (jobId: string) =>
    req<{ count: number; rows: ReportRow[] }>(`/api/jobs/${jobId}/report/rebuild`, {
      method: "POST",
    }),
  previewInputUrl: (jobId: string, page = 0) =>
    `${API}/api/jobs/${jobId}/preview/input?page=${page}&t=${Date.now()}`,
  previewBalloonUrl: (jobId: string, page = 0) =>
    `${API}/api/jobs/${jobId}/preview/balloon?page=${page}&t=${Date.now()}`,
  downloadBallooned: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/ballooned`),
  downloadSourceUrl: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/source`),
  downloadReportPdf: (jobId: string) => withAuthQuery(`/api/jobs/${jobId}/report/pdf`),
  downloadExcel: (jobId: string) => withAuthQuery(`/api/jobs/${jobId}/report/excel`),
  authFetch: async (url: string) => {
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || j.message || JSON.stringify(j);
      } catch {
        /* ignore */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.blob();
  },
};

export type User = {
  id: string;
  username: string;
  email: string;
  full_name: string;
  role: string;
  permissions: { read?: boolean; write?: boolean; delete?: boolean };
  active: boolean;
};

export type Org = {
  id: string;
  name: string;
  tenant_id: string;
  engineers: number;
  status: string;
  trial_ends: string;
};

export type Engineer = {
  id: string;
  full_name: string;
  email: string;
  username: string;
  status: string;
  org_id?: string;
  active: boolean;
  permissions: { read?: boolean; write?: boolean; delete?: boolean };
};

export type Balloon = {
  balloon: number;
  page: number;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  category?: string;
  pattern?: string;
  bx?: number | null;
  by?: number | null;
};

export type MissingDim = {
  id: number;
  page: number;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  size?: number;
  zone?: string;
  pattern?: string;
  category?: string;
  reason?: string;
};

export type ReportRow = {
  sno: number;
  balloon: number;
  reference: string;
  nominal: string;
  tol_low: string;
  tol_high: string;
  instrument: string;
  unit: string;
  instr_id: string;
  measured: string[];
  remarks: string;
};

export type MeasureCol = {
  id: string;
  label: string;
  values: Record<string, string>;
};

export type ReportPayload = {
  part_number: string;
  part_name: string;
  revision: string;
  material: string;
  mass: string;
  finish: string;
  rows: ReportRow[];
  measure_columns: MeasureCol[];
};

export type Job = {
  id: string;
  files: { index: number; name: string; path: string; page_count?: number }[];
  balloons: Balloon[];
  report_rows: ReportRow[];
  measure_columns: MeasureCol[];
  missing_dimensions?: MissingDim[];
  part_number?: string;
  part_name?: string;
  revision?: string;
  material?: string;
  mass?: string;
  finish?: string;
  status: string;
  page_count?: number;
};

export function measureStatus(
  nominal: string,
  tolLow: string,
  tolHigh: string,
  measured: string,
): "green" | "yellow" | "red" | "empty" {
  const num = (s: string) => {
    const m = String(s || "").match(/[-+]?\d*\.?\d+/);
    return m ? parseFloat(m[0]) : null;
  };
  const m = num(measured);
  if (m == null) return "empty";
  const n = num(nominal);
  if (n == null) return "empty";
  if (Math.abs(m - n) < 1e-9) return "green";
  let lo = num(tolLow);
  let hi = num(tolHigh);
  if (lo == null && hi == null) return "red";
  if (lo == null && hi != null) {
    lo = -Math.abs(hi);
    hi = Math.abs(hi);
  }
  if (hi == null && lo != null) {
    hi = Math.abs(lo);
    lo = -Math.abs(lo);
  }
  const low = n + (lo ?? 0);
  const high = n + (hi ?? 0);
  const a = Math.min(low, high);
  const b = Math.max(low, high);
  if (m >= a && m <= b) return "yellow";
  return "red";
}
