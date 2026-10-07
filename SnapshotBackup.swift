import Cocoa

final class PayloadButton: NSButton { var payload = "" }

let fm = FileManager.default
let agentRoot = Bundle.main.bundleURL.deletingLastPathComponent().path
let dataRoot = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Snapshot Backup").path
let defaultBackupRoot = fm.homeDirectoryForCurrentUser.appendingPathComponent("SnapshotBackups").path
let pythonCandidates = ["/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python3"]
let python = pythonCandidates.first(where: { fm.isExecutableFile(atPath: $0) }) ?? "/usr/bin/python3"

func configuredBackupRoot() -> String {
    let cfg = readJSON(dataRoot + "/config.json") ?? [:]
    return cfg["backup_root"] as? String ?? defaultBackupRoot
}

func readJSON(_ path: String) -> [String: Any]? {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
    return obj
}
func runProcess(_ executable: String, _ args: [String], completion: (() -> Void)? = nil) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: executable)
    p.arguments = args
    if let completion = completion {
        p.terminationHandler = { _ in DispatchQueue.main.async { completion() } }
    }
    do { try p.run() } catch { completion?() }
}
func runProcessCapture(_ executable: String, _ args: [String], completion: @escaping (Int32,String,String) -> Void) {
    let p = Process()
    let out = Pipe()
    let err = Pipe()
    p.executableURL = URL(fileURLWithPath: executable)
    p.arguments = args
    p.standardOutput = out
    p.standardError = err
    p.terminationHandler = { proc in
        let stdout = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let stderr = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        DispatchQueue.main.async { completion(proc.terminationStatus, stdout, stderr) }
    }
    do { try p.run() }
    catch { completion(-1, "", error.localizedDescription) }
}

func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular, color: NSColor = .labelColor) -> NSTextField {
    let l = NSTextField(labelWithString: text)
    l.font = .systemFont(ofSize: size, weight: weight)
    l.textColor = color
    l.lineBreakMode = .byTruncatingMiddle
    return l
}
func clear(_ stack: NSStackView) {
    for v in stack.arrangedSubviews { stack.removeArrangedSubview(v); v.removeFromSuperview() }
}
func humanBytes(_ n: NSNumber?) -> String {
    guard let n = n else { return "—" }
    return ByteCountFormatter.string(fromByteCount: n.int64Value, countStyle: .file)
}

final class ManagerController: NSObject {
    let window: NSWindow
    let content = NSStackView()
    let body = NSStackView()
    let segmented = NSSegmentedControl(labels: ["Backups", "Local Data Discovery"], trackingMode: .selectOne, target: nil, action: nil)
    var scanning = false
    var showCovered = false

