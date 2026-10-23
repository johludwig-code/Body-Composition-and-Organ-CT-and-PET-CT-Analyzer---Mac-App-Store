import AppKit
import BCOAKit
import SwiftUI

/// About & Licenses and How to Cite (plan §14). The license texts come from
/// THIRD_PARTY_NOTICES.md, which the build generates from the packages that
/// are actually in the bundle. The intended use is information, never a
/// screen to accept (App Review 2.4.5(vi)).
struct AboutView: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 64, height: 64)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(AppIdentity.displayName).font(.title2.bold())
                    Text("Version \(AppIdentity.version)").foregroundStyle(.secondary)
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    section("Intended Use") {
                        Text(IntendedUse.statement)
                        Text(IntendedUse.everyPage).fontWeight(.semibold)
                        Text(IntendedUse.regulatory)
                        Text(IntendedUse.responsibility)
                    }
                    section("How to Cite") {
                        Text("""
                            Segmentations are generated with MOOSE (Shiyam Sundar et al., J Nucl Med 2022; \
                            Ferrara et al., Sci Data 2026), based on nnU-Net (Isensee et al., Nat Methods \
                            2021). The app corrects MOOSE in two places at run time, so its results can \
                            differ from those of MOOSE run on its own; the export's methods text names \
                            those applied to its results.
                            """)
                    }
                    section("Rights") {
                        Text("""
                            MOOSE is the work of its authors (ENHANCE.PET) and licensed under the Apache \
                            License 2.0; this app is based on MOOSE and is neither made nor endorsed by \
                            them. The MOOSE model weights are licensed under CC BY 4.0 by their authors \
                            (https://creativecommons.org/licenses/by/4.0/); the bundled checkpoints were \
                            reduced to the network weights needed for inference, which are unchanged. \
                            Nothing in this app's license restricts the rights that license grants.
                            """)
                        Text("""
                            The case report embeds DejaVu Sans (Bitstream Vera and Arev font licences). \
                            Every component's licence text is listed below.
                            """)
                        Text("The app works offline: it has no network access, and no data leaves this Mac.")
                        Text(IntendedUse.warranty)
                    }
                }
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 280)
            Text("Licenses").font(.headline)
            // 1.4 MB of licence texts: an NSTextView lays out only what is
            // shown, where a SwiftUI Text measured all of it at once.
            NoticesView(text: notices)
            HStack {
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 680, height: 720)
    }

    private func section<Content: View>(_ title: LocalizedStringKey, @ViewBuilder content: () -> Content)
        -> some View
    {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.headline)
            content()
        }
    }

    private var notices: String {
        (try? String(contentsOf: BundleLayout.main.thirdPartyNotices, encoding: .utf8))
            ?? String(localized: "License notices are missing from this build.")
    }
}

private struct NoticesView: NSViewRepresentable {
    let text: String

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        if let view = scroll.documentView as? NSTextView {
            view.isEditable = false
            view.isSelectable = true
            view.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            view.string = text
        }
        return scroll
    }

    func updateNSView(_ view: NSScrollView, context: Context) {}
}
