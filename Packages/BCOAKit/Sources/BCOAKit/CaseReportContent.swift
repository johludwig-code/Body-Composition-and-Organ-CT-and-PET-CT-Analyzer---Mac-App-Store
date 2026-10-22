import Foundation

/// One case's content file, `<case>.report.json`, as the worker's export
/// writes it (ADR 0017 section 2). The numbers, the QC decisions and the
/// notes are the export's; the app only draws them. Decoded by hand and
/// leniently, so that a key added later does not make older files unreadable;
/// what the report cannot do without makes it refuse instead.
public struct CaseReportContent: Sendable, Equatable {
    public struct Structure: Sendable, Equatable {
        public var labelID: Int
        public var label: String
        public var voxelCount: Int?
        public var volumeML: Double?
        public var huMean: Double?
        public var huSD: Double?
        public var huMedian: Double?
        public var flags: [String]
        public var excluded: Bool
        public var note: String?

        public init(labelID: Int, label: String, voxelCount: Int? = nil, volumeML: Double? = nil,
                    huMean: Double? = nil, huSD: Double? = nil, huMedian: Double? = nil,
                    flags: [String] = [], excluded: Bool = false, note: String? = nil) {
            self.labelID = labelID
            self.label = label
            self.voxelCount = voxelCount
            self.volumeML = volumeML
            self.huMean = huMean
            self.huSD = huSD
            self.huMedian = huMedian
            self.flags = flags
            self.excluded = excluded
            self.note = note
        }

        /// Not in this CT, as opposed to left out by QC or not measured.
        public var notFound: Bool { !excluded && (voxelCount == 0 || flags.contains("empty_label")) }
    }

    public struct Model: Sendable, Equatable {
        public var name: String
        public var key: String
        /// "ok", "failed", "not_run" or "excluded".
        public var status: String
        public var structures: [Structure]

        public init(name: String, key: String, status: String, structures: [Structure]) {
            self.name = name
            self.key = key
            self.status = status
            self.structures = structures
        }
    }

    public struct SeriesInfo: Sendable, Equatable {
        public var description: String?
        public var modality: String?
        public var sliceThicknessMM: Double?
        public var pixelSpacingMM: Double?
        public var kernel: String?
        public var kvp: Double?
        public var manufacturer: String?
        public var contrastAgent: String?
        public var imageCount: Int?

        public init() {}
    }

    public var caseName: String
    public var pseudonym: String
    public var timepoint: Int
    public var seriesIndex: Int
    /// "days_since_first", "year" or "iso", as the export chose.
    public var dateKind: String
    public var dateValue: String?
    public var sex: String?
    public var ageYears: Int?
    public var seriesKey: String
    public var series: SeriesInfo
    public var device: String?
    public var qcStatus: String
    public var reviewed: Bool
    public var models: [Model]
    public var runID: String
    public var versions: [String: String]
    public var modelChecksums: [String: String]
    public var methodsText: String

    public enum DecodeError: Error, Equatable {
        case notJSON, missing(String), format(Int)
    }

    /// The only format there is; a newer one is refused rather than drawn
    /// half-understood.
    public static let format = 1

    /// The worker's names, and the name the PDF gets (ADR 0017 section 8).
    public static let casePattern = #"^P\d{4,}_t\d+_s\d+$"#

    public init(json data: Data) throws {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw DecodeError.notJSON
        }
        let format = object["format"] as? Int ?? 0
        guard format == Self.format else { throw DecodeError.format(format) }
        func string(_ key: String) throws -> String {
            guard let value = object[key] as? String, !value.isEmpty else { throw DecodeError.missing(key) }
            return value
        }
        caseName = try string("case")
        guard caseName.range(of: Self.casePattern, options: .regularExpression) != nil else {
            throw DecodeError.missing("case")
        }
        pseudonym = try string("pseudonym")
        seriesKey = try string("series_key")
        runID = try string("run_id")
        timepoint = object["timepoint"] as? Int ?? 1
        seriesIndex = object["series_index"] as? Int ?? 1
        let date = object["date"] as? [String: Any] ?? [:]
        dateKind = date["kind"] as? String ?? "days_since_first"
        dateValue = Self.text(date["value"])
        sex = object["sex"] as? String
        ageYears = object["age_years"] as? Int
        device = object["device"] as? String
        let qc = object["qc"] as? [String: Any] ?? [:]
        qcStatus = qc["status"] as? String ?? "unreviewed"
        reviewed = qc["reviewed"] as? Bool ?? false
        let info = object["series"] as? [String: Any] ?? [:]
        var series = SeriesInfo()
        series.description = info["description"] as? String
        series.modality = info["modality"] as? String
        series.sliceThicknessMM = Self.number(info["slice_thickness_mm"])
        series.pixelSpacingMM = Self.number(info["pixel_spacing_mm"])
        series.kernel = info["kernel"] as? String
        series.kvp = Self.number(info["kvp"])
        series.manufacturer = info["manufacturer"] as? String
        series.contrastAgent = info["contrast_agent"] as? String
        series.imageCount = info["image_count"] as? Int
        self.series = series
        models = (object["models"] as? [[String: Any]] ?? []).compactMap { model in
            guard let name = model["name"] as? String else { return nil }
            let structures = (model["structures"] as? [[String: Any]] ?? []).compactMap { s -> Structure? in
                guard let id = s["label_id"] as? Int, let label = s["label"] as? String else { return nil }
                return Structure(
                    labelID: id, label: label, voxelCount: s["voxel_count"] as? Int,
                    volumeML: Self.number(s["volume_ml"]), huMean: Self.number(s["hu_mean"]),
                    huSD: Self.number(s["hu_sd"]), huMedian: Self.number(s["hu_median"]),
                    flags: s["flags"] as? [String] ?? [], excluded: s["excluded"] as? Bool ?? false,
                    note: s["note"] as? String)
            }
            return Model(name: name, key: model["key"] as? String ?? name,
                         status: model["status"] as? String ?? "not_run", structures: structures)
        }
        versions = (object["versions"] as? [String: Any] ?? [:]).compactMapValues { Self.text($0) }
        modelChecksums = (object["model_checksums"] as? [String: Any] ?? [:]).compactMapValues { $0 as? String }
        methodsText = object["methods_text"] as? String ?? ""
    }

    private static func number(_ value: Any?) -> Double? {
        // `is Bool` holds for every NSNumber of 0 or 1, so a slice thickness
        // of 1.0 would read as a flag; the CF type tells the two apart.
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let double = number.doubleValue
        return double.isFinite ? double : nil
    }

    private static func text(_ value: Any?) -> String? {
        switch value {
        case let string as String: string
        case let number as NSNumber: number.stringValue
        default: nil
        }
    }

    /// The PDF's file name, beside its content file.
    public var pdfName: String { "\(caseName)_case_report.pdf" }
}
