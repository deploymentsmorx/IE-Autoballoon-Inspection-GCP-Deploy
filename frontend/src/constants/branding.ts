export const COSTSHEET_ADMIN_PORTAL = import.meta.env.VITE_ADMIN_PORTAL_ONLY === "true";
export const TABLET_EXE = import.meta.env.VITE_TABLET_CLIENT === "true";
export const APP_TITLE = COSTSHEET_ADMIN_PORTAL
  ? "Cost Sheet Admin Portal"
  : TABLET_EXE
    ? "Inspection Tablet"
    : "AutoBalloon & Inspection Software System";
