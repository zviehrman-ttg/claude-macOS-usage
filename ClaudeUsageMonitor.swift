import Cocoa

// MARK: - JSON Models

struct BackendState: Codable {
    let tier: String?
    let username: String?
    let orgId: String?
    let hasSession: Bool?
    let hasCliCreds: Bool?
    let availableOrgs: [[String: String]]?
    let titleIcon: String?
    let menuItems: [MenuItem]?
    let message: String?
    let error: String?
    let success: Bool?
    let instructions: String?

    enum CodingKeys: String, CodingKey {
        case tier, username, message, error, success, instructions
        case orgId = "org_id"
        case hasSession = "has_session"
        case hasCliCreds = "has_cli_creds"
        case availableOrgs = "available_orgs"
        case titleIcon = "title_icon"
        case menuItems = "menu_items"
    }
}

struct MenuItem: Codable {
    let type: String
    let title: String?
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var backendDir: String = ""
    var pythonPath: String = ""
    var refreshTimer: Timer?
    var currentOrgId: String?
    var hasSession: Bool = false
    var availableOrgs: [[String: String]] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Find paths
        let executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        let appDir: String
        if executableURL.pathComponents.contains("Contents") {
            // Running from .app bundle
            appDir = executableURL
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .deletingLastPathComponent().path
        } else {
            appDir = executableURL.deletingLastPathComponent().path
        }
        backendDir = appDir
        pythonPath = "\(appDir)/.venv/bin/python3"

        // Check if venv exists
        if !FileManager.default.fileExists(atPath: pythonPath) {
            // Fallback: try system python
            pythonPath = "/usr/bin/python3"
        }

        // Create status item
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "\u{2728}"

        // Build initial loading menu
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Loading...", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "q"))
        statusItem.menu = menu

        // Run init in background
        runBackend(command: "init") { [weak self] state in
            self?.applyState(state)
        }

