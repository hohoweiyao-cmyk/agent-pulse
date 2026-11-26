import Foundation
import XCTest
@testable import AgentPulse

final class ScriptRunnerTests: XCTestCase {
    func testLargeStderrDoesNotBlockSuccessfulCollector() throws {
        try withDirectory { directory in
            let script = directory.appendingPathComponent("collector.py")
            try "import sys\nsys.stderr.write('x' * 200000)\nprint('ok')\n"
                .write(to: script, atomically: true, encoding: .utf8)
            let result = ScriptRunner.run(pythonPath: "/usr/bin/python3", scriptURL: script, args: [], timeout: 5)
            XCTAssertEqual(String(decoding: try result.get(), as: UTF8.self), "ok\n")
        }
    }

    func testHungCollectorTimesOut() throws {
        try withDirectory { directory in
            let script = directory.appendingPathComponent("collector.py")
            try "import time\ntime.sleep(20)\n".write(to: script, atomically: true, encoding: .utf8)
            let before = Date()
            let result = ScriptRunner.run(pythonPath: "/usr/bin/python3", scriptURL: script, args: [], timeout: 0.2)
            XCTAssertThrowsError(try result.get()) { error in
                XCTAssertTrue(error.localizedDescription.contains("超时"))
            }
            XCTAssertLessThan(Date().timeIntervalSince(before), 5)
        }
    }

    func testProjectActionRejectsFilesAndMissingPaths() throws {
        try withDirectory { directory in
            let file = directory.appendingPathComponent("file.txt")
            try Data().write(to: file)
            let alias = directory.appendingPathComponent("alias")
            try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: file)
            XCTAssertNotNil(ClockStore.validatedProjectDirectory(directory.path))
            XCTAssertNil(ClockStore.validatedProjectDirectory(file.path))
            XCTAssertNil(ClockStore.validatedProjectDirectory(alias.path))
            XCTAssertNil(ClockStore.validatedProjectDirectory("relative"))
            XCTAssertNil(ClockStore.validatedProjectDirectory(file.path + "-missing"))
        }
    }

    private func withDirectory(_ body: (URL) throws -> Void) throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        try body(directory)
    }
}
