export const COSTSHEET_ADMIN_PORTAL = import.meta.env.VITE_ADMIN_PORTAL_ONLY === "true";
export const APP_TITLE = COSTSHEET_ADMIN_PORTAL
  ? "Cost Sheet Admin Portal"
  : "AutoBalloon & Inspection Software System";
