package ai.smorx.inspectiontablet;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.io.OutputStream;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER = 1001;
    private static final String PREFS = "autoballoon";
    private static final String SERVER = "server";

    private WebView webView;
    private LinearLayout setup;
    private EditText serverField;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        webView = findViewById(R.id.webView);
        setup = findViewById(R.id.setup);
        serverField = findViewById(R.id.serverField);
        Button openServer = findViewById(R.id.openServer);

        String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString(SERVER, getString(R.string.default_server));
        serverField.setText(saved);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, true);

        DeviceBridge bridge = new DeviceBridge(this);
        webView.addJavascriptInterface(bridge, "IEDevice");
        webView.addJavascriptInterface(bridge, "Android");
        webView.addJavascriptInterface(bridge, "SmorxDevice");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    setup.setVisibility(View.VISIBLE);
                    Toast.makeText(MainActivity.this, "Cannot open that server. Check the address.", Toast.LENGTH_LONG).show();
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER);
                } catch (Exception error) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this, "Cannot open files on this device.", Toast.LENGTH_SHORT).show();
                    return false;
                }
                return true;
            }
        });
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                enqueueDownload(url, contentDisposition, mimeType);
            }
        });

        if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.READ_PHONE_STATE}, 41);
        }

        openServer.setOnClickListener(v -> openServer());
        openServer();
    }

    private void openServer() {
        String url = serverField.getText().toString().trim();
        if (url.isEmpty()) {
            Toast.makeText(this, "Enter the server address.", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://" + url;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(SERVER, url).apply();
        setup.setVisibility(LinearLayout.GONE);
        webView.setVisibility(WebView.VISIBLE);
        webView.loadUrl(url);
    }

    void enqueueDownload(String url, String contentDisposition, String mimeType) {
        if (url == null || url.startsWith("blob:") || url.startsWith("data:")) {
            Toast.makeText(this, "This file cannot be saved from the page. Update the app.", Toast.LENGTH_LONG).show();
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
            request.addRequestHeader("User-Agent", webView.getSettings().getUserAgentString());
            DownloadManager manager = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            manager.enqueue(request);
            Toast.makeText(this, "Saving to Downloads…", Toast.LENGTH_SHORT).show();
        } catch (Exception error) {
            Toast.makeText(this, "Download failed.", Toast.LENGTH_LONG).show();
        }
    }

    void saveBase64File(String base64, String name, String mime) {
        new Thread(() -> {
            try {
                byte[] bytes = Base64.decode(base64, Base64.DEFAULT);
                String type = mime == null || mime.isEmpty() ? "application/octet-stream" : mime;
                String fileName = name == null || name.isEmpty() ? "inspection.pdf" : name;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.Downloads.DISPLAY_NAME, fileName);
                    values.put(MediaStore.Downloads.MIME_TYPE, type);
                    values.put(MediaStore.Downloads.IS_PENDING, 1);
                    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                    if (uri == null) throw new IllegalStateException("insert");
                    try (OutputStream out = getContentResolver().openOutputStream(uri)) {
                        if (out == null) throw new IllegalStateException("stream");
                        out.write(bytes);
                    }
                    values.clear();
                    values.put(MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(uri, values, null, null);
                } else {
                    throw new IllegalStateException("old");
                }
                runOnUiThread(() -> Toast.makeText(this, "Saved " + fileName, Toast.LENGTH_SHORT).show());
            } catch (Exception error) {
                runOnUiThread(() -> Toast.makeText(this, "Could not save the file.", Toast.LENGTH_LONG).show());
            }
        }).start();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER || fileCallback == null) return;
        Uri[] result = null;
        if (resultCode == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                int count = data.getClipData().getItemCount();
                result = new Uri[count];
                for (int i = 0; i < count; i++) result[i] = data.getClipData().getItemAt(i).getUri();
            } else if (data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
        }
        fileCallback.onReceiveValue(result);
        fileCallback = null;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }
}
