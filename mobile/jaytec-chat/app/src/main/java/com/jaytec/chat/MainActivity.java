package com.jaytec.chat;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import android.util.Base64;

public class MainActivity extends Activity {
    private static final int BG = Color.rgb(5, 8, 12);
    private static final int PANEL = Color.rgb(13, 21, 29);
    private static final int CYAN = Color.rgb(0, 229, 255);
    private static final int BLUE = Color.rgb(35, 125, 255);
    private static final int GREEN = Color.rgb(48, 225, 116);
    private static final int TEXT = Color.rgb(232, 247, 255);
    private static final int MUTED = Color.rgb(125, 151, 166);
    private static final int RED = Color.rgb(255, 85, 100);

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final AtomicBoolean pollInFlight = new AtomicBoolean(false);

    private SharedPreferences prefs;
    private SecureTokenStore tokenStore;
    private FrameLayout content;
    private TextView topStatus;
    private LinearLayout messages;
    private ScrollView chatScroll;
    private EditText composer;
    private LinearLayout activityList;
    private TextView diagnostics;
    private EditText gatewayInput;
    private EditText tokenInput;
    private CheckBox allowHttp;
    private boolean resumed;
    private String currentTab = "chat";
    private long lastRetryAt = 0L;

    private final Runnable pollLoop = new Runnable() {
        @Override public void run() {
            if (resumed && "chat".equals(currentTab)) pollOnce();
            if (resumed) main.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("jaytec_chat", MODE_PRIVATE);
        tokenStore = new SecureTokenStore(this);
        buildShell();
        showChat();
    }

    @Override protected void onResume() {
        super.onResume();
        resumed = true;
        main.removeCallbacks(pollLoop);
        main.post(pollLoop);
        pollOnce();
    }

    @Override protected void onPause() {
        resumed = false;
        main.removeCallbacks(pollLoop);
        super.onPause();
    }

    private void buildShell() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);

        LinearLayout header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(dp(16), dp(12), dp(16), dp(10));
        TextView title = label("JAYTEC CHAT", 20, CYAN, true);
        header.addView(title, new LinearLayout.LayoutParams(0, dp(44), 1));
        topStatus = label("OFFLINE", 12, MUTED, true);
        topStatus.setGravity(Gravity.CENTER);
        header.addView(topStatus, new LinearLayout.LayoutParams(dp(90), dp(36)));
        root.addView(header);

        content = new FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        LinearLayout nav = new LinearLayout(this);
        nav.setPadding(dp(8), dp(6), dp(8), dp(10));
        nav.setGravity(Gravity.CENTER);
        nav.addView(navButton("CHAT", v -> showChat()), weight());
        nav.addView(navButton("ACTIVITY", v -> showActivity()), weight());
        nav.addView(navButton("SETTINGS", v -> showSettings()), weight());
        root.addView(nav);