    override init() {
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:820,height:620),
                          styleMask:[.titled,.closable,.miniaturizable,.resizable],
                          backing:.buffered,defer:false)
        window.title = "Snapshot Backup"
        window.minSize = NSSize(width:680,height:480)
        super.init()
        build()
    }

    func build() {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 16
        root.edgeInsets = NSEdgeInsets(top:22,left:24,bottom:22,right:24)
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = NSView()
        window.contentView?.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor)
        ])

        let titleRow=NSStackView(); titleRow.orientation = .horizontal; titleRow.alignment = .centerY; titleRow.spacing=10
        let icon=NSImageView(image:NSImage(systemSymbolName:"externaldrive.badge.timemachine",accessibilityDescription:nil)!)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize:25,weight:.medium)
        titleRow.addArrangedSubview(icon)
        let titles=NSStackView(); titles.orientation = .vertical; titles.spacing=2
        titles.addArrangedSubview(label("Snapshot Backup",size:23,weight:.semibold))
        titles.addArrangedSubview(label("Local-first snapshots with cloud redundancy",size:12,color:.secondaryLabelColor))
        titleRow.addArrangedSubview(titles)
        root.addArrangedSubview(titleRow)

        segmented.selectedSegment = 0
        segmented.target=self; segmented.action=#selector(tabChanged)
        root.addArrangedSubview(segmented)

        let scroll=NSScrollView()
        scroll.hasVerticalScroller=true
        scroll.drawsBackground=false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints=false
        body.orientation = .vertical; body.alignment = .leading; body.spacing=10
        body.translatesAutoresizingMaskIntoConstraints=false
        let clip=NSClipView(); clip.drawsBackground=false; clip.documentView=body
        scroll.contentView=clip
        root.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalTo:root.widthAnchor,constant:-48).isActive=true
        scroll.setContentHuggingPriority(.defaultLow,for:.vertical)
        NSLayoutConstraint.activate([
            body.leadingAnchor.constraint(equalTo:scroll.contentView.leadingAnchor),
            body.trailingAnchor.constraint(equalTo:scroll.contentView.trailingAnchor),
            body.topAnchor.constraint(equalTo:scroll.contentView.topAnchor),
            body.widthAnchor.constraint(equalTo:scroll.contentView.widthAnchor)
        ])
        renderBackups()
    }

    func show(tab:Int=0) {
        segmented.selectedSegment=tab
        if tab==0 { renderBackups() } else { renderDiscovery() }
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps:true)
    }

    @objc func tabChanged() {
        if segmented.selectedSegment==0 { renderBackups() } else { renderDiscovery() }
    }

    func sectionHeader(_ title:String,_ subtitle:String?=nil) {
        body.addArrangedSubview(label(title,size:16,weight:.semibold))
        if let subtitle=subtitle { body.addArrangedSubview(label(subtitle,size:11,color:.secondaryLabelColor)) }
    }

    func card() -> NSStackView {
        let s=NSStackView(); s.orientation = .vertical; s.alignment = .leading; s.spacing=5
        s.edgeInsets=NSEdgeInsets(top:12,left:14,bottom:12,right:14)
        s.wantsLayer=true; s.layer?.cornerRadius=10
        s.layer?.backgroundColor=NSColor.controlBackgroundColor.cgColor
        return s
    }

    func renderBackups() {
        clear(body)
        let config=readJSON(dataRoot+"/config.json") ?? [:]
        let jobs=config["jobs"] as? [[String:Any]] ?? []
        sectionHeader("Backup Jobs", String(jobs.count) + " configured · nightly scheduler at 9:00 PM")
        for job in jobs {
            let name=job["name"] as? String ?? "Backup"
            let type=job["type"] as? String ?? "folder"
            let source=job["source"] as? String ?? ""
            let state=readJSON(dataRoot+"/state/"+name+".json") ?? [:]
            let result=state["last_result"] as? String ?? "not run"
            let snap=state["last_snapshot"] as? String ?? "Never"
            let c=card()
            let top=NSStackView(); top.orientation = .horizontal; top.alignment = .centerY
            let typeIcon = type=="folder" ? "📁" : "🗄️"
            top.addArrangedSubview(label(typeIcon + " " + name,size:15,weight:.semibold))
            let spacer=NSView(); spacer.setContentHuggingPriority(.defaultLow,for:.horizontal); top.addArrangedSubview(spacer)
            let statusColor: NSColor = result=="success" ? .systemGreen : (result=="error" ? .systemRed : .secondaryLabelColor)
            top.addArrangedSubview(label(result.uppercased(),size:10,weight:.semibold,color:statusColor))
            c.addArrangedSubview(top)
            let typeLabel = type=="sqlite" ? "SQLite safe snapshot" : (type=="postgres" ? "PostgreSQL logical dump" : (type=="docker-postgres" ? "Docker PostgreSQL logical dump" : "Folder snapshot"))
            c.addArrangedSubview(label(typeLabel,size:11,color:.secondaryLabelColor))
            c.addArrangedSubview(label(source,size:11,color:.secondaryLabelColor))
            c.addArrangedSubview(label("Last snapshot: "+snap,size:11,color:.tertiaryLabelColor))
            let actions=NSStackView(); actions.orientation = .horizontal; actions.spacing=7
            let run=PayloadButton(title:"Run Now",target:self,action:#selector(runJob(_:))); run.bezelStyle = .rounded; run.payload=name
            actions.addArrangedSubview(run)
            if type=="folder" || type=="sqlite" {
                let reveal=PayloadButton(title:"Reveal Source",target:self,action:#selector(revealSource(_:))); reveal.bezelStyle = .rounded; reveal.payload=source
                actions.addArrangedSubview(reveal)
            }
            let remove=PayloadButton(title:"Remove…",target:self,action:#selector(removeJob(_:))); remove.bezelStyle = .rounded; remove.payload=name
            actions.addArrangedSubview(remove)
            c.addArrangedSubview(actions)
            body.addArrangedSubview(c)
            c.widthAnchor.constraint(equalTo:body.widthAnchor).isActive=true
        }
        let footer=NSStackView(); footer.orientation = .horizontal; footer.spacing=8
        let addFolder=NSButton(title:"＋ Add Folder…",target:self,action:#selector(addFolder)); addFolder.bezelStyle = .rounded
        let refresh=NSButton(title:"Refresh",target:self,action:#selector(refreshBackups)); refresh.bezelStyle = .rounded
        let rawConfig=NSButton(title:"Open Raw Config",target:self,action:#selector(openRawConfig)); rawConfig.bezelStyle = .rounded
        footer.addArrangedSubview(addFolder); footer.addArrangedSubview(refresh); footer.addArrangedSubview(rawConfig)
        body.addArrangedSubview(footer)
    }

    func renderDiscovery() {
        clear(body)
        let d=readJSON(dataRoot+"/state/database_discovery.json")
        let items=d?["items"] as? [[String:Any]] ?? []
        let scanned=d?["scanned"] as? String ?? "Never"
        let uncovered=d?["uncovered_count"] as? Int ?? 0
        sectionHeader("Local Data Discovery", "Last scan: " + scanned + " · " + String(items.count) + " candidates · " + String(uncovered) + " need attention")

        let tools=NSStackView(); tools.orientation = .horizontal; tools.spacing=8
        let scan=NSButton(title: scanning ? "Scanning…" : "Scan This Mac",target:self,action:#selector(scanDatabases))
        scan.bezelStyle = .rounded; scan.isEnabled = !scanning
        tools.addArrangedSubview(scan)
        let toggle=NSButton(title: showCovered ? "Show Needs Attention" : "Show All",target:self,action:#selector(toggleCovered))
        toggle.bezelStyle = .rounded
        tools.addArrangedSubview(toggle)
        body.addArrangedSubview(tools)

        if items.isEmpty {
            body.addArrangedSubview(label("Run a scan to find SQLite, DuckDB, Realm, LMDB, Redis, LevelDB/RocksDB and PostgreSQL data.",size:12,color:.secondaryLabelColor))
            return
        }

        let displayItems = showCovered ? items : items.filter {
            let coverage = (($0["coverage"] as? [[String:Any]]) ?? [])
            let attention = $0["attention"] as? Bool ?? true
            return coverage.isEmpty && attention
        }
        for item in displayItems.prefix(300) {
            let path=item["path"] as? String ?? ""
            let kind=item["kind"] as? String ?? "Database"
            let score=item["score"] as? Int ?? 0
            let coverage=item["coverage"] as? [[String:Any]] ?? []
            let safe=item["safe_mode"] as? String ?? ""
            let size=humanBytes(item["size"] as? NSNumber)
            let recommendation=item["recommendation"] as? String ?? ""
            let c=card()
            let top=NSStackView(); top.orientation = .horizontal; top.alignment = .centerY
            top.addArrangedSubview(label(kind,size:14,weight:.semibold))
            let spacer=NSView(); spacer.setContentHuggingPriority(.defaultLow,for:.horizontal); top.addArrangedSubview(spacer)
            if !recommendation.isEmpty {
                let recommendationColor:NSColor
                if recommendation=="Recommended" {
                    recommendationColor = .systemBlue
                } else if recommendation=="Likely testing DB" {
                    recommendationColor = .systemYellow
                } else {
                    recommendationColor = .secondaryLabelColor
                }
                top.addArrangedSubview(label(recommendation.uppercased(),size:9,weight:.bold,color:recommendationColor))
            }
            let badge = coverage.isEmpty ? "UNPROTECTED" : "COVERED"
            let badgeColor:NSColor = coverage.isEmpty ? .systemOrange : .systemGreen
            top.addArrangedSubview(label(badge,size:9,weight:.bold,color:badgeColor))
            c.addArrangedSubview(top)
            c.addArrangedSubview(label(path,size:11,color:.secondaryLabelColor))
            let coverageText = coverage.compactMap{$0["job"] as? String}.joined(separator:", ")
            c.addArrangedSubview(label(size + " · relevance " + String(score) + "/100 · " + safe,size:10,color:.tertiaryLabelColor))
            if !coverageText.isEmpty { c.addArrangedSubview(label("Covered by: "+coverageText,size:10,color:.secondaryLabelColor)) }

            let actions=NSStackView(); actions.orientation = .horizontal; actions.spacing=7
            if kind=="SQLite" || kind=="Database file" {
                let safeConfigured=item["sqlite_safe_configured"] as? Bool ?? false
                if !safeConfigured {
                    let title = kind=="SQLite" ? "Add SQLite-Safe Backup" : "Test / Add as SQLite"
                    let add=PayloadButton(title:title,target:self,action:#selector(addSQLite(_:)))
                    add.bezelStyle = .rounded; add.payload=path
                    actions.addArrangedSubview(add)
                } else {
                    c.addArrangedSubview(label("✓ Dedicated SQLite-safe backup configured",size:10,weight:.semibold,color:.systemGreen))
                }
            } else if kind=="PostgreSQL database" {
                let safeConfigured=item["postgres_safe_configured"] as? Bool ?? false
                if !safeConfigured {
                    let add=PayloadButton(title:"Add PostgreSQL Backup",target:self,action:#selector(addPostgres(_:)))
                    add.bezelStyle = .rounded; add.payload=path
                    actions.addArrangedSubview(add)
                } else {
                    c.addArrangedSubview(label("✓ Dedicated pg_dump backup configured",size:10,weight:.semibold,color:.systemGreen))
                }
            } else if kind=="Docker PostgreSQL database" {
                let safeConfigured=item["postgres_safe_configured"] as? Bool ?? false
                if !safeConfigured {
                    let add=PayloadButton(title:"Add Docker PostgreSQL Backup",target:self,action:#selector(addDockerPostgres(_:)))
                    add.bezelStyle = .rounded; add.payload=path
                    actions.addArrangedSubview(add)
                } else {
                    c.addArrangedSubview(label("✓ Dedicated Docker pg_dump backup configured",size:10,weight:.semibold,color:.systemGreen))
                }
            }
            let ignore=PayloadButton(title:"Ignore This Item",target:self,action:#selector(ignoreDatabase(_:)))
            ignore.bezelStyle = .rounded; ignore.payload=path
            actions.addArrangedSubview(ignore)
            c.addArrangedSubview(actions)
            body.addArrangedSubview(c)
            c.widthAnchor.constraint(equalTo:body.widthAnchor).isActive=true
        }
    }

    @objc func refreshBackups(){ renderBackups() }

    @objc func addFolder(){
        let panel=NSOpenPanel()
        panel.title="Choose a Folder to Back Up"
        panel.prompt="Add Backup"
        panel.canChooseFiles=false
        panel.canChooseDirectories=true
        panel.allowsMultipleSelection=false
        panel.canCreateDirectories=false
        guard panel.runModal() == .OK, let url=panel.url else { return }

        runProcessCapture(python,[agentRoot+"/backupctl.py","add-folder",url.path]) { code,out,err in
            if code==0 {
                self.renderBackups()
            } else {
                let alert=NSAlert()
                alert.messageText="Couldn’t add that folder"
                let detail = err.trimmingCharacters(in:.whitespacesAndNewlines)
                alert.informativeText = detail.isEmpty ? "The folder overlaps an existing backup or could not be configured." : detail
                alert.alertStyle = .warning
                alert.addButton(withTitle:"OK")
                alert.runModal()
            }
        }
    }

    @objc func removeJob(_ sender:PayloadButton){
        let name=sender.payload
        let config=readJSON(dataRoot+"/config.json") ?? [:]
        let jobs=config["jobs"] as? [[String:Any]] ?? []
        guard let job=jobs.first(where:{ ($0["name"] as? String)==name }) else { return }
        let destination=job["destination"] as? String ?? ""
        let source=job["source"] as? String ?? ""

        let alert=NSAlert()
        alert.messageText="Remove “"+name+"” backup?"
        alert.informativeText="This removes the job from Snapshot Backup and deletes its generated backup files at:\n\n"+destination+"\n\nThe original source is NEVER deleted:\n"+source
        alert.alertStyle = .warning
        alert.addButton(withTitle:"Remove Backup")
        alert.addButton(withTitle:"Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        sender.isEnabled=false; sender.title="Removing…"
        runProcessCapture(python,[agentRoot+"/backupctl.py","remove-job",name]) { code,out,err in
            if code==0 {
                self.renderBackups()
                runProcess(python,[agentRoot+"/db_discovery.py","scan"])
            } else {
                sender.isEnabled=true; sender.title="Remove…"
                let a=NSAlert()
                a.messageText="Couldn’t remove backup"
                let detail=err.trimmingCharacters(in:.whitespacesAndNewlines)
                a.informativeText=detail.isEmpty ? "The backup configuration was not changed." : detail
                a.alertStyle = .warning
                a.addButton(withTitle:"OK")
                a.runModal()
            }
        }
    }

    @objc func toggleCovered(){ showCovered.toggle(); renderDiscovery() }
    @objc func openRawConfig(){ NSWorkspace.shared.open(URL(fileURLWithPath:dataRoot+"/config.json")) }
    @objc func revealSource(_ sender:PayloadButton){ NSWorkspace.shared.selectFile(sender.payload,inFileViewerRootedAtPath:"") }
    @objc func runJob(_ sender:PayloadButton){
        let name=sender.payload
        sender.isEnabled=false; sender.title="Running…"
        runProcess(python,[agentRoot+"/backupctl.py","run",name]) { self.renderBackups() }
    }
    @objc func scanDatabases(){
        scanning=true; renderDiscovery()
        runProcess(python,[agentRoot+"/db_discovery.py","scan"]) {
            self.scanning=false; self.renderDiscovery()
        }
    }
    @objc func addSQLite(_ sender:PayloadButton){
        let path=sender.payload
        sender.isEnabled=false; sender.title="Adding…"
        runProcess(python,[agentRoot+"/db_discovery.py","add-sqlite",path]) {
            runProcess(python,[agentRoot+"/db_discovery.py","scan"]) { self.renderDiscovery() }
        }
    }

    @objc func addPostgres(_ sender:PayloadButton){
        let source=sender.payload
        sender.isEnabled=false; sender.title="Adding…"
        runProcess(python,[agentRoot+"/db_discovery.py","add-postgres",source]) {
            runProcess(python,[agentRoot+"/db_discovery.py","scan"]) { self.renderDiscovery() }
        }
    }

    @objc func addDockerPostgres(_ sender:PayloadButton){
        let source=sender.payload
        sender.isEnabled=false; sender.title="Adding…"
        runProcess(python,[agentRoot+"/db_discovery.py","add-docker-postgres",source]) {
            runProcess(python,[agentRoot+"/db_discovery.py","scan"]) { self.renderDiscovery() }
        }
    }

    @objc func ignoreDatabase(_ sender:PayloadButton){
        let path=sender.payload
        let alert=NSAlert()
        alert.messageText="Ignore this item?"
        alert.informativeText="This item will be hidden from future local-data discovery scans:\n\n" + path + "\n\nYou can restore ignored items later by editing state/database_ignored.json."
        alert.alertStyle = .warning
        alert.addButton(withTitle:"Ignore Item")
        alert.addButton(withTitle:"Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        sender.isEnabled=false; sender.title="Ignoring…"
        runProcess(python,[agentRoot+"/db_discovery.py","ignore",path]) {
            runProcess(python,[agentRoot+"/db_discovery.py","scan"]) { self.renderDiscovery() }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var item:NSStatusItem!
    var timer:Timer?
    let menu=NSMenu()
    let manager=ManagerController()

    func applicationDidFinishLaunching(_ note:Notification){
        item=NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        if let b=item.button {
            b.image=NSImage(systemSymbolName:"externaldrive.badge.timemachine",accessibilityDescription:"Snapshot Backup")
            b.image?.isTemplate=true
        }
        item.menu=menu; rebuildMenu()
        timer=Timer.scheduledTimer(withTimeInterval:30,repeats:true){[weak self] _ in self?.rebuildMenu()}
    }

    func rebuildMenu(){
        menu.removeAllItems()
        let status=readJSON(dataRoot+"/state/status.json") ?? [:]
        let running=status["running"] as? Bool ?? false
        let updated=status["updated"] as? String ?? "Never"
        let title=NSMenuItem(title:running ? "Snapshot Backup — Running…" : "Snapshot Backup — Ready",action:nil,keyEquivalent:"")
        title.isEnabled=false; menu.addItem(title)
        if let results=status["last_results"] as? [[String:Any]],let last=results.last {
            let job=last["job"] as? String ?? "backup", result=last["result"] as? String ?? "unknown"
            let row=NSMenuItem(title:"Last: " + job + " — " + result,action:nil,keyEquivalent:""); row.isEnabled=false; menu.addItem(row)
        }
        let timeRow=NSMenuItem(title:"Status updated: " + updated,action:nil,keyEquivalent:""); timeRow.isEnabled=false; menu.addItem(timeRow)
        menu.addItem(.separator())

        let run=NSMenuItem(title:"Back Up Now",action:#selector(runNow),keyEquivalent:""); run.target=self; run.isEnabled = !running; menu.addItem(run)
        let manage=NSMenuItem(title:"Manage Backups…",action:#selector(openManager),keyEquivalent:""); manage.target=self; menu.addItem(manage)
        let discover=NSMenuItem(title:"Scan for Local Data…",action:#selector(scanDatabases),keyEquivalent:""); discover.target=self; menu.addItem(discover)
        menu.addItem(.separator())
        let backups=NSMenuItem(title:"Open Backup Folder",action:#selector(openBackups),keyEquivalent:""); backups.target=self; menu.addItem(backups)
        let logs=NSMenuItem(title:"Open Logs",action:#selector(openLogs),keyEquivalent:""); logs.target=self; menu.addItem(logs)
        menu.addItem(.separator())
        let schedule=NSMenuItem(title:"Automatic backup: Daily at 9:00 PM",action:nil,keyEquivalent:""); schedule.isEnabled=false; menu.addItem(schedule)
        menu.addItem(.separator())
        let quit=NSMenuItem(title:"Quit Status Menu",action:#selector(quitApp),keyEquivalent:"q"); quit.target=self; menu.addItem(quit)
    }

    @objc func runNow(){ runProcess(python,[agentRoot+"/backupctl.py","run-all"]); DispatchQueue.main.asyncAfter(deadline:.now()+0.6){self.rebuildMenu()} }
    @objc func openManager(){ manager.show(tab:0) }
    @objc func scanDatabases(){ manager.show(tab:1); manager.scanDatabases() }
    @objc func openBackups(){ NSWorkspace.shared.open(URL(fileURLWithPath:configuredBackupRoot())) }
    @objc func openLogs(){ NSWorkspace.shared.open(URL(fileURLWithPath:dataRoot+"/logs")) }
    @objc func quitApp(){ NSApp.terminate(nil) }
}

let app=NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate=AppDelegate()
app.delegate=delegate
app.run()

