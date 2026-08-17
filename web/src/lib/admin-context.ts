import type { Writable } from "svelte/store";

import type { AdminSession } from "./admin-api";

export type AdminSection = "overview" | "callers" | "credentials" | "policy";
export type AdminSessionPhase = "checking" | "signed_out" | "ready";

export interface AdminUiContext {
  activeSection: Writable<AdminSection>;
  session: Writable<AdminSession | null>;
  sessionPhase: Writable<AdminSessionPhase>;
  logoutRequested: Writable<number>;
}

export const ADMIN_UI_CONTEXT = Symbol("zangpu-admin-ui");
