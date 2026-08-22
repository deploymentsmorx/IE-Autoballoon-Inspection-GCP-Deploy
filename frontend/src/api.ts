const ENV_API = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") || "";
/** In Vite dev, prefer same-origin `/api` (proxied to 9686) to avoid CORS / false "unreachable". */
const API_CANDIDATES = Array.from(
  new Set(
    [
      ...(import.meta.env.DEV ? [""] : []),
      ENV_API,
      "http://127.0.0.1:9686",
      "http://localhost:9686",
    ].filter((b) => b !== undefined && b !== null && (b === "" || Boolean(b))),
  ),
);
let API = API_CANDIDATES[0] || "http://127.0.0.1:9686";

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

function apiUnreachableMessage(detail?: string) {
  const where = API || "http://127.0.0.1:9686";
  const extra = detail ? ` (${detail})` : "";
  return `Cannot reach API at ${where}${extra}. Start backend first: backend\\start-api.bat (port 9686), keep that window open, then try again.`;
}

function timeoutMessage(ms: number, operation?: string) {
  const sec = Math.round(ms / 1000);
  const op = operation ? ` (${operation})` : "";
  return (
    `Request timed out after ${sec}s${op}. ` +
    `The backend did not respond in time — usually the drawing is still processing or the API stopped. ` +
    `Keep backend\\start-api.bat open and watch that window for errors. ` +
    `Large drawings often finish in 1–3 minutes; if it keeps timing out, restart the API and try again.`
  );
}

/** Fail fast when the API is down (avoids endless "Loading…" in the EXE). */
async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  ms = 20000,
  operation?: string,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error(timeoutMessage(ms, operation));
    }
    throw e;
  } finally {
    window.clearTimeout(timer);
  }
}

async function fetchApi(
  path: string,
  init: RequestInit = {},
  ms = 20000,
  operation?: string,
): Promise<Response> {
  // Always retry the currently-pinned base first. Without this, every call restarted
  // from API_CANDIDATES[0] (the cloud URL), so a single transient blip against the
  // cloud backend would silently redirect that one request to a local backend
  // (e.g. a dev server left running on :9686) while later requests went back to
  // the cloud — splitting one job's requests across two different backends/job stores.
  const tryOrder = [API, ...API_CANDIDATES.filter((b) => b !== API)];
  let lastErr: unknown = null;
  for (const base of tryOrder) {
    try {
      const res = await fetchWithTimeout(`${base}${path}`, init, ms, operation);
      if (base !== API) {
        console.warn(
          `[api] Switched backend from "${API || "(unset)"}" to "${base || "(same-origin)"}" ` +
            `after a request failed to reach the previous one. Jobs created before this point ` +
            `will not exist on the new backend.`,
        );
      }
      API = base || API;
      return res;
    } catch (e) {
      lastErr = e;
      const msg = e instanceof Error ? e.message : String(e || "");
      // Do not retry other hosts after a timeout — the server may still be working,
      // and a second balloon/audit request would make things worse.
      if (msg.includes("timed out")) {
        throw e;
      }
    }
  }
  if (lastErr instanceof Error) {
    if (lastErr.message.includes("timed out") || lastErr.message.includes("Cannot reach API")) {
      throw lastErr;
    }
  }
  throw new Error(apiUnreachableMessage());
}

