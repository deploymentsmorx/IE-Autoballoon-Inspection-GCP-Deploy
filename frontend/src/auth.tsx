import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type User } from "./api";

type AuthCtx = {
  user: User | null;
  token: string | null;
  /** False until the first /me check finishes (prevents refresh bounce to /admin). */
  ready: boolean;
  login: (u: string, p: string, device?: { device_id: string; hostname: string }) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
  clearJob: () => void;
};

const Ctx = createContext<AuthCtx | null>(null);

/** Clear saved inspection job (drawings / balloons / report) from this device. */
export function clearJobSession() {
  localStorage.removeItem("smorx_job");
  sessionStorage.removeItem("smorx_job");
}

/** Current-session job only (cleared when EXE / browser is closed). */
export function getJobSession(): string | null {
  return sessionStorage.getItem("smorx_job") || null;
}

export function setJobSession(jobId: string) {
  sessionStorage.setItem("smorx_job", jobId);
  // Do not keep across app reopen
  localStorage.removeItem("smorx_job");
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(localStorage.getItem("smorx_token"));
  const [ready, setReady] = useState(!localStorage.getItem("smorx_token"));

  const refresh = async () => {
    if (!localStorage.getItem("smorx_token")) {
      setUser(null);
      setToken(null);
      return;
    }
    const me = await api.me();
    setUser(me);
  };

  useEffect(() => {
    // Drop any leftover job from a previous EXE / browser run (old localStorage).
    localStorage.removeItem("smorx_job");
    let cancelled = false;
    (async () => {
      if (!token) {
        setReady(true);
        return;
      }
      try {
        // Cap wait so EXE does not sit on "Loading…" if API is down
        await Promise.race([
          refresh(),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("API timeout")), 9000),
          ),
        ]);
      } catch {
        if (!cancelled) {
          clearJobSession();
          localStorage.removeItem("smorx_token");
          setToken(null);
          setUser(null);
        }
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = async (
    username: string,
    password: string,
    device?: { device_id: string; hostname: string },
  ) => {
    const res = await api.login(username, password, device);
    // Fresh login → no leftover drawing from a previous session
    clearJobSession();
    localStorage.setItem("smorx_token", res.token);
    setToken(res.token);
    setUser(res.user);
    setReady(true);
  };

  const logout = () => {
    // Keep device/license-activation keys — logging out must not un-activate
    // the desktop install. Otherwise "Sign in" after logout wrongly bounces
    // an already-activated machine back to the Activate-license page.
    const keep = new Set([
      "smorx_device_id",
      "smorx_hostname",
      "smorx_license_activated",
      "smorx_license_key",
      "smorx_customer_id",
      "smorx_installation_id",
    ]);
    const keys = Object.keys(localStorage).filter(
      (k) => k.startsWith("smorx_") && !keep.has(k),
    );
    for (const k of keys) localStorage.removeItem(k);
    setToken(null);
    setUser(null);
    setReady(true);
  };

  const clearJob = () => {
    clearJobSession();
  };

  const value = useMemo(
    () => ({ user, token, ready, login, logout, refresh, clearJob }),
    [user, token, ready],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const v = useContext(Ctx);
  if (!v) throw new Error("AuthProvider missing");
  return v;
}
