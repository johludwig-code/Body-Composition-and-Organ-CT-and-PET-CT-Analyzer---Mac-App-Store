import BCOAKit
import SwiftUI

/// About & Licenses and How to Cite (plan §14). The license texts come from
/// THIRD_PARTY_NOTICES.md, which the build generates from the packages that
/// are actually in the bundle.
struct AboutView: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(AppIdentity.displayName).font(.title2.bold())
            Text("Version \(AppIdentity.version)").foregroundStyle(.secondary)
            Text(IntendedUse.statement)
            Divider()
            Text("How to Cite").font(.headline)
            Text("""
                Segmentations are generated with MOOSE (Shiyam Sundar et al., J Nucl Med 2022; \
                Ferrara et al., Sci Data 2026), based on nnU-Net (Isensee et al., Nat Methods \
                2021). MOOSE model weights are licensed under CC BY 4.0 by their authors; the \
                bundled checkpoints were reduced to the network weights needed for inference. \
                The app corrects MOOSE in two places at run time, so its results can differ \
                from those of MOOSE run on its own; the export's methods text names those \
                applied to its results.
                """)
            .textSelection(.enabled)
            Divider()
            Text("Licenses").font(.headline)
            ScrollView {
                Text(notices).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 620, height: 560)
    }

    private var notices: String {
        (try? String(contentsOf: BundleLayout.main.thirdPartyNotices, encoding: .utf8))
            ?? String(localized: "License notices are missing from this build.")
    }
}
