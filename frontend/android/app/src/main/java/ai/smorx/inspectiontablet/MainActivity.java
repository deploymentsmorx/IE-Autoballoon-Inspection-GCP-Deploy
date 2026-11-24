package ai.smorx.inspectiontablet;

import android.annotation.SuppressLint;
import android.os.Build;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;

public class MainActivity extends BridgeActivity {
    @Override
    public void onStart() {
        super.onStart();
        WebView webView = getBridge() != null ? getBridge().getWebView() : null;
        if (webView != null) {
            webView.addJavascriptInterface(new TabletDevice(this), "SmorxDevice");
        }
    }

    public static final class TabletDevice {
        private final MainActivity activity;

        TabletDevice(MainActivity activity) {
            this.activity = activity;
        }

        @JavascriptInterface
        public String getDeviceInfo() {
            String model = Build.MODEL == null ? "" : Build.MODEL.trim();
            String serial = firstUseful(
                    readProp("ril.serialnumber"),
                    readProp("ro.serialno"),
                    readProp("ro.boot.serialno"),
                    legacySerial(),
                    androidId()
            );
            try {
                JSONObject json = new JSONObject();
                json.put("model", model);
                json.put("serial", serial);
                json.put("device_id", serial);
                json.put("hostname", model.isEmpty() ? "Android tablet" : model);
                return json.toString();
            } catch (Exception e) {
                return "{\"model\":\"\",\"serial\":\"\",\"device_id\":\"\",\"hostname\":\"Android tablet\"}";
            }
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
