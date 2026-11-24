package ai.smorx.inspectiontablet;

import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;
import android.telephony.TelephonyManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.BridgeActivity;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;

public class MainActivity extends BridgeActivity {
    @Override
    public void onStart() {
        super.onStart();
        attachDeviceBridge();
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.READ_PHONE_STATE)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{android.Manifest.permission.READ_PHONE_STATE},
                    41
            );
        }
    }

    @Override
    public void onResume() {
        super.onResume();
        attachDeviceBridge();
    }

    private void attachDeviceBridge() {
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView != null) {
            webView.addJavascriptInterface(new TabletDevice(this), "SmorxDevice");
            webView.setDownloadListener(new DownloadListener() {
                @Override
                public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                    enqueueDownload(url, contentDisposition, mimeType);
                }
            });
        }
    }

    void enqueueDownload(String url, String contentDisposition, String mimeType) {
        if (url == null || url.startsWith("blob:") || url.startsWith("data:")) {
            Toast.makeText(this, "This file cannot be saved. Update the app.", Toast.LENGTH_LONG).show();
            return;
        }
        try {
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            request.setTitle(name);
            request.setDescription(name);
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            if (mimeType != null && !mimeType.isEmpty()) request.setMimeType(mimeType);
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) request.addRequestHeader("Cookie", cookie);
            WebView webView = getBridge() != null ? getBridge().getWebView() : null;
            if (webView != null) request.addRequestHeader("User-Agent", webView.getSettings().getUserAgentString());
            DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            if (manager == null) throw new IllegalStateException("no download manager");
            manager.enqueue(request);
            Toast.makeText(this, "Saving to My Files → Download", Toast.LENGTH_SHORT).show();
        } catch (Exception error) {
            Toast.makeText(this, "Download failed.", Toast.LENGTH_LONG).show();
        }
    }

    public static final class TabletDevice {
        private final MainActivity activity;

        TabletDevice(MainActivity activity) {
            this.activity = activity;
        }

        @JavascriptInterface
        public void downloadUrl(String url) {
            activity.runOnUiThread(() -> activity.enqueueDownload(url, null, mimeFor(url)));
        }

        private static String mimeFor(String url) {
            String lower = url == null ? "" : url.toLowerCase();
            if (lower.contains(".pdf")) return "application/pdf";
            if (lower.contains(".xlsx")) return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
            if (lower.contains(".csv")) return "text/csv";
            if (lower.contains(".json")) return "application/json";
            return "application/octet-stream";
        }

        @JavascriptInterface
        public String getDeviceInfo() {
            String model = Build.MODEL == null ? "" : Build.MODEL.trim();
            String imei = readImei();
            String serial = firstUseful(
                    readProp("ril.serialnumber"),
                    readProp("ro.serialno"),
                    readProp("ro.boot.serialno"),
                    legacySerial()
            );
            String deviceId = !imei.isEmpty() ? imei : (!serial.isEmpty() ? serial : androidId());
            try {
                JSONObject json = new JSONObject();
                json.put("model", model);
                json.put("imei", imei);
                json.put("serial", serial);
                json.put("device_id", deviceId);
                json.put("hostname", model.isEmpty() ? "Android tablet" : model);
                return json.toString();
            } catch (Exception e) {
                return "{\"model\":\"\",\"imei\":\"\",\"serial\":\"\",\"device_id\":\"\",\"hostname\":\"Android tablet\"}";
            }
        }

        @SuppressLint({"HardwareIds", "MissingPermission"})
        private String readImei() {
            String fromProp = digitsOnly(firstUseful(
                    readProp("ril.gsm.imei"),
                    readProp("persist.radio.imei"),
                    readProp("persist.radio.imei1"),
                    readProp("gsm.imei")
            ));
            if (fromProp.length() >= 14) return fromProp;
            try {
                if (ContextCompat.checkSelfPermission(activity, android.Manifest.permission.READ_PHONE_STATE)
                        != PackageManager.PERMISSION_GRANTED) {
                    return "";
                }
                TelephonyManager tm = (TelephonyManager) activity.getSystemService(Context.TELEPHONY_SERVICE);
                if (tm == null) return "";
                String raw = "";
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    raw = tm.getImei();
                    if ((raw == null || raw.isEmpty()) && tm.getPhoneCount() > 0) {
                        raw = tm.getImei(0);
                    }
                } else {
                    raw = tm.getDeviceId();
                }
                String digits = digitsOnly(raw);
                return digits.length() >= 14 ? digits : "";
            } catch (Exception e) {
                return "";
            }
        }

        private static String digitsOnly(String value) {
            if (value == null) return "";
            return value.replaceAll("\\D", "");
        }

        @SuppressLint("HardwareIds")
        private String legacySerial() {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    return Build.getSerial();
                }
                return Build.SERIAL;
            } catch (Exception e) {
                return "";
            }
        }

        @SuppressLint("HardwareIds")
        private String androidId() {
            try {
                return Settings.Secure.getString(activity.getContentResolver(), Settings.Secure.ANDROID_ID);
            } catch (Exception e) {
                return "";
            }
        }

        private static String readProp(String key) {
            Process process = null;
            try {
                process = Runtime.getRuntime().exec(new String[]{"getprop", key});
                BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()));
                String line = reader.readLine();
                reader.close();
                return line == null ? "" : line.trim();
            } catch (Exception e) {
                return "";
            } finally {
                if (process != null) process.destroy();
            }
        }

        private static String firstUseful(String... values) {
            for (String value : values) {
                if (value == null) continue;
                String cleaned = value.trim();
                if (cleaned.isEmpty()) continue;
                if (cleaned.equalsIgnoreCase("unknown")) continue;
                if (cleaned.equalsIgnoreCase("null")) continue;
                return cleaned;
            }
            return "";
        }
    }
}
