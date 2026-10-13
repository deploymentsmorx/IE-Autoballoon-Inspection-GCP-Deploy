import { isDesktopApp } from "./device";

const ENV_API = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") || "";
const LOCAL_API_FALLBACKS = ["http://127.0.0.1:9686", "http://localhost:9686"];
const isLocalApiUrl = (url: string) => /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(url);
/** In Vite dev, prefer same-origin `/api` (proxied to 9686) to avoid CORS / false "unreachable". */
const API_CANDIDATES = Array.from(
  new Set(
    [
      ...(import.meta.env.DEV ? [""] : []),
      ENV_API,
      // Packaged cloud EXE must not fall back to localhost — JWT secrets differ → "Invalid token".
      ...(import.meta.env.DEV || !ENV_API || isLocalApiUrl(ENV_API) ? LOCAL_API_FALLBACKS : []),
    ].filter((b) => b !== undefined && b !== null && (b === "" || Boolean(b)))));
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

/** Wait until the server finishes — no client-side request timeout. */
async function fetchNoTimeout(url: string, init: RequestInit = {}): Promise<Response> {
  return await fetch(url, init);
}

async function fetchApi(path: string, init: RequestInit = {}): Promise<Response> {
  // Always retry the currently-pinned base first. Without this, every call restarted
  // from API_CANDIDATES[0] (the cloud URL), so a single transient blip against the
  // cloud backend would silently redirect that one request to a local backend
  // (e.g. a dev server left running on :9686) while later requests went back to
  // the cloud — splitting one job's requests across two different backends/job stores.
  // Never hop backends once logged in — tokens are signed per-server (JWT secret).
  const tryOrder = authToken()
    ? [API]
    : [API, ...API_CANDIDATES.filter((b) => b !== API)];
  let lastErr: unknown = null;
  for (const base of tryOrder) {
    try {
      const res = await fetchNoTimeout(`${base}${path}`, init);
      if (base !== API) {
        console.warn(
          `[api] Switched backend from "${API || "(unset)"}" to "${base || "(same-origin)"}" ` +
            `after a request failed to reach the previous one. Jobs created before this point ` +
            `will not exist on the new backend.`);
      }
      API = base || API;
      return res;
    } catch (e) {
      lastErr = e;
    }
  }
  if (lastErr instanceof Error && lastErr.message.includes("Cannot reach API")) {
    throw lastErr;
  }
  throw new Error(apiUnreachableMessage());
}

