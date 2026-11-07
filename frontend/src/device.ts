import { api } from "./api";
import { TABLET_EXE } from "./constants/branding";

const DEVICE_KEY = "smorx_device_id";
const HOST_KEY = "smorx_hostname";
const ACTIVATED_KEY = "smorx_license_activated";
const TABLET_KEY = "smorx_client";

export function isTabletClient(): boolean {
  return TABLET_EXE || localStorage.getItem(TABLET_KEY) === "tablet";
}

export function markTabletClient(): void {
  localStorage.setItem(TABLET_KEY, "tablet");
}

export function clearTabletClient(): void {
  if (TABLET_EXE) return;
  localStorage.removeItem(TABLET_KEY);
}

if (TABLET_EXE) {
  try {
    markTabletClient();
  } catch {
    /* localStorage may be unavailable during SSR */
  }
} else {
  try {
    localStorage.removeItem(TABLET_KEY);
  } catch {
    /* leftover tablet flag from an old EXE must not trap the desktop installer */
  }
}

export type DeviceInfo = {
  device_id: string;
  hostname: string;
};

function randomToken(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID().replace(/-/g, "").slice(0, 16).toUpperCase();
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`.toUpperCase();
}

function isNativeAndroid(): boolean {
  if (typeof window === "undefined") return false;
  const cap = (window as unknown as { Capacitor?: { isNativePlatform?: () => boolean; getPlatform?: () => string } })
    .Capacitor;
  try {
    return Boolean(cap?.isNativePlatform?.() && cap.getPlatform?.() === "android");
  } catch {
    return false;
  }
}

function fallbackDeviceId(): DeviceInfo {
  let id = localStorage.getItem(DEVICE_KEY);
  if (!id) {
    id = isNativeAndroid() ? `DEV-ANDROID-${randomToken()}` : `DEV-WEB-${randomToken()}`;
    localStorage.setItem(DEVICE_KEY, id);
  }
  const hostname = localStorage.getItem(HOST_KEY) || (isNativeAndroid() ? "android-tablet" : "browser");
  return { device_id: id, hostname };
}

/** Stable device id: Windows MachineGuid via Tauri, else a saved browser id. */
export async function getDeviceInfo(): Promise<DeviceInfo> {
  const inDesktop =
    typeof window !== "undefined" &&
    ("__TAURI_INTERNALS__" in window || "__TAURI__" in window);
  if (!inDesktop) return fallbackDeviceId();
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const info = await invoke<DeviceInfo>("get_device_info");
    localStorage.setItem(DEVICE_KEY, info.device_id);
    localStorage.setItem(HOST_KEY, info.hostname);
    return info;
  } catch {
    return fallbackDeviceId();
  }
}

export function isLicenseActivated(): boolean {
  return localStorage.getItem(ACTIVATED_KEY) === "1";
}

/**
 * Clear the local "activated" flag when the server rejects this device at
 * login (installation missing or reassigned elsewhere) — otherwise the app
 * is stuck believing it's activated while the server has nothing to match
 * against, and the Activate screen becomes unreachable.
 */
export function clearLicenseActivation(): void {
  localStorage.removeItem(ACTIVATED_KEY);
  localStorage.removeItem("smorx_license_key");
  localStorage.removeItem("smorx_customer_id");
  localStorage.removeItem("smorx_installation_id");
}

export function markLicenseActivated(payload: {
  license_key: string;
  customer_id?: string;
  installation_id?: string;
}): void {
  localStorage.setItem(ACTIVATED_KEY, "1");
  localStorage.setItem("smorx_license_key", payload.license_key);
  if (payload.customer_id) localStorage.setItem("smorx_customer_id", payload.customer_id);
  if (payload.installation_id) {
    localStorage.setItem("smorx_installation_id", payload.installation_id);
  }
}

/**
 * Re-confirm activation with the backend, keyed by the machine's real device ID
 * (Windows MachineGuid via Tauri) rather than the local flag, which a reinstall
 * or cleared browser storage can wipe even though the server still has this
 * device approved. No-op if already activated locally.
 */
export async function verifyActivationWithServer(): Promise<boolean> {
  if (isLicenseActivated()) return true;
  try {
    const info = await getDeviceInfo();
    const status = await api.deviceStatus(info.device_id);
    if (status.activated) {
      markLicenseActivated({
        license_key: status.license_key || "",
        customer_id: status.customer_id,
        installation_id: status.installation_id,
      });
      return true;
    }
  } catch {
    // Backend unreachable or device unknown — fall through to normal activation flow.
  }
  return false;
}

export async function isDesktopApp(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  if ("__TAURI_INTERNALS__" in window || "__TAURI__" in window) return true;
  try {
    const { isTauri } = await import("@tauri-apps/api/core");
    return await isTauri();
  } catch {
    return false;
  }
}
