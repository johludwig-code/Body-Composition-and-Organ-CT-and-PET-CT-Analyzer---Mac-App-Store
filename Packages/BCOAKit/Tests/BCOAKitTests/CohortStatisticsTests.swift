import Foundation
import Testing
@testable import BCOAKit

@Test func fewerThanTwentySeriesAreNeverOutliers() {
    #expect(CohortStatistics.outliers([1, 2, 3, 1000]) == [false, false, false, false])
}

@Test func oneWrongSegmentationStandsOut() {
    // 19 livers between 1 400 and 1 760 mL, and one of 120 mL: a fragment.
    let livers = (0..<19).map { 1400.0 + Double($0) * 20 } + [120]
    let flags = CohortStatistics.outliers(livers)
    #expect(flags.last == true)
    #expect(flags.dropLast().allSatisfy { !$0 })
    // Median 1 570, MAD 100: z = 0.6745 × (120 − 1 570) / 100 = −9.78025.
    let z = try? #require(CohortStatistics.robustZ(livers).last ?? nil)
    #expect(abs((z ?? 0) + 9.78025) < 1e-9)
}

@Test func identicalValuesHaveNoZ() {
    #expect(CohortStatistics.robustZ(Array(repeating: 5.0, count: 25)).allSatisfy { $0 == nil })
}

@Test func theMedianOfAnEvenCountIsTheMeanOfTheMiddleTwo() {
    #expect(CohortStatistics.median([4, 1, 3, 2]) == 2.5)
    #expect(CohortStatistics.median([3, 1, 2]) == 2)
}
