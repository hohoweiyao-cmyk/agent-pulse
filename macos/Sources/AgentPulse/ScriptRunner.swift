import Darwin
import Foundation

/// Run the local collector off the main thread with separate, private output streams.
enum ScriptRunner {
    static func run(pythonPath: String, scriptURL: URL, args: [String], timeout: Double) -> Result<Data, Error> {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appendingPathComponent("AgentPulse-\(UUID())", isDirectory: true)
        do {
            try files.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            defer { try? files.removeItem(at: directory) }
            let out = directory.appendingPathComponent("stdout")
            let err = directory.appendingPathComponent("stderr")
            for url in [out, err] {
                guard files.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
                    return .failure(scriptError("无法写入采集暂存文件，请检查磁盘空间。"))
                }
            }

            let stdout = try FileHandle(forWritingTo: out)
            let stderr = try FileHandle(forWritingTo: err)
            defer {
                try? stdout.close()
                try? stderr.close()
            }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonPath)
            process.arguments = ["-I", "-B", scriptURL.path] + args
            process.standardOutput = stdout
            process.standardError = stderr
            let ended = DispatchSemaphore(value: 0)
            process.terminationHandler = { _ in ended.signal() }
            try process.run()
            guard ended.wait(timeout: .now() + timeout) == .success else {
                if process.isRunning {
                    process.terminate()
                    if ended.wait(timeout: .now() + 2) == .timedOut, process.isRunning {
                        kill(process.processIdentifier, SIGKILL)
                    }
                }
                return .failure(scriptError("后台处理超时，请稍后刷新。"))
            }

            if process.terminationStatus == 0 {
                return .success(try Data(contentsOf: out))
            }
            let reader = try FileHandle(forReadingFrom: err)
            defer { try? reader.close() }
            let errorData = try reader.read(upToCount: 2_048) ?? Data()
            let firstLine = String(decoding: errorData, as: UTF8.self).split(whereSeparator: \.isNewline).first
            let detail = firstLine.flatMap { line in
                line.first?.isASCII == false ? String(line) : nil
            }
            return .failure(scriptError(detail ?? "后台处理失败（代码 \(process.terminationStatus)），请检查本地数据。"))
        } catch {
            return .failure(scriptError("无法启动或读取后台采集器，请检查 Python 3 和本地数据权限。"))
        }
    }

    private static func scriptError(_ message: String) -> NSError {
        NSError(domain: "AgentPulse", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