        setContentView(root);
    }

    private LinearLayout.LayoutParams weight() {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, dp(46), 1);
        p.setMargins(dp(4), 0, dp(4), 0);
        return p;
    }

    private Button navButton(String text, View.OnClickListener l) {
        Button b = button(text);
        b.setOnClickListener(l);
        return b;
    }

    private void showChat() {
        currentTab = "chat";
        content.removeAllViews();
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.VERTICAL);
        wrap.setPadding(dp(10), dp(4), dp(10), dp(8));

        TextView hint = label("LIVE SESSION  •  1s RECONCILE", 11, MUTED, true);
        hint.setPadding(dp(6), dp(2), 0, dp(6));
        wrap.addView(hint);

        chatScroll = new ScrollView(this);
        messages = new LinearLayout(this);
        messages.setOrientation(LinearLayout.VERTICAL);
        messages.setPadding(dp(4), dp(4), dp(4), dp(10));
        chatScroll.addView(messages);
        wrap.addView(chatScroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));

        LinearLayout sendRow = new LinearLayout(this);
        sendRow.setGravity(Gravity.BOTTOM);
        composer = new EditText(this);
        composer.setHint("Message...");
        composer.setHintTextColor(MUTED);
        composer.setTextColor(TEXT);
        composer.setTextSize(16);
        composer.setMinLines(1);
        composer.setMaxLines(5);
        composer.setBackground(roundRect(PANEL, CYAN, 1));
        composer.setPadding(dp(12), dp(10), dp(12), dp(10));
        sendRow.addView(composer, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));

        Button send = button("SEND");
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(dp(86), dp(52));
        sp.setMargins(dp(8), 0, 0, 0);
        sendRow.addView(send, sp);
        send.setOnClickListener(v -> queueMessage());

        wrap.addView(sendRow);
        content.addView(wrap);
        renderMessages();
        updateTopStatus();
        pollOnce();
    }

    private void showActivity() {
        currentTab = "activity";
        content.removeAllViews();
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.VERTICAL);
        wrap.setPadding(dp(12), dp(8), dp(12), dp(8));

        TextView head = label("MCP / TOOL / SPECIALIST ACTIVITY", 14, CYAN, true);
        wrap.addView(head);

        LinearLayout filters = new LinearLayout(this);
        String[] names = {"ALL", "RUNNING", "COMPLETED", "FAILED"};
        for (String n : names) {
            Button b = button(n);
            b.setTextSize(10);
            b.setOnClickListener(v -> renderActivity(((Button)v).getText().toString().toLowerCase(Locale.US)));
            filters.addView(b, weight());
        }
        wrap.addView(filters);

        ScrollView scroll = new ScrollView(this);
        activityList = new LinearLayout(this);
        activityList.setOrientation(LinearLayout.VERTICAL);
        activityList.setPadding(0, dp(8), 0, dp(8));
        scroll.addView(activityList);
        wrap.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        content.addView(wrap);
        renderActivity("all");
        updateTopStatus();
    }

    private void showSettings() {
        currentTab = "settings";
        content.removeAllViews();
        ScrollView sv = new ScrollView(this);
        LinearLayout wrap = new LinearLayout(this);
        wrap.setOrientation(LinearLayout.VERTICAL);
        wrap.setPadding(dp(16), dp(12), dp(16), dp(20));

        wrap.addView(label("GATEWAY", 18, CYAN, true));
        wrap.addView(label("Base URL", 12, MUTED, false));
        gatewayInput = input(prefs.getString("gateway", ""));
        gatewayInput.setHint("https://gateway.example");
        wrap.addView(gatewayInput);

        wrap.addView(space(8));
        wrap.addView(label("Bearer/access token", 12, MUTED, false));
        tokenInput = input("");
        tokenInput.setHint(tokenStore.hasToken() ? "•••••••• (stored securely)" : "token");
        tokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        wrap.addView(tokenInput);

        allowHttp = new CheckBox(this);
        allowHttp.setText("Developer: allow LAN HTTP");
        allowHttp.setTextColor(TEXT);
        allowHttp.setChecked(prefs.getBoolean("allow_http", false));
        wrap.addView(allowHttp);

        LinearLayout actions = new LinearLayout(this);
        Button save = button("SAVE");
        Button test = button("TEST");
        actions.addView(save, weight());
        actions.addView(test, weight());
        wrap.addView(actions);

        Button newSession = button("NEW SESSION");
        LinearLayout.LayoutParams np = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48));
        np.setMargins(0, dp(10), 0, dp(10));
        wrap.addView(newSession, np);

        diagnostics = label("", 12, TEXT, false);
        diagnostics.setBackground(roundRect(PANEL, Color.rgb(40, 58, 70), 1));
        diagnostics.setPadding(dp(12), dp(12), dp(12), dp(12));
        wrap.addView(diagnostics);

        save.setOnClickListener(v -> saveSettings());
        test.setOnClickListener(v -> testConnection());
        newSession.setOnClickListener(v -> {
            prefs.edit().remove("conversation_id").putLong("last_seq", 0L).apply();
            Toast.makeText(this, "New session will start with next message", Toast.LENGTH_SHORT).show();
            refreshDiagnostics();
        });

        sv.addView(wrap);
        content.addView(sv);
        refreshDiagnostics();
        updateTopStatus();
    }

    private void saveSettings() {
        String gateway = gatewayInput.getText().toString().trim();
        if (!validateGateway(gateway, allowHttp.isChecked())) return;
        prefs.edit()
                .putString("gateway", stripSlash(gateway))
                .putBoolean("allow_http", allowHttp.isChecked())
                .apply();
        String token = tokenInput.getText().toString();
        if (!token.isEmpty()) {
            try {
                tokenStore.save(token);
                tokenInput.setText("");
                tokenInput.setHint("•••••••• (stored securely)");
            } catch (Exception e) {
                toast("Could not secure token: " + e.getMessage());
                return;
            }
        }
        toast("Settings saved");
        refreshDiagnostics();
        pollOnce();
    }

    private boolean validateGateway(String value, boolean lanAllowed) {
        if (value.isEmpty()) {
            toast("Gateway URL required");
            return false;
        }
        if (value.startsWith("https://")) return true;
        if (value.startsWith("http://") && lanAllowed && isPrivateLan(value)) return true;
        toast("Use HTTPS, or enable LAN HTTP for a private/local address");
        return false;
    }

    private boolean isPrivateLan(String s) {
        try {
            String host = new URL(s).getHost().toLowerCase(Locale.US);
            if (host.equals("localhost") || host.equals("127.0.0.1") || host.endsWith(".local")) return true;
            if (host.startsWith("10.") || host.startsWith("192.168.")) return true;
            if (host.startsWith("172.")) {
                String[] p = host.split("\\.");
                if (p.length > 1) {
                    int second = Integer.parseInt(p[1]);
                    return second >= 16 && second <= 31;
                }
            }
        } catch (Exception ignored) {}
        return false;
    }

    private void queueMessage() {
        String text = composer.getText().toString().trim();
        if (text.isEmpty()) return;
        String gateway = prefs.getString("gateway", "");
        if (!validateGateway(gateway, prefs.getBoolean("allow_http", false))) {
            showSettings();
            return;
        }
        String id = UUID.randomUUID().toString();
        try {
            JSONArray history = history();
            JSONObject item = new JSONObject();
            item.put("id", id);
            item.put("role", "user");
            item.put("text", text);
            item.put("ts", System.currentTimeMillis());
            item.put("status", "queued");
            history.put(item);
            saveHistory(history);
        } catch (Exception e) {
            toast("Could not queue message");
            return;
        }
        composer.setText("");
        renderMessages();
        io.execute(() -> sendMessageById(id));
    }

    private void sendMessageById(String id) {
        try {
            JSONArray h = history();
            JSONObject target = null;
            for (int i = 0; i < h.length(); i++) {
                JSONObject o = h.getJSONObject(i);
                if (id.equals(o.optString("id"))) { target = o; break; }
            }
            if (target == null) return;
            JSONObject req = new JSONObject();
            String conv = prefs.getString("conversation_id", "");
            if (!conv.isEmpty()) req.put("conversation_id", conv);
            req.put("client_message_id", id);
            req.put("text", target.optString("text"));
            HttpResult r = request("POST", "/v1/chat/messages", req.toString());
            if (r.code >= 200 && r.code < 300) {
                JSONObject body = safeObject(r.body);
                String assigned = body.optString("conversation_id", conv);
                if (!assigned.isEmpty()) prefs.edit().putString("conversation_id", assigned).apply();
                target.put("status", "sent");
                saveHistory(h);
                setOnline("ONLINE");
                runOnUiThread(() -> { if ("chat".equals(currentTab)) renderMessages(); });
                pollOnce();
            } else if (r.code == 401) {
                target.put("status", "auth");
                saveHistory(h);
                setOnline("AUTH");
            } else {
                target.put("status", "queued");
                saveHistory(h);
                setOnline("OFFLINE");
            }
        } catch (Exception e) {
            setOnline("OFFLINE");
        }
    }

    private void retryQueued() {
        long now = System.currentTimeMillis();
        if (now - lastRetryAt < 5000) return;
        lastRetryAt = now;
        try {
            JSONArray h = history();
            for (int i = 0; i < h.length(); i++) {
                JSONObject o = h.getJSONObject(i);
                if ("user".equals(o.optString("role")) && "queued".equals(o.optString("status"))) {
                    sendMessageById(o.optString("id"));
                    break;
                }
            }
        } catch (Exception ignored) {}
    }

    private void pollOnce() {
        if (!pollInFlight.compareAndSet(false, true)) return;
        io.execute(() -> {
            try {
                retryQueued();
                String conv = prefs.getString("conversation_id", "");
                if (conv.isEmpty()) {
                    setOnline(prefs.getString("gateway", "").isEmpty() ? "OFFLINE" : "READY");
                    return;
                }
                long after = prefs.getLong("last_seq", 0L);
                String path = "/v1/chat/events?conversation_id=" +
                        URLEncoder.encode(conv, "UTF-8") + "&after_seq=" + after;
                HttpResult r = request("GET", path, null);
                if (r.code >= 200 && r.code < 300) {
                    parseEvents(r.body);
                    prefs.edit().putLong("last_sync", System.currentTimeMillis()).apply();
                    setOnline("ONLINE");
                } else if (r.code == 401) {
                    setOnline("AUTH");
                } else {
                    setOnline("OFFLINE");
                }
            } catch (Exception e) {
                setOnline("OFFLINE");
            } finally {
                pollInFlight.set(false);
                runOnUiThread(() -> {
                    if ("chat".equals(currentTab)) renderMessages();
                    if ("settings".equals(currentTab)) refreshDiagnostics();
                });
            }
        });
    }

    private void parseEvents(String bodyText) {
        try {
            JSONObject root = safeObject(bodyText);
            JSONArray events = root.optJSONArray("events");
            if (events == null) return;
            JSONArray h = history();
            JSONArray a = activity();
            long maxSeq = prefs.getLong("last_seq", 0L);

            for (int i = 0; i < events.length(); i++) {
                JSONObject e = events.optJSONObject(i);
                if (e == null) continue;
                long seq = e.optLong("seq", 0L);
                if (seq > maxSeq) maxSeq = seq;
                String id = e.optString("id", seq > 0 ? "seq-" + seq : UUID.randomUUID().toString());
                String type = e.optString("type", "");
                String role = e.optString("role", "");
                if (role.isEmpty()) {
                    if (type.toLowerCase(Locale.US).contains("assistant")) role = "assistant";
                    else if (type.toLowerCase(Locale.US).contains("user")) role = "user";
                }
                String text = e.optString("text", "");
                if (text.isEmpty()) {
                    Object c = e.opt("content");
                    if (c instanceof String) text = (String)c;
                    else if (c != null) text = String.valueOf(c);
                }

                if (("assistant".equals(role) || "user".equals(role)) && !text.isEmpty() && !containsId(h, id)) {
                    JSONObject m = new JSONObject();
                    m.put("id", id);
                    m.put("role", role);
                    m.put("text", text);
                    m.put("ts", e.optLong("timestamp", System.currentTimeMillis()));
                    m.put("status", "received");
                    h.put(m);
                }

                JSONObject tool = e.optJSONObject("tool");
                JSONObject specialist = e.optJSONObject("specialist");
                boolean activityEvent = tool != null || specialist != null ||
                        type.toLowerCase(Locale.US).contains("tool") ||
                        type.toLowerCase(Locale.US).contains("mcp") ||
                        type.toLowerCase(Locale.US).contains("specialist");
                if (activityEvent && !containsId(a, id)) {
                    JSONObject x = new JSONObject();
                    x.put("id", id);
                    x.put("name", tool != null ? tool.optString("name", "Tool") :
                            specialist != null ? specialist.optString("name", "Specialist") :
                            e.optString("name", type.isEmpty() ? "Activity" : type));
                    x.put("status", e.optString("status", "completed"));
                    x.put("summary", e.optString("summary", text));
                    x.put("ts", e.optLong("timestamp", System.currentTimeMillis()));
                    a.put(x);
                }
            }
            prefs.edit().putLong("last_seq", maxSeq).apply();
            saveHistory(h);
            saveActivity(a);
        } catch (Exception ignored) {}
    }

    private boolean containsId(JSONArray arr, String id) {
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.optJSONObject(i);
            if (o != null && id.equals(o.optString("id"))) return true;
        }
        return false;
    }

    private void renderMessages() {
        if (messages == null) return;
        messages.removeAllViews();
        try {
            JSONArray h = history();
            for (int i = 0; i < h.length(); i++) {
                JSONObject m = h.optJSONObject(i);
                if (m == null) continue;
                String role = m.optString("role", "assistant");
                LinearLayout row = new LinearLayout(this);
                row.setGravity("user".equals(role) ? Gravity.RIGHT : Gravity.LEFT);
                row.setPadding(0, dp(3), 0, dp(3));

                LinearLayout bubble = new LinearLayout(this);
                bubble.setOrientation(LinearLayout.VERTICAL);
                bubble.setPadding(dp(12), dp(9), dp(12), dp(7));
                int bg = "user".equals(role) ? Color.rgb(8, 52, 72) : PANEL;
                int stroke = "user".equals(role) ? CYAN : Color.rgb(45, 64, 76);
                bubble.setBackground(roundRect(bg, stroke, 1));

                TextView who = label("user".equals(role) ? "YOU" : "GPT / JAYTEC", 10,
                        "user".equals(role) ? CYAN : GREEN, true);
                bubble.addView(who);
                TextView txt = label(m.optString("text", ""), 16, TEXT, false);
                bubble.addView(txt);

                String status = m.optString("status", "");
                if (!status.isEmpty() && "user".equals(role)) {
                    TextView st = label(status.toUpperCase(Locale.US), 9,
                            "auth".equals(status) ? RED : MUTED, false);
                    st.setGravity(Gravity.RIGHT);
                    bubble.addView(st);
                }

                LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                        (int)(getResources().getDisplayMetrics().widthPixels * 0.83),
                        ViewGroup.LayoutParams.WRAP_CONTENT);
                row.addView(bubble, bp);
                messages.addView(row);
            }
            if (chatScroll != null) chatScroll.post(() -> chatScroll.fullScroll(View.FOCUS_DOWN));
        } catch (Exception ignored) {}
    }

    private void renderActivity(String filter) {
        if (activityList == null) return;
        activityList.removeAllViews();
        try {
            JSONArray a = activity();
            for (int i = a.length() - 1; i >= 0; i--) {
                JSONObject x = a.optJSONObject(i);
                if (x == null) continue;
                String status = x.optString("status", "completed").toLowerCase(Locale.US);
                if (!"all".equals(filter)) {
                    if ("completed".equals(filter) && !(status.contains("success") || status.contains("complete"))) continue;
                    if ("running".equals(filter) && !(status.contains("run") || status.contains("queue"))) continue;
                    if ("failed".equals(filter) && !status.contains("fail")) continue;
                }
                LinearLayout card = new LinearLayout(this);
                card.setOrientation(LinearLayout.VERTICAL);
                card.setPadding(dp(12), dp(10), dp(12), dp(10));
                card.setBackground(roundRect(PANEL, status.contains("fail") ? RED : Color.rgb(41, 69, 82), 1));
                TextView name = label(x.optString("name", "Activity"), 14, CYAN, true);
                card.addView(name);
                card.addView(label(status.toUpperCase(Locale.US), 10,
                        status.contains("fail") ? RED : GREEN, true));
                String summary = x.optString("summary", "");
                if (!summary.isEmpty()) card.addView(label(summary, 13, TEXT, false));
                LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
                cp.setMargins(0, 0, 0, dp(8));
                activityList.addView(card, cp);
            }
        } catch (Exception ignored) {}
    }

    private void testConnection() {
        saveSettings();
        io.execute(() -> {
            try {
                HttpResult r = request("GET", "/v1/health", null);
                runOnUiThread(() -> {
                    if (r.code >= 200 && r.code < 300) {
                        toast("Gateway online");
                        setOnline("ONLINE");
                    } else if (r.code == 401) {
                        toast("Gateway reachable, token rejected");
                        setOnline("AUTH");
                    } else {
                        toast("Health check failed: HTTP " + r.code);
                        setOnline("OFFLINE");
                    }
                    refreshDiagnostics();
                });
            } catch (Exception e) {
                runOnUiThread(() -> toast("Connection failed: " + e.getMessage()));
                setOnline("OFFLINE");
            }
        });
    }

    private HttpResult request(String method, String path, String body) throws Exception {
        String base = prefs.getString("gateway", "");
        if (!validateStoredGateway(base)) return new HttpResult(0, "Gateway not configured");
        URL url = new URL(stripSlash(base) + path);
        HttpURLConnection c = (HttpURLConnection) url.openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(12000);
        c.setRequestMethod(method);
        c.setRequestProperty("Accept", "application/json");
        String token = tokenStore.load();
        if (token != null && !token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
        if (body != null) {
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            try (OutputStream os = c.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = c.getResponseCode();
        InputStream stream = code >= 200 && code < 400 ? c.getInputStream() : c.getErrorStream();
        StringBuilder sb = new StringBuilder();
        if (stream != null) {
            try (BufferedReader br = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                String line;
                while ((line = br.readLine()) != null) sb.append(line);
            }
        }
        c.disconnect();
        return new HttpResult(code, sb.toString());
    }

    private boolean validateStoredGateway(String value) {
        if (value == null || value.isEmpty()) return false;
        if (value.startsWith("https://")) return true;
        return value.startsWith("http://") && prefs.getBoolean("allow_http", false) && isPrivateLan(value);
    }

    private void refreshDiagnostics() {
        if (diagnostics == null) return;
        String conv = prefs.getString("conversation_id", "");
        long seq = prefs.getLong("last_seq", 0L);
        long sync = prefs.getLong("last_sync", 0L);
        String when = sync == 0 ? "never" : new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date(sync));
        String gateway = prefs.getString("gateway", "");
        diagnostics.setText(
                "Gateway: " + (gateway.isEmpty() ? "not configured" : redactUrl(gateway)) +
                "\nToken: " + (tokenStore.hasToken() ? "stored securely" : "not set") +
                "\nConversation: " + (conv.isEmpty() ? "not assigned" : conv) +
                "\nLast sequence: " + seq +
                "\nLast sync: " + when +
                "\nSync cadence: 1 second (foreground chat)" +
                "\nApp: 0.1.0");
    }

    private String redactUrl(String s) {
        try {
            URL u = new URL(s);
            int p = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + (p > 0 ? ":" + p : "");
        } catch (Exception e) {
            return "configured";
        }
    }

    private void updateTopStatus() {
        String gateway = prefs.getString("gateway", "");
        if (gateway.isEmpty()) setOnline("OFFLINE");
        else if (prefs.getLong("last_sync", 0L) > 0) setOnline("ONLINE");
        else setOnline("READY");
    }

    private void setOnline(String state) {
        runOnUiThread(() -> {
            if (topStatus == null) return;
            topStatus.setText(state);
            if ("ONLINE".equals(state)) topStatus.setTextColor(GREEN);
            else if ("AUTH".equals(state) || "OFFLINE".equals(state)) topStatus.setTextColor(RED);
            else topStatus.setTextColor(CYAN);
        });
    }

    private JSONArray history() {
        try { return new JSONArray(prefs.getString("history", "[]")); }
        catch (Exception e) { return new JSONArray(); }
    }

    private JSONArray activity() {
        try { return new JSONArray(prefs.getString("activity", "[]")); }
        catch (Exception e) { return new JSONArray(); }
    }

    private void saveHistory(JSONArray a) { prefs.edit().putString("history", a.toString()).apply(); }
    private void saveActivity(JSONArray a) { prefs.edit().putString("activity", a.toString()).apply(); }

    private JSONObject safeObject(String s) {
        try { return new JSONObject(s == null || s.isEmpty() ? "{}" : s); }
        catch (Exception e) { return new JSONObject(); }
    }

    private String stripSlash(String s) {
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    private View space(int d) {
        View v = new View(this);
        v.setLayoutParams(new LinearLayout.LayoutParams(1, dp(d)));
        return v;
    }

    private EditText input(String value) {
        EditText e = new EditText(this);
        e.setText(value);
        e.setTextColor(TEXT);
        e.setHintTextColor(MUTED);
        e.setTextSize(15);
        e.setSingleLine(true);
        e.setPadding(dp(12), dp(10), dp(12), dp(10));
        e.setBackground(roundRect(PANEL, Color.rgb(47, 68, 81), 1));
        return e;
    }

    private TextView label(String s, int size, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(size);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private Button button(String text) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextColor(TEXT);
        b.setTextSize(12);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setBackground(roundRect(Color.rgb(9, 35, 48), CYAN, 1));
        b.setAllCaps(false);
        return b;
    }

    private GradientDrawable roundRect(int fill, int stroke, int width) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(10));
        g.setStroke(dp(width), stroke);
        return g;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    private static class HttpResult {
        final int code;
        final String body;
        HttpResult(int c, String b) { code = c; body = b; }
    }

    private static class SecureTokenStore {
        private static final String KEY_ALIAS = "jaytec_chat_token_key";
        private static final String PREFS = "jaytec_chat_secure";
        private static final String CIPHER = "AES/GCM/NoPadding";
        private final Context ctx;

        SecureTokenStore(Context c) { ctx = c.getApplicationContext(); }

        boolean hasToken() {
            SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            return p.contains("ciphertext") && p.contains("iv");
        }

        void save(String token) throws Exception {
            SecretKey key = getOrCreateKey();
            Cipher cipher = Cipher.getInstance(CIPHER);
            cipher.init(Cipher.ENCRYPT_MODE, key);
            byte[] encrypted = cipher.doFinal(token.getBytes(StandardCharsets.UTF_8));
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                    .putString("ciphertext", Base64.encodeToString(encrypted, Base64.NO_WRAP))
                    .putString("iv", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
                    .apply();
        }

        String load() throws Exception {
            SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            String ct = p.getString("ciphertext", "");
            String iv = p.getString("iv", "");
            if (ct.isEmpty() || iv.isEmpty()) return "";
            SecretKey key = getOrCreateKey();
            Cipher cipher = Cipher.getInstance(CIPHER);
            cipher.init(Cipher.DECRYPT_MODE, key,
                    new GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)));
            byte[] clear = cipher.doFinal(Base64.decode(ct, Base64.NO_WRAP));
            return new String(clear, StandardCharsets.UTF_8);
        }

        private SecretKey getOrCreateKey() throws Exception {
            KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
            ks.load(null);
            if (ks.containsAlias(KEY_ALIAS)) {
                return ((KeyStore.SecretKeyEntry)ks.getEntry(KEY_ALIAS, null)).getSecretKey();
            }
            KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            kg.init(new KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build());
            return kg.generateKey();
        }
    }
}
