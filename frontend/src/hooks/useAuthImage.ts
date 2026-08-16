import { useEffect, useRef, useState } from "react";
import { api } from "../api";

/**
 * Load a preview/image URL with Authorization (Bearer), as an object URL.
 * Keeps the previous image visible while the next URL loads so the page
 * does not blank/flicker during edit/save preview refreshes.
 */
export function useAuthImage(url: string | null | undefined) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  const prevObjectUrlRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    if (!url) {
      if (prevObjectUrlRef.current) {
        URL.revokeObjectURL(prevObjectUrlRef.current);
        prevObjectUrlRef.current = null;
      }
      setSrc(null);
      setFailed(false);
      setLoading(false);
      return;
    }

    setLoading(true);
    setFailed(false);
    // Do NOT clear src here — keep showing the last good frame.

    (async () => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          if (attempt > 0) await new Promise((r) => setTimeout(r, 350));
          const blob = await api.authFetch(url);
          if (cancelled) return;
          if (!blob || blob.size < 32) throw new Error("Empty preview");
          objectUrl = URL.createObjectURL(blob);
          const old = prevObjectUrlRef.current;
          prevObjectUrlRef.current = objectUrl;
          setSrc(objectUrl);
          setFailed(false);
          lastErr = null;
          // Revoke previous after swap (next tick so canvas can bind first)
          if (old && old !== objectUrl) {
            setTimeout(() => URL.revokeObjectURL(old), 1500);
          }
          break;
        } catch (e) {
          lastErr = e;
        }
      }
      if (!cancelled) {
        if (lastErr) {
          // Only mark failed if we have nothing to show
          if (!prevObjectUrlRef.current) {
            setSrc(null);
            setFailed(true);
          }
        }
        setLoading(false);
      } else if (objectUrl && objectUrl !== prevObjectUrlRef.current) {
        URL.revokeObjectURL(objectUrl);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [url]);

  useEffect(() => {
    return () => {
      if (prevObjectUrlRef.current) {
        URL.revokeObjectURL(prevObjectUrlRef.current);
        prevObjectUrlRef.current = null;
      }
    };
  }, []);

  return { src, failed, loading };
}
