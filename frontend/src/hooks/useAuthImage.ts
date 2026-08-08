import { useEffect, useState } from "react";
import { api } from "../api";

/**
 * Load a preview/image URL with Authorization (Bearer), as an object URL.
 * Retries once — concurrent preview races used to 500 on Windows file locks.
 */
export function useAuthImage(url: string | null | undefined) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    if (!url) {
      setSrc(null);
      setFailed(false);
      setLoading(false);
      return;
    }

    setLoading(true);
    setFailed(false);
    setSrc(null);

    (async () => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          if (attempt > 0) await new Promise((r) => setTimeout(r, 350));
          const blob = await api.authFetch(url);
          if (cancelled) return;
          if (!blob || blob.size < 32) throw new Error("Empty preview");
          objectUrl = URL.createObjectURL(blob);
          setSrc(objectUrl);
          setFailed(false);
          lastErr = null;
          break;
        } catch (e) {
          lastErr = e;
        }
      }
      if (!cancelled) {
        if (lastErr) {
          setSrc(null);
          setFailed(true);
        }
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  return { src, failed, loading };
}
