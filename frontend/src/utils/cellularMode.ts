import { useEffect, useRef, useState } from "react";
import { fetchCellularStatus } from "../api";
import type { CellularStatusResponse } from "../types";

/**
 * The cellular version of the page: asked for in the URL, granted by the
 * server.
 *
 * `?cellular=1` (or `true`, `yes`) asks for it. The page then asks
 * GET /api/cellular once, and only an enabled answer turns it on; anything
 * else leaves it "unavailable" and the page behaves as the broadcast search.
 * Without the flag nothing here makes a request.
 */

const ON_VALUES = ["1", "true", "yes"];

/** True when the query string asks for the cellular version. With the key
 *  repeated, the first one decides. */
export function cellularFlagSet(search: string): boolean {
  const raw = new URLSearchParams(search).get("cellular");
  return raw !== null && ON_VALUES.includes(raw.trim().toLowerCase());
}

export type CellularMode =
  | { kind: "off" }
  | { kind: "checking" }
  | { kind: "on"; status: CellularStatusResponse }
  | { kind: "unavailable" };

const OFF: CellularMode = { kind: "off" };
const CHECKING: CellularMode = { kind: "checking" };
const UNAVAILABLE: CellularMode = { kind: "unavailable" };

/**
 * Where the cellular version stands for this mount. `requested` is read once
 * by the caller and is not expected to change.
 *
 * Not requested: "off", and no request. Requested: "checking" until the one
 * GET /api/cellular answers, then "on" or "unavailable" for good.
 *
 * The request lives in a ref rather than at module level. StrictMode runs the
 * effect, its cleanup and the effect again on the same mount, and the ref
 * survives that, so the second run subscribes to the request the first one
 * started instead of sending another. A fresh mount gets a fresh ref and asks
 * again. The request is not aborted on cleanup, since StrictMode's cleanup
 * looks the same as a real unmount and aborting it would leave the remount
 * nothing to wait for; the answer of an unmounted component is just dropped.
 */
export function useCellularMode(requested: boolean): CellularMode {
  const [mode, setMode] = useState<CellularMode>(requested ? CHECKING : OFF);
  const request = useRef<Promise<CellularStatusResponse | null> | null>(null);

  useEffect(() => {
    if (!requested) return;
    if (!request.current) request.current = fetchCellularStatus();
    let live = true;
    request.current.then((status) => {
      if (live) setMode(status ? { kind: "on", status } : UNAVAILABLE);
    });
    return () => {
      live = false;
    };
  }, [requested]);

  return mode;
}
