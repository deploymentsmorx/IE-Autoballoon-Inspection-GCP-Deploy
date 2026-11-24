import type { CapacitorConfig } from "@capacitor/cli";

/** The installed APK opens this site, so later website deploys show on the tablet without a new APK. */
const serverUrl = (process.env.CAPACITOR_SERVER_URL || "http://100.51.165.76:8464").replace(/\/$/, "");

const config: CapacitorConfig = {
  appId: "ai.smorx.inspectiontablet",
  appName: "Auto ballooning software",
  webDir: "dist",
  server: {
    url: serverUrl,
    cleartext: true,
    androidScheme: "http",
  },
  android: {
    allowMixedContent: true,
  },
  plugins: {
    CapacitorHttp: {
      enabled: true,
    },
  },
};

export default config;
