import { useEffect, useRef, useState } from "react";

const COPIED_MS = 2000;

/**
 * Copies the page URL, which after a search is the share link for it (see
 * utils/sharedSearch). Read at click time rather than passed in, so what is
 * copied is exactly what the address bar shows.
 *
 * The clipboard API is absent outside a secure context and can refuse even
 * inside one (permissions, an unfocused document). Either way the link is
 * shown in a read-only field, already selected, for a manual copy rather than
 * the button failing silently.
 */
export default function CopyLink() {
  const [state, setState] = useState<"idle" | "copied" | "manual">("idle");
  const [href, setHref] = useState("");
  const field = useRef<HTMLInputElement>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  useEffect(() => {
    if (state !== "manual") return;
    field.current?.focus();
    field.current?.select();
  }, [state, href]);

  async function copy() {
    const url = window.location.href;
    setHref(url);
    window.clearTimeout(timer.current);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(url);
      setState("copied");
      timer.current = window.setTimeout(() => setState("idle"), COPIED_MS);
    } catch {
      setState("manual");
    }
  }

  return (
    <div className={state === "manual" ? "copy-link is-manual" : "copy-link"}>
      {/* Live, so the swap to "Copied" is announced and not only seen. */}
      <button type="button" className="btn btn-secondary btn-sm" onClick={copy} aria-live="polite">
        {state === "copied" ? "Copied" : "Copy link"}
      </button>
      {state === "manual" && (
        <input
          ref={field}
          type="text"
          className="copy-link-field"
          readOnly
          value={href}
          aria-label="Link to this search"
          onFocus={(e) => e.currentTarget.select()}
        />
      )}
    </div>
  );
}
