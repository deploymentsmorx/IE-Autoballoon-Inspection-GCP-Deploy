const DEVICE_KEY = "smorx_device_id";
const HOST_KEY = "smorx_hostname";
const ACTIVATED_KEY = "smorx_license_activated";

export type DeviceInfo = {
  device_id: string;
  hostname: string;
};

function fallbackDeviceId(): DeviceInfo {
  let id = localStorage.getItem(DEVICE_KEY);
  if (!id) {
    id = `DEV-WEB-${crypto.randomUUID().replace(/-/g, "").slice(0, 16).toUpperCase()}`;
    localStorage.setItem(DEVICE_KEY, id);
  }
  const hostname = localStorage.getItem(HOST_KEY) || "browser";
  return { device_id: id, hostname };
}

/** Stable device id: Windows MachineGuid via Tauri, else browser UUID. */
export async function getDeviceInfo(): Promise<DeviceInfo> {
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

export async function isDesktopApp(): Promise<boolean> {
  try {
    const { isTauri } = await import("@tauri-apps/api/core");
    return isTauri();
  } catch {
    return false;
  }
}
