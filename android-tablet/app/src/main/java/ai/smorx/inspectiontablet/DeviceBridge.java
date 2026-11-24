package ai.smorx.inspectiontablet;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.provider.Settings;
import android.telephony.TelephonyManager;
import android.webkit.JavascriptInterface;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Method;

public class DeviceBridge {
    private final MainActivity activity;

    DeviceBridge(MainActivity activity) {
        this.activity = activity;
    }

    @JavascriptInterface
    public String getDeviceInfo() {
        return info();
    }

    @JavascriptInterface
    public String info() {
        try {
            String model = Build.MODEL == null ? "" : Build.MODEL.trim();
            String serial = readSerial();
            String imei = readImei();
            String deviceId = !imei.isEmpty() ? imei : serial;
            JSONObject json = new JSONObject();
            json.put("device_id", deviceId);
            json.put("serial", serial);
            json.put("imei", imei);
            json.put("model", model);
            json.put("device_name", model);
            json.put("hostname", model.isEmpty() ? "Android tablet" : model);
            json.put("platform", "Android");
            return json.toString();
        } catch (Exception error) {
            return "{\"device_id\":\"\",\"serial\":\"\",\"imei\":\"\",\"model\":\"\",\"hostname\":\"Android tablet\",\"platform\":\"Android\"}";
        }
    }

    @JavascriptInterface
    public String getSerial() {
        return readSerial();
    }

    @JavascriptInterface
    public String getModel() {
        return Build.MODEL == null ? "" : Build.MODEL;
    }

    @JavascriptInterface
    public String getDeviceName() {
        return Build.MODEL == null ? "Tablet" : Build.MODEL;
    }

    @JavascriptInterface
    public void downloadUrl(String url) {
        activity.runOnUiThread(() -> activity.enqueueDownload(url, null, null));
    }

    @JavascriptInterface
    public void saveFile(String base64, String name, String mime) {
        activity.saveBase64File(base64, name, mime);
    }

    private String readSerial() {
        String serial = systemProperty("ro.serialno");
        if (serial.isEmpty()) serial = systemProperty("ril.serialnumber");
        if (serial.isEmpty()) serial = readProp("ro.boot.serialno");
        if (serial.isEmpty() && Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            serial = Build.SERIAL == null ? "" : Build.SERIAL;
        }
        if (serial.isEmpty() || "unknown".equalsIgnoreCase(serial)) {
            serial = Settings.Secure.getString(activity.getContentResolver(), Settings.Secure.ANDROID_ID);
        }
        return serial == null ? "" : serial.trim();
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
            if (activity.checkSelfPermission(android.Manifest.permission.READ_PHONE_STATE)
                    != PackageManager.PERMISSION_GRANTED) {
                return "";
            }
            TelephonyManager tm = (TelephonyManager) activity.getSystemService(Context.TELEPHONY_SERVICE);
            if (tm == null) return "";
            String raw;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                raw = tm.getImei();
                if ((raw == null || raw.isEmpty()) && tm.getPhoneCount() > 0) raw = tm.getImei(0);
            } else {
                raw = tm.getDeviceId();
            }
            String digits = digitsOnly(raw);
            return digits.length() >= 14 ? digits : "";
        } catch (Exception error) {
            return "";
        }
    }

    private static String systemProperty(String key) {
        try {
            Class<?> clazz = Class.forName("android.os.SystemProperties");
            Method method = clazz.getMethod("get", String.class);
            Object value = method.invoke(null, key);
            return value == null ? "" : String.valueOf(value).trim();
        } catch (Exception error) {
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
        } catch (Exception error) {
            return "";
        } finally {
            if (process != null) process.destroy();
        }
    }

    private static String firstUseful(String... values) {
        for (String value : values) {
            if (value == null) continue;
            String cleaned = value.trim();
            if (cleaned.isEmpty() || cleaned.equalsIgnoreCase("unknown") || cleaned.equalsIgnoreCase("null")) continue;
            return cleaned;
        }
        return "";
    }

    private static String digitsOnly(String value) {
        if (value == null) return "";
        return value.replaceAll("\\D", "");
    }
}