async function req<T>(path: string, init: RequestInit & { timeoutMs?: number; operation?: string } = {}): Promise<T> {
  const { timeoutMs, operation, ...rest } = init;
  const headers = {
    ...(rest.headers || {}),
    ...authHeaders(),
  } as Record<string, string>;
  if (rest.body && !(rest.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  let res: Response;
  try {
    res = await fetchApi(path, { ...rest, headers }, timeoutMs ?? 20000, operation);
  } catch (e) {
    rethrowFetchError(e);
  }
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

function rethrowFetchError(e: unknown): never {
  const msg = e instanceof Error ? e.message : String(e || "");
  if (
    msg.includes("timed out") ||
    msg.includes("Cannot reach API") ||
    msg.includes("Save cancelled")
  ) {
    throw new Error(msg);
  }
  if (
    e instanceof TypeError ||
    msg.includes("Failed to fetch") ||
    msg.includes("NetworkError") ||
    msg.includes("Load failed") ||
    msg.includes("private-network") ||
    msg.includes("Private Network")
  ) {
    throw new Error(apiUnreachableMessage(msg));
  }
  throw new Error(msg || apiUnreachableMessage());
}

/** Fetch a file as Blob (Bearer + clearer errors). */
async function fetchBlob(
  pathOrUrl: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<Blob> {
  const { timeoutMs = 180000, ...rest } = init;
  const headers = {
    ...(rest.headers || {}),
    ...authHeaders(),
  } as Record<string, string>;
  if (rest.body && !(rest.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let res: Response;
  try {
    if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://")) {
      res = await fetchWithTimeout(pathOrUrl, { ...rest, headers }, timeoutMs);
    } else {
      res = await fetchApi(pathOrUrl, { ...rest, headers }, timeoutMs);
    }
  } catch (e) {
    rethrowFetchError(e);
  }
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
}

/** True when running inside the Tauri desktop EXE (WebView2). */
async function isDesktopApp(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  if ("__TAURI_INTERNALS__" in window || "__TAURI__" in window) return true;
  try {
    const { isTauri } = await import("@tauri-apps/api/core");
    return await isTauri();
  } catch {
    return false;
  }
}

/**
 * Save a blob to disk.
 * Desktop EXE: write into Windows Downloads via Tauri (WebView <a download> often does nothing).
 * Browser: Save dialog when available, else normal download.
 */
async function saveBlob(blob: Blob, filename: string): Promise<string> {
  if (await isDesktopApp()) {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const bytes = Array.from(new Uint8Array(await blob.arrayBuffer()));
      const savedPath = await invoke<string>("save_download", { filename, bytes });
      if (!savedPath) throw new Error("Save returned no path");
      return savedPath;
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e || "");
      throw new Error(msg || "Could not save file to Downloads");
    }
  }

  const w = window as Window & {
    showSaveFilePicker?: (opts: {
      suggestedName?: string;
      types?: { description: string; accept: Record<string, string[]> }[];
    }) => Promise<{ createWritable: () => Promise<{ write: (b: Blob) => Promise<void>; close: () => Promise<void> }> }>;
  };

  if (typeof w.showSaveFilePicker === "function") {
    try {
      const handle = await w.showSaveFilePicker({
        suggestedName: filename,
        types: [
          {
            description: "Download",
            accept: {
              "application/pdf": [".pdf"],
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
              "application/octet-stream": [".*"],
            },
          },
        ],
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return filename;
    } catch (e: unknown) {
      if (e && typeof e === "object" && "name" in e && (e as { name: string }).name === "AbortError") {
        throw new Error("Save cancelled");
      }
    }
  }

  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  return filename;
}

export const api = {
  apiBase: API,
  login: (username: string, password: string) =>
    req<{ token: string; user: User }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  activate: (body: {
    license_key: string;
    username: string;
    password: string;
    device_id: string;
    hostname: string;
    install_password: string;
  }) =>
    req<{
      token: string;
      customer: {
        customer_id: string;
        organization: string;
        license_key: string;
        status: string;
      };
      installation: {
        installation_id: string;
        device_id: string;
        hostname: string;
        status: string;
        activated_at: string;
      };
      user: { id: string; username: string; role: string; must_change_password?: boolean };
    }>("/api/auth/activate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deviceStatus: (deviceId: string) =>
    req<{
      activated: boolean;
      license_key?: string;
      customer_id?: string;
      installation_id?: string;
    }>(`/api/auth/device-status?device_id=${encodeURIComponent(deviceId)}`),
  changePassword: (current_password: string, new_password: string) =>
    req<{ ok: boolean }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  me: () => req<User>("/api/auth/me"),
  listCustomers: () => req<Customer[]>("/api/admin/customers"),
  createCustomer: (body: {
    organization: string;
    license_key?: string;
    org_id?: string;
    install_password?: string;
    require_device_approval?: boolean;
  }) =>
    req<Customer>("/api/admin/customers", { method: "POST", body: JSON.stringify(body) }),
  updateCustomer: (
    customerId: string,
    body: Partial<{
      status: string;
      install_password: string;
      require_device_approval: boolean;
      org_id: string;
    }>,
  ) =>
    req<Customer>(`/api/admin/customers/${customerId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listApprovedDevices: (customerId: string) =>
    req<ApprovedDevice[]>(`/api/admin/customers/${customerId}/devices`),
  addApprovedDevice: (customerId: string, device_id: string, hostname: string) =>
    req<ApprovedDevice>(`/api/admin/customers/${customerId}/devices`, {
      method: "POST",
      body: JSON.stringify({ device_id, hostname }),
    }),
  deleteApprovedDevice: (customerId: string, deviceRowId: string) =>
    req<{ ok: boolean }>(`/api/admin/customers/${customerId}/devices/${deviceRowId}`, {
      method: "DELETE",
    }),
  listInstallations: (customerId?: string) =>
    req<Installation[]>(
      `/api/admin/installations${customerId ? `?customer_id=${encodeURIComponent(customerId)}` : ""}`,
    ),
  deleteEngineer: (userId: string) =>
    req<{ ok: boolean }>(`/api/admin/engineers/${userId}`, { method: "DELETE" }),
  listOrgs: () => req<Org[]>("/api/admin/organizations"),
  createOrg: (name: string) =>
    req<Org>("/api/admin/organizations", { method: "POST", body: JSON.stringify({ name }) }),
  listEngineers: () => req<Engineer[]>("/api/admin/engineers"),
  addEngineer: (body: {
    full_name: string;
    email: string;
    org_id: string;
    customer_id?: string;
    password?: string;
  }) =>
    req<{ id: string; username: string; email: string; temp_password?: string }>(
      "/api/admin/engineers",
      { method: "POST", body: JSON.stringify(body) },
    ),
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
      timeoutMs: 120000,
    });
  },
  runBalloon: (jobId: string, fileIndex = 0) =>
    req<{ count: number; balloons: Balloon[]; pass_log: unknown[]; page_count?: number }>(
      `/api/jobs/${jobId}/balloon?file_index=${fileIndex}&passes=2&use_tiles=true`,
      { method: "POST", timeoutMs: 900000, operation: "Run auto ballooning" },
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
    }>(`/api/jobs/${jobId}/audit?file_index=${fileIndex}&rounds=2&apply=true`, {
      method: "POST",
      timeoutMs: 900000,
      operation: "Audit / Cross-check",
    }),
  addMissingDims: (jobId: string, items: MissingDim[]) =>
    req<{ count: number; balloons: Balloon[]; missing_count: number; missing: MissingDim[] }>(
      `/api/jobs/${jobId}/audit/add-missing`,
      { method: "POST", body: JSON.stringify({ items }) },
    ),
  getJob: (jobId: string, page = 0) => req<Job>(`/api/jobs/${jobId}?page=${page}`),
  saveBalloons: (jobId: string, balloons: Balloon[]) =>
    req<{ count: number; balloons: Balloon[] }>(`/api/jobs/${jobId}/balloons`, {
      method: "PUT",
      body: JSON.stringify({ balloons }),
      timeoutMs: 180000,
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
    req(`/api/jobs/${jobId}/report`, {
      method: "PUT",
      body: JSON.stringify(payload),
      timeoutMs: 90000,
      operation: "Save report",
    }),
  rebuildReport: (jobId: string) =>
    req<{ count: number; rows: ReportRow[] }>(`/api/jobs/${jobId}/report/rebuild`, {
      method: "POST",
    }),
  extractTitle: (jobId: string) =>
    req<{
      part_number: string;
      part_name: string;
      revision: string;
      material: string;
      mass: string;
      finish: string;
      serial_number: string;
      report_date: string;
      company_name: string;
      supplier_name: string;
      customer_name: string;
      authorized_signatory: string;
    }>(`/api/jobs/${jobId}/title-extract`, { method: "POST", timeoutMs: 120000 }),
  previewInputUrl: (jobId: string, page = 0, rev = 0) =>
    `${API}/api/jobs/${jobId}/preview/input?page=${page}&v=${rev}`,
  previewBalloonUrl: (jobId: string, page = 0, rev = 0) =>
    `${API}/api/jobs/${jobId}/preview/balloon?page=${page}&v=${rev}`,
  downloadBallooned: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/ballooned`),
  downloadSourceUrl: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/source`),
  downloadReportPdf: (jobId: string) => withAuthQuery(`/api/jobs/${jobId}/report/pdf`),
  downloadExcel: (jobId: string) => withAuthQuery(`/api/jobs/${jobId}/report/excel`),
  /** Save report fields and download the PDF in one API call. */
  downloadReportPdfFile: async (jobId: string, payload: ReportPayload, suggestedName?: string) => {
    const filename = suggestedName || `${jobId}_inspection_report.pdf`;
    if (await isDesktopApp()) {
      const { invoke } = await import("@tauri-apps/api/core");
      const token = authToken();
      const savedPath = await invoke<string>("download_post", {
        url: `${API}/api/jobs/${jobId}/report/pdf`,
        filename,
        authorization: token ? `Bearer ${token}` : null,
        body: JSON.stringify(payload),
      });
      if (!savedPath) throw new Error("Save returned no path");
      return savedPath;
    }
    const blob = await fetchBlob(`/api/jobs/${jobId}/report/pdf`, {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: 180000,
    });
    if (!blob || blob.size === 0) {
      throw new Error("Report PDF was empty — save report data and try again.");
    }
    return saveBlob(blob, filename);
  },
  authFetch: fetchBlob,
  /** Download API file to disk (Save dialog on desktop when available). */
  downloadFile: async (url: string, filename: string) => {
    if (await isDesktopApp()) {
      const { invoke } = await import("@tauri-apps/api/core");
      const token = authToken();
      const savedPath = await invoke<string>("download_url", {
        url,
        filename,
        authorization: token ? `Bearer ${token}` : null,
      });
      if (!savedPath) throw new Error("Save returned no path");
      return savedPath;
    }
    const blob = await fetchBlob(url, { timeoutMs: 180000 });
    if (!blob || blob.size === 0) {
      throw new Error("Download returned an empty file — run auto ballooning / rebuild report first.");
    }
    return saveBlob(blob, filename);
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
  must_change_password?: boolean;
  customer_id?: string | null;
};

export type Customer = {
  customer_id: string;
  organization: string;
  license_key: string;
  status: string;
  org_id?: string | null;
  tenant_id?: string | null;
  require_device_approval: boolean;
  has_install_password: boolean;
  approved_devices: number;
  active_installations: number;
};

export type ApprovedDevice = {
  id: string;
  customer_id: string;
  device_id: string;
  hostname: string;
  status: string;
  activated_at: string;
};

export type Installation = {
  installation_id: string;
  customer_id: string;
  device_id: string;
  hostname: string;
  status: string;
  activated_at: string;
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

export type PageGeometry = {
  rotation: number;
  page_width: number;
  page_height: number;
  mediabox_width: number;
  mediabox_height: number;
  preview_scale: number;
  /** PyMuPDF matrix unrotated → display: [a,b,c,d,e,f] */
  rot: number[];
  /** display → unrotated */
  derot: number[];
};

export type ReportPayload = {
  part_number: string;
  part_name: string;
  revision: string;
  material: string;
  mass: string;
  finish: string;
  serial_number?: string;
  report_date?: string;
  company_name?: string;
  supplier_name?: string;
  customer_name?: string;
  authorized_signatory?: string;
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
  serial_number?: string;
  report_date?: string;
  company_name?: string;
  supplier_name?: string;
  customer_name?: string;
  authorized_signatory?: string;
  page_geometry?: PageGeometry;
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
