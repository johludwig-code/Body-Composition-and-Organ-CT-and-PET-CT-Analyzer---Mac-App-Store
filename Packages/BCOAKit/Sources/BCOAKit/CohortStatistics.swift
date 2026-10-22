import Foundation

/// The `volume_outlier` rule of plan §12: a robust z-score from the median and
/// the median absolute deviation, so that one wrong segmentation does not move
/// the bar it is measured against, as it would a mean and SD.
public enum CohortStatistics {
    /// The plan's defaults: flag above 3.5, and only from 20 series on,
    /// because a median of a handful says little about what is unusual.
    public static let threshold = 3.5
    public static let minimumSeries = 20

    /// 0.6745 makes the MAD comparable with an SD for normal data (Iglewicz
    /// and Hoaglin's modified z-score).
    public static func robustZ(_ values: [Double]) -> [Double?] {
        guard values.count >= minimumSeries else { return values.map { _ in nil } }
        let center = median(values)
        let mad = median(values.map { abs($0 - center) })
        guard mad > 0 else { return values.map { _ in nil } }
        return values.map { 0.6745 * ($0 - center) / mad }
    }

    public static func outliers(_ values: [Double]) -> [Bool] {
        robustZ(values).map { ($0.map(abs) ?? 0) > threshold }
    }

    public static func median(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return .nan }
        let sorted = values.sorted()
        let middle = sorted.count / 2
        return sorted.count % 2 == 1 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2
    }
}
