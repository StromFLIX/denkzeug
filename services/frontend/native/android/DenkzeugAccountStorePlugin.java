package dev.nautionette.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.util.Collections;
import java.util.UUID;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Refresh credentials only. AES key is non-exportable and owned by Android Keystore. */
@CapacitorPlugin(name = "DenkzeugAccountStore")
public class DenkzeugAccountStorePlugin extends Plugin {
    private static final String ALIAS = "denkzeug.account.refresh.v1";
    private final String capability = UUID.randomUUID().toString() + UUID.randomUUID().toString();
    private boolean scopedBridge = false;
    @Override public void load() {
        // Capacitor has a legacy addJavascriptInterface fallback exposed to subframes.
        // A document-start capability exists only in our bundled top-level document,
        // so untrusted subframes cannot call this vault even on that fallback.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            try {
                String script = "if (window === window.top) Object.defineProperty(globalThis, 'denkzeugAccountCapability', {value:'" + capability + "'});";
                WebViewCompat.addDocumentStartJavaScript(getBridge().getWebView(), script, Collections.singleton("https://localhost"));
                scopedBridge = true;
            } catch (RuntimeException ignored) { /* Refuse secret operations if scoping failed. */ }
        }
    }
    private SharedPreferences preferences() { return getContext().getSharedPreferences("denkzeug.account.secrets", Context.MODE_PRIVATE); }
    private String origin(PluginCall call) throws Exception {
        if (!scopedBridge || !capability.equals(call.getString("capability"))) throw new SecurityException("Untrusted account caller. Update Android System WebView if needed.");
        String value = call.getString("origin", "");
        URI uri = new URI(value);
        if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null || !uri.getRawPath().isEmpty()) throw new IllegalArgumentException("Invalid account origin");
        return value;
    }
    private synchronized SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore"); store.load(null);
        if (!store.containsAlias(ALIAS)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setRandomizedEncryptionRequired(true).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(ALIAS, null);
    }
    @PluginMethod public void get(PluginCall call) {
        try {
            String origin = origin(call), stored = preferences().getString(origin, null), value = "";
            if (stored != null) {
                String[] parts = stored.split(":", 2);
                Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
                cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)));
                cipher.updateAAD(origin.getBytes(StandardCharsets.UTF_8));
                value = new String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
            }
            JSObject result = new JSObject(); result.put("value", value); call.resolve(result);
        } catch (Exception error) { call.reject("Secure account storage unavailable. Sign in again or unlock your device."); }
    }
    @PluginMethod public void set(PluginCall call) {
        try {
            String origin = origin(call), value = call.getString("value", "");
            if (value.isEmpty() || value.length() > 16384) throw new IllegalArgumentException();
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key());
            cipher.updateAAD(origin.getBytes(StandardCharsets.UTF_8));
            String encrypted = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP) + ":" + Base64.encodeToString(cipher.doFinal(value.getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);
            if (!preferences().edit().putString(origin, encrypted).commit()) throw new IllegalStateException();
            call.resolve();
        } catch (Exception error) { call.reject("Could not save your account in Android Keystore."); }
    }
    @PluginMethod public void remove(PluginCall call) {
        try { if (!preferences().edit().remove(origin(call)).commit()) throw new IllegalStateException(); call.resolve(); }
        catch (Exception error) { call.reject("Could not remove the saved account."); }
    }
}
