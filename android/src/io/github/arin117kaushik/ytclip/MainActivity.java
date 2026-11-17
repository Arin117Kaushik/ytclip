package io.github.arin117kaushik.ytclip;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Html;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The ytclip page in an app window. The page is served by ytclip running inside Termux,
 * which this app starts in the background, so Termux itself never has to be opened.
 */
public class MainActivity extends Activity {
    static final String HOME = "http://127.0.0.1:8765/";
    static final String TERMUX = "com.termux";
    static final String PERMISSION = "com.termux.permission.RUN_COMMAND";
    static final int WAIT_TRIES = 60;  // about 45 seconds; the first start after a reboot is the slow one

    final Handler handler = new Handler(Looper.getMainLooper());
    WebView web;
    String target = HOME;
    int tries;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().setStatusBarColor(Color.BLACK);
        getWindow().setNavigationBarColor(Color.BLACK);
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);  // the page remembers quality and format
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) waitForServer();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if ("127.0.0.1".equals(url.getHost())) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, url));  // other links open in the browser
                return true;
            }
        });
        setContentView(web);
        readShare(getIntent());

        if (!termuxInstalled()) {
            show("ytclip needs the Termux app.",
                 "Install Termux from F-Droid, open it and run the ytclip setup line from the README, then open ytclip again.");
        } else if (checkSelfPermission(PERMISSION) == PackageManager.PERMISSION_GRANTED) {
            startServer();
        } else {
            show("One more step", "Allow ytclip to run commands in Termux. That is how it starts the downloader.");
            requestPermissions(new String[] {PERMISSION}, 1);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readShare(intent);
        if (checkSelfPermission(PERMISSION) == PackageManager.PERMISSION_GRANTED) startServer();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            startServer();
        } else {
            show("ytclip cannot start",
                 "It needs permission to run commands in Termux. Allow it in Settings, Apps, ytclip, Permissions.");
        }
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    /** A link shared from YouTube becomes ?url=... on the page, which fills it in. */
    void readShare(Intent intent) {
        target = HOME;
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return;
        String text = intent.getStringExtra(Intent.EXTRA_TEXT);
        Matcher link = Pattern.compile("https?://\\S+").matcher(text == null ? "" : text);
        if (link.find()) target = HOME + "?url=" + Uri.encode(link.group());
    }

    boolean termuxInstalled() {
        try {
            getPackageManager().getPackageInfo(TERMUX, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    /** Asks Termux to run "ytclip --no-browser". If it is already running, the second copy just exits. */
    void startServer() {
        Intent run = new Intent("com.termux.RUN_COMMAND");
        run.setClassName(TERMUX, "com.termux.app.RunCommandService");
        run.putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/ytclip");
        run.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[] {"--no-browser"});
        run.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        try {
            startService(run);
        } catch (RuntimeException e) {
            show("Could not reach Termux", e.getMessage());
            return;
        }
        tries = 0;
        web.loadUrl(target);
    }

    void waitForServer() {
        tries += 1;
        if (tries > WAIT_TRIES) {
            show("ytclip did not start",
                 "Open Termux and run the ytclip setup line again (it turns on the setting this app needs), then reopen ytclip.");
            return;
        }
        show("Starting ytclip...", "");
        handler.postDelayed(() -> web.loadUrl(target), 750);
    }

    void show(String title, String detail) {
        web.loadDataWithBaseURL(null,
            "<meta name=viewport content='width=device-width'>"
            + "<body style='background:#000;color:#fff;font:16px/1.5 sans-serif;padding:24px'>"
            + "<h2>" + Html.escapeHtml(title) + "</h2><p style='color:#b3b3b3'>" + Html.escapeHtml(detail) + "</p>",
            "text/html", "utf-8", null);
    }
}