async function req<T>(path: string, init: RequestInit & { operation?: string } = {}): Promise<T> {
  const { operation: _operation, ...rest } = init;
  const headers = {
    ...(rest.headers || {}),
    ...authHeaders()} as Record<string, string>;
  if (rest.body && !(rest.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  let res: Response;
  try {
    res = await fetchApi(path, { ...rest, headers });
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
  if (msg.includes("Cannot reach API") || msg.includes("Save cancelled")) {
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
  init: RequestInit = {}): Promise<Blob> {
  const rest = init;
  const headers = {
    ...(rest.headers || {}),
    ...authHeaders()} as Record<string, string>;
  if (rest.body && !(rest.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let res: Response;
  try {
    if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://")) {
      res = await fetchNoTimeout(pathOrUrl, { ...rest, headers });
    } else {
      res = await fetchApi(pathOrUrl, { ...rest, headers });
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
              "application/octet-stream": [".*"]}},
        ]});
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
  login: (username: string, password: string, device?: { device_id: string; hostname: string }) =>
    req<{ token: string; user: User }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password, ...device })}),
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
      body: JSON.stringify(body)}),
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
      body: JSON.stringify({ current_password, new_password })}),
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
    }>) =>
    req<Customer>(`/api/admin/customers/${customerId}`, {
      method: "PATCH",
      body: JSON.stringify(body)}),
  listApprovedDevices: (customerId: string) =>
    req<ApprovedDevice[]>(`/api/admin/customers/${customerId}/devices`),
  addApprovedDevice: (customerId: string, device_id: string, hostname: string) =>
    req<ApprovedDevice>(`/api/admin/customers/${customerId}/devices`, {
      method: "POST",
      body: JSON.stringify({ device_id, hostname })}),
  deleteApprovedDevice: (customerId: string, deviceRowId: string) =>
    req<{ ok: boolean }>(`/api/admin/customers/${customerId}/devices/${deviceRowId}`, {
      method: "DELETE"}),
  listInstallations: (customerId?: string) =>
    req<Installation[]>(
      `/api/admin/installations${customerId ? `?customer_id=${encodeURIComponent(customerId)}` : ""}`),
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
      { method: "POST", body: JSON.stringify(body) }),
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
      body: fd});
  },
  runBalloon: (jobId: string, fileIndex = 0, numberingMode: "continuous" | "per_page" = "continuous") =>
    req<{ count: number; balloons: Balloon[]; pass_log: unknown[]; page_count?: number }>(
      `/api/jobs/${jobId}/balloon?file_index=${fileIndex}&passes=1&use_tiles=true&numbering_mode=${numberingMode}`,
      { method: "POST" }),
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
      method: "POST"}),
  addMissingDims: (jobId: string, items: MissingDim[]) =>
    req<{ count: number; balloons: Balloon[]; missing_count: number; missing: MissingDim[] }>(
      `/api/jobs/${jobId}/audit/add-missing`,
      { method: "POST", body: JSON.stringify({ items }) }),
  getJob: (jobId: string, page = 0) => req<Job>(`/api/jobs/${jobId}?page=${page}`),
  getPageGeometry: (jobId: string, page = 0) =>
    req<PageGeometry>(`/api/jobs/${jobId}/page-geometry?page=${page}`),
  saveBalloons: (jobId: string, balloons: Balloon[], opts?: { redrawPdf?: boolean }) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(
      `/api/jobs/${jobId}/balloons?redraw_pdf=${opts?.redrawPdf !== false}`,
      {
        method: "PUT",
        body: JSON.stringify({ balloons })}),
  createBalloon: async (
    jobId: string,
    data: {
      page: number;
      text: string;
      x: number;
      y: number;
      width?: number;
      height?: number;
      balloon?: number;
    }) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[]; created?: number }>(`/api/jobs/${jobId}/balloons/create`, {
      method: "POST",
      body: (() => {
        const fd = new FormData();
        fd.append("page", String(data.page));
        fd.append("text", data.text);
        fd.append("x", String(data.x));
        fd.append("y", String(data.y));
        fd.append("width", String(data.width ?? 24));
        fd.append("height", String(data.height ?? 14));
        if (data.balloon != null && data.balloon > 0) {
          fd.append("balloon_no", String(data.balloon));
        }
        return fd;
      })()}),
  resequenceBalloons: (jobId: string) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(
      `/api/jobs/${jobId}/balloons/resequence`,
      { method: "POST" },
    ),
  deleteBalloon: (jobId: string, no: number) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(`/api/jobs/${jobId}/balloons/${no}`, {
      method: "DELETE"}),
  deleteBalloonsMany: (jobId: string, nos: number[]) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(
      `/api/jobs/${jobId}/balloons/delete-many`,
      { method: "POST", body: JSON.stringify({ balloon_numbers: nos }) },
    ),
  deletePageBalloons: (jobId: string, page: number) =>
    req<{ count: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(
      `/api/jobs/${jobId}/balloons/delete-page?page=${page}`,
      { method: "POST" },
    ),
  balloonArea: (
    jobId: string,
    body: {
      page: number;
      x: number;
      y: number;
      width: number;
      height: number;
      clear_page?: boolean;
      replace_in_area?: boolean;
    },
  ) =>
    req<{ count: number; added: number; balloons: Balloon[]; report_rows?: ReportRow[] }>(
      `/api/jobs/${jobId}/balloon/area`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  saveReport: (
    jobId: string,
    payload: ReportPayload,
    opts?: { localOnly?: boolean; headersOnly?: boolean },
  ) =>
    req(`/api/jobs/${jobId}/report?local_only=${opts?.localOnly ? "true" : "false"}&headers_only=${opts?.headersOnly ? "true" : "false"}`, {
      method: "PUT",
      body: JSON.stringify(payload)}),
  saveReportRowsChunk: (
    jobId: string,
    body: {
      rows: ReportRow[];
      offset: number;
      replace?: boolean;
      done?: boolean;
      measure_columns?: MeasureCol[];
    },
  ) =>
    req<{ ok: boolean; rows: number; offset: number }>(
      `/api/jobs/${jobId}/report/rows?local_only=true`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
  /** Save large reports in chunks so the UI stays responsive. */
  saveReportChunked: async (
    jobId: string,
    payload: ReportPayload,
    onProgress?: (msg: string) => void,
  ) => {
    const rows = payload.rows || [];
    const headers = { ...payload, rows: [] as ReportRow[] };
    onProgress?.("Saving report header…");
    await api.saveReport(jobId, headers, { localOnly: true, headersOnly: true });
    const chunkSize = 400;
    for (let offset = 0; offset < rows.length; offset += chunkSize) {
      const chunk = rows.slice(offset, offset + chunkSize);
      const done = offset + chunk.length >= rows.length;
      onProgress?.(
        `Saving rows ${offset + 1}–${offset + chunk.length} of ${rows.length}…`,
      );
      await new Promise((r) => setTimeout(r, 0));
      await api.saveReportRowsChunk(jobId, {
        rows: chunk,
        offset,
        replace: offset === 0,
        done,
        measure_columns: done ? payload.measure_columns : undefined,
      });
    }
    if (!rows.length) {
      await api.saveReportRowsChunk(jobId, {
        rows: [],
        offset: 0,
        replace: true,
        done: true,
        measure_columns: payload.measure_columns,
      });
    }
  },
  rebuildReport: (jobId: string) =>
    req<{ count: number; rows: ReportRow[] }>(`/api/jobs/${jobId}/report/rebuild`, {
      method: "POST"}),
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
    }>(`/api/jobs/${jobId}/title-extract`, { method: "POST"}),
  previewInputUrl: (jobId: string, page = 0, rev = 0) =>
    `${API}/api/jobs/${jobId}/preview/input?page=${page}&v=${rev}`,
  previewBalloonUrl: (jobId: string, page = 0, rev = 0) =>
    `${API}/api/jobs/${jobId}/preview/balloon?page=${page}&v=${rev}`,
  downloadBallooned: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/ballooned`),
  downloadSourceUrl: (jobId: string) =>
    withAuthQuery(`/api/jobs/${jobId}/download/source`),
  downloadReportPdf: (jobId: string, filter?: ReportDownloadFilter) =>
    withAuthQuery(`/api/jobs/${jobId}/report/pdf${reportDownloadQuery(filter)}`),
  downloadExcel: (jobId: string, filter?: ReportDownloadFilter) =>
    withAuthQuery(`/api/jobs/${jobId}/report/excel${reportDownloadQuery(filter)}`),
  downloadTemplateReport: (
    jobId: string,
    kind: "master" | "cmq",
    fmt: "pdf" | "excel",
    filter?: ReportDownloadFilter,
  ) => withAuthQuery(`/api/jobs/${jobId}/report/${kind}/${fmt}${reportDownloadQuery(filter)}`),
  downloadCsv: (jobId: string, filter?: ReportDownloadFilter) =>
    withAuthQuery(`/api/jobs/${jobId}/report/csv${reportDownloadQuery(filter)}`),
  downloadJson: (jobId: string, filter?: ReportDownloadFilter) =>
    withAuthQuery(`/api/jobs/${jobId}/report/json${reportDownloadQuery(filter)}`),
  /**
   * Fast download: save headers only (rows already on server), then GET PDF.
   * Pass rowsDirty=true to upload row edits in chunks first.
   */
  downloadReportPdfFile: async (
    jobId: string,
    payload: ReportPayload,
    suggestedName?: string,
    opts?: {
      rowsDirty?: boolean;
      skipSave?: boolean;
      onProgress?: (msg: string) => void;
      filter?: ReportDownloadFilter;
    },
  ) => {
    const filename = suggestedName || `${jobId}_inspection_report.pdf`;
    const onProgress = opts?.onProgress;
    if (!opts?.skipSave) {
      if (opts?.rowsDirty) {
        await api.saveReportChunked(jobId, payload, onProgress);
      } else {
        onProgress?.("Saving report header…");
        const headersOnly = { ...payload, rows: [] as ReportRow[] };
        await api.saveReport(jobId, headersOnly, { localOnly: true, headersOnly: true });
      }
    }
    onProgress?.(
      `Building PDF (${payload.rows?.length || 0} rows)… window stays responsive — please wait`,
    );
    await new Promise((r) => setTimeout(r, 40));
    return api.downloadFile(api.downloadReportPdf(jobId, opts?.filter), filename);
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
        authorization: token ? `Bearer ${token}` : null});
      if (!savedPath) throw new Error("Save returned no path");
      return savedPath;
    }
    const blob = await fetchBlob(url);
    if (!blob || blob.size === 0) {
      throw new Error("Download returned an empty file — run auto ballooning / rebuild report first.");
    }
    return saveBlob(blob, filename);
  }};

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
  /** Arrow tip (PDF space). When unset, leader joins dim centre. */
  tx?: number | null;
  ty?: number | null;
  /** Arrowhead size scale (1 = default). */
  arrow_size?: number | null;
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
  page?: number;
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

export type ReportDownloadFilter = {
  page?: number;
  area?: { page: number; x: number; y: number; width: number; height: number };
};

function reportDownloadQuery(filter?: ReportDownloadFilter): string {
  if (!filter) return "";
  const q = new URLSearchParams();
  if (filter.page) q.set("page", String(filter.page));
  if (filter.area) {
    q.set("area_page", String(filter.area.page));
    q.set("area_x", String(filter.area.x));
    q.set("area_y", String(filter.area.y));
    q.set("area_w", String(filter.area.width));
    q.set("area_h", String(filter.area.height));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function reportFilterSuffix(filter?: ReportDownloadFilter): string {
  if (!filter) return "";
  const parts: string[] = [];
  if (filter.page) parts.push(`_p${filter.page}`);
  if (filter.area) parts.push("_area");
  return parts.join("");
}

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
  master_fields?: Record<string, string>;
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
  master_fields?: Record<string, string>;
  page_geometry?: PageGeometry;
  balloon_numbering?: "continuous" | "per_page";
  status: string;
  page_count?: number;
};

export function measureStatus(
  nominal: string,
  tolLow: string,
  tolHigh: string,
  measured: string): "green" | "yellow" | "red" | "empty" {
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
