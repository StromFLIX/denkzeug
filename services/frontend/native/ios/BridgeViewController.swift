import Capacitor
import Security
import WebKit

/// Vue Router owns the same history, including scoped lists and deep links.
class BridgeViewController: CAPBridgeViewController {
    private let accountCapability = UUID().uuidString + UUID().uuidString
    override func webView(with frame: CGRect, configuration: WKWebViewConfiguration) -> WKWebView {
        // WK script message handlers are visible to subframes. Keep a vault capability
        // only in the bundled main frame, never in generated/opaque mini-app frames.
        let source = "if (location.protocol === 'capacitor:' && location.hostname === 'localhost') Object.defineProperty(globalThis, 'denkzeugAccountCapability', {value:'\(accountCapability)'});"
        configuration.userContentController.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        return super.webView(with: frame, configuration: configuration)
    }
    override func capacitorDidLoad() {
        super.capacitorDidLoad()
        webView?.allowsBackForwardNavigationGestures = true
        let store = DenkzeugAccountStorePlugin()
        store.capability = accountCapability
        bridge?.registerPluginInstance(store)
    }
}

/// The Keychain item never syncs to iCloud or migrates to another device.
@objc(DenkzeugAccountStorePlugin)
public class DenkzeugAccountStorePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "DenkzeugAccountStorePlugin"
    public let jsName = "DenkzeugAccountStore"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "get", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "set", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "remove", returnType: CAPPluginReturnPromise)
    ]
    fileprivate var capability = ""
    private func query(_ call: CAPPluginCall) throws -> [String: Any] {
        guard !capability.isEmpty, call.getString("capability") == capability,
              let origin = call.getString("origin"), let url = URLComponents(string: origin),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil,
              url.path.isEmpty, url.query == nil, url.fragment == nil else {
            throw NSError(domain: "DenkzeugAccountStore", code: 1)
        }
        return [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "dev.nautionette.app.account.refresh.v1",
                kSecAttrAccount as String: origin]
    }
    @objc func get(_ call: CAPPluginCall) {
        do {
            var item = try query(call)
            item[kSecReturnData as String] = true
            item[kSecMatchLimit as String] = kSecMatchLimitOne
            var data: CFTypeRef?
            let status = SecItemCopyMatching(item as CFDictionary, &data)
            if status == errSecItemNotFound { call.resolve(["value": ""]); return }
            guard status == errSecSuccess, let bytes = data as? Data, let value = String(data: bytes, encoding: .utf8) else {
                call.reject("Unlock your device to open the saved account."); return
            }
            call.resolve(["value": value])
        } catch { call.reject("Invalid secure account storage request.") }
    }
    @objc func set(_ call: CAPPluginCall) {
        do {
            let item = try query(call)
            guard let value = call.getString("value"), !value.isEmpty, value.utf8.count <= 16384 else {
                call.reject("Invalid account credential."); return
            }
            let attributes: [String: Any] = [kSecValueData as String: Data(value.utf8),
                kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
            let status = SecItemUpdate(item as CFDictionary, attributes as CFDictionary)
            if status == errSecItemNotFound {
                let combined = item.merging(attributes) { _, replacement in replacement }
                guard SecItemAdd(combined as CFDictionary, nil) == errSecSuccess else {
                    call.reject("Could not save your account in Keychain."); return
                }
            } else if status != errSecSuccess { call.reject("Could not update your account in Keychain."); return }
            call.resolve()
        } catch { call.reject("Invalid secure account storage request.") }
    }
    @objc func remove(_ call: CAPPluginCall) {
        do {
            let status = SecItemDelete(try query(call) as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                call.reject("Could not remove the saved account."); return
            }
            call.resolve()
        } catch { call.reject("Invalid secure account storage request.") }
    }
}
