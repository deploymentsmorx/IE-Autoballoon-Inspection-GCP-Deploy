import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type User } from "./api";

type AuthCtx = {
  user: User | null;
  token: string | null;
  /** False until the first /me check finishes (prevents refresh bounce to /admin). */
  ready: boolean;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
  clearJob: () => void;
};

const Ctx = createContext<AuthCtx | null>(null);

/** Clear saved inspection job (drawings / balloons) from this browser. */
export function clearJobSession() {
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
    let cancelled = false;
    (async () => {
      if (!token) {
        setReady(true);
        return;
      }
      try {
        await refresh();
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

  const login = async (username: string, password: string) => {
    const res = await api.login(username, password);
    // Fresh login → no leftover drawing from a previous session
    clearJobSession();
    localStorage.setItem("smorx_token", res.token);
    setToken(res.token);
    setUser(res.user);
    setReady(true);
  };

  const logout = () => {
    const keys = Object.keys(localStorage).filter((k) => k.startsWith("smorx_"));
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