        // Auto-refresh every 5 minutes
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            self?.refreshData()
        }
    }

    // MARK: - Backend Communication

    func runBackend(command: String, args: [String] = [], completion: @escaping (BackendState) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }

            let process = Process()
            process.executableURL = URL(fileURLWithPath: self.pythonPath)
            process.arguments = ["\(self.backendDir)/backend.py", command] + args
            process.currentDirectoryURL = URL(fileURLWithPath: self.backendDir)

            // Pass through environment for keychain access
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = self.backendDir
            process.environment = env

            let pipe = Pipe()
            let errPipe = Pipe()
            process.standardOutput = pipe
            process.standardError = errPipe

            do {
                try process.run()
                process.waitUntilExit()

                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                let errData = errPipe.fileHandleForReading.readDataToEndOfFile()

                if let errStr = String(data: errData, encoding: .utf8), !errStr.isEmpty {
                    NSLog("Backend stderr: %@", errStr)
                }

                if let state = try? JSONDecoder().decode(BackendState.self, from: data) {
                    DispatchQueue.main.async {
                        completion(state)
                    }
                } else {
                    let raw = String(data: data, encoding: .utf8) ?? "no output"
                    NSLog("Failed to parse backend output: %@", raw)
                    DispatchQueue.main.async {
                        completion(BackendState(
                            tier: nil, username: nil, orgId: nil, hasSession: nil,
                            hasCliCreds: nil, availableOrgs: nil, titleIcon: nil,
                            menuItems: nil, message: nil,
                            error: "Backend error: \(raw)", success: nil, instructions: nil
                        ))
                    }
                }
            } catch {
                NSLog("Failed to run backend: %@", error.localizedDescription)
                DispatchQueue.main.async {
                    completion(BackendState(
                        tier: nil, username: nil, orgId: nil, hasSession: nil,
                        hasCliCreds: nil, availableOrgs: nil, titleIcon: nil,
                        menuItems: nil, message: nil,
                        error: "Failed to run backend: \(error.localizedDescription)",
                        success: nil, instructions: nil
                    ))
                }
            }
        }
    }

    // MARK: - State Management

    func applyState(_ state: BackendState) {
        // Update title icon
        if let icon = state.titleIcon {
            statusItem.button?.title = icon
        }

        // Track state
        if let org = state.orgId {
            currentOrgId = org
        }
        if let session = state.hasSession {
            hasSession = session
        }
        if let orgs = state.availableOrgs {
            availableOrgs = orgs
        }

        // Build menu from items
        let menu = NSMenu()

        if let items = state.menuItems {
            for item in items {
                if item.type == "separator" {
                    menu.addItem(NSMenuItem.separator())
                } else {
                    let menuItem = NSMenuItem(title: item.title ?? "", action: nil, keyEquivalent: "")
                    menu.addItem(menuItem)
                }
            }
        }

        // Add last-updated timestamp
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        let tsItem = NSMenuItem(title: "Last updated: \(formatter.string(from: Date()))", action: nil, keyEquivalent: "")
        menu.addItem(tsItem)
        menu.addItem(NSMenuItem.separator())

        // Action items
        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refreshData), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let settingsItem = NSMenuItem(title: "Open claude.ai/settings/usage", action: #selector(openSettings), keyEquivalent: "")
        settingsItem.target = self
        menu.addItem(settingsItem)

        // Org switcher
        if availableOrgs.count > 1 {
            let switchMenu = NSMenu()
            for org in availableOrgs {
                let orgId = org["org_id"] ?? ""
                let orgName = org["name"] ?? orgId
                let prefix = (orgId == currentOrgId) ? "\u{25CF} " : "   "
                let orgItem = NSMenuItem(title: "\(prefix)\(orgName)", action: #selector(switchOrg(_:)), keyEquivalent: "")
                orgItem.target = self
                orgItem.representedObject = orgId
                switchMenu.addItem(orgItem)
            }
            let switchItem = NSMenuItem(title: "Switch Organization", action: nil, keyEquivalent: "")
            switchItem.submenu = switchMenu
            menu.addItem(switchItem)
        }

        // Session management
        if hasSession {
            let disconnectItem = NSMenuItem(title: "Disconnect Session", action: #selector(disconnectSession), keyEquivalent: "")
            disconnectItem.target = self
            menu.addItem(disconnectItem)
        } else {
            let connectItem = NSMenuItem(title: "Connect claude.ai Session...", action: #selector(connectSession), keyEquivalent: "")
            connectItem.target = self
            menu.addItem(connectItem)
        }

        menu.addItem(NSMenuItem.separator())
        let quitItem = NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu

        // Show notification if there's a message
        if let msg = state.message {
            showNotification(subtitle: msg)
        }
        if let err = state.error {
            NSLog("Backend error: %@", err)
        }
    }

    // MARK: - Actions

    @objc func refreshData() {
        var args: [String] = []
        if let orgId = currentOrgId {
            args.append(orgId)
        }
        runBackend(command: "refresh", args: args) { [weak self] state in
            self?.applyState(state)
        }
    }

    @objc func openSettings() {
        if let url = URL(string: "https://claude.ai/settings/usage") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func connectSession() {
        // First try Chrome auto-extract
        runBackend(command: "connect-chrome") { [weak self] state in
            if state.success == true {
                self?.applyState(state)
            } else {
                // Show manual connect dialog
                self?.showManualConnectDialog()
            }
        }
    }

    func showManualConnectDialog() {
        let alert = NSAlert()
        alert.messageText = "Connect claude.ai Session"
        alert.informativeText = """
            To connect your claude.ai account:

            1. Open claude.ai in your browser and log in
            2. Open DevTools (Cmd+Option+I)
            3. Go to Application > Cookies > claude.ai
            4. Find the 'sessionKey' cookie
            5. Copy its Value and paste it below

            Stored securely in your macOS Keychain.
            """
        alert.addButton(withTitle: "Connect")
        alert.addButton(withTitle: "Cancel")

        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 380, height: 24))
        input.placeholderString = "Paste sessionKey here"
        alert.accessoryView = input

        // Bring to front
        NSApp.activate(ignoringOtherApps: true)

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            let key = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: "'\""))
            if !key.isEmpty {
                runBackend(command: "connect-manual", args: [key]) { [weak self] state in
                    if state.success == true {
                        self?.applyState(state)
                    } else {
                        self?.showNotification(subtitle: state.error ?? "Connection failed")
                    }
                }
            }
        }
    }

    @objc func disconnectSession() {
        runBackend(command: "disconnect") { [weak self] state in
            self?.applyState(state)
        }
    }

    @objc func switchOrg(_ sender: NSMenuItem) {
        guard let orgId = sender.representedObject as? String else { return }
        runBackend(command: "switch-org", args: [orgId]) { [weak self] state in
            self?.applyState(state)
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }

    // MARK: - Notifications

    func showNotification(subtitle: String) {
        let notification = NSUserNotification()
        notification.title = "Claude Usage Monitor"
        notification.subtitle = subtitle
        NSUserNotificationCenter.default.deliver(notification)
    }
}

// MARK: - Main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
