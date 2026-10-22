import BCOAStore
import Foundation

/// Turns the import's codes and their parameters into the US-English text of
/// the String Catalog (ADR 0023). The database holds no prose; a code without
/// a text is shown as itself, which a test of the registry keeps from
/// happening for every code that reaches the interface.
enum CodeText {
    static func text(group: String, code: String, params: [String: Any] = [:]) -> String {
        guard let template = IndexCodeTexts.text(group: group, code: code) else { return code }
        return fill(template, params: params)
    }

    /// `{"v": 1, "outcome": …, "codes": […], "params": {…}}` as one or more
    /// sentences.
    static func reason(_ json: String?) -> String {
        guard let object = parse(json) else { return "" }
        let codes = object["codes"] as? [String] ?? []
        let params = object["params"] as? [String: Any] ?? [:]
        return codes.map { text(group: "select", code: $0, params: params) }.joined(separator: " ")
    }

    static func outcome(_ json: String?) -> String? {
        parse(json)?["outcome"] as? String
    }

    static func check(_ check: ProjectTree.Check) -> String {
        text(group: "check", code: check.code, params: parse(check.paramsJSON) ?? [:])
    }

    /// The status line of a series (registry group `status`).
    static func status(of series: ProjectTree.Series, patientStatus: String) -> String {
        if series.gone { return text(group: "status", code: "status.gone") }
        if patientStatus == "unconfirmed" { return text(group: "status", code: "status.held") }
        switch (series.origin, series.selected) {
        case ("user", true): return text(group: "status", code: "status.user.selected")
        case ("user", false): return text(group: "status", code: "status.user.deselected")
        case ("bulk_thin_ct", true): return text(group: "status", code: "status.bulk.selected")
        case ("cohort", let selected):
            return text(group: "status", code: selected ? "status.cohort.selected" : "status.cohort.not_selected",
                        params: ["label": series.cohortLabel ?? ""])
        case (_, true): return text(group: "status", code: "status.auto.selected")
        default: return text(group: "status", code: "status.auto.not_selected")
        }
    }

    static func sourceState(_ source: SourceRow) -> String {
        var params: [String: Any] = [:]
        if let at = source.indexedAt, let date = ISO8601DateFormatter().date(from: at) {
            params["date"] = date.formatted(date: .abbreviated, time: .shortened)
        }
        return text(group: "source_state", code: source.state, params: params)
    }

    // MARK: -

    private static func parse(_ json: String?) -> [String: Any]? {
        guard let data = json?.data(using: .utf8) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private static func fill(_ template: String, params: [String: Any]) -> String {
        var text = template
        for (name, value) in params {
            text = text.replacingOccurrences(of: "{\(name)}", with: format(name: name, value: value))
        }
        return text
    }

    /// Numbers follow the system region in the interface (plan §2 rule 10);
    /// parameters that are themselves codes get their own text.
    private static func format(name: String, value: Any) -> String {
        switch value {
        case let number as NSNumber where CFGetTypeID(number) == CFBooleanGetTypeID():
            return number.boolValue ? String(localized: "yes") : String(localized: "no")
        case let number as NSNumber:
            return number.doubleValue.formatted(.number.precision(.fractionLength(0...2)))
        case let string as String:
            switch name {
            case "orientation":
                return IndexCodeTexts.text(group: "orientation", code: "orientation.\(string)") ?? string
            case "syntax":
                return IndexCodeTexts.text(group: "transfer_syntax", code: string) ?? string
            case "reason":
                return IndexCodeTexts.text(group: "split_reason", code: string) ?? string
            default:
                return string
            }
        case let list as [Any]:
            return list.map { format(name: name, value: $0) }.joined(separator: ", ")
        default:
            return "\(value)"
        }
    }
}
