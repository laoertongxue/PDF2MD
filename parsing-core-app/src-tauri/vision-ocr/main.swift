import AppKit
import CryptoKit
import Darwin
import Foundation
import PDFKit
import Vision

private struct OCRRequest: Decodable {
    let command: String
    let pdfPath: String?
    let page: Int?
    let dpi: Double?
    let languages: [String]?
    let outputDir: String?

    enum CodingKeys: String, CodingKey {
        case command
        case pdfPath = "pdf_path"
        case page
        case dpi
        case languages
        case outputDir = "output_dir"
    }
}

private struct BoundingBox: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

private struct Candidate: Codable {
    let text: String
    let confidence: Float
}

private struct Observation: Codable {
    let text: String
    let confidence: Float
    let boundingBox: BoundingBox
    let candidates: [Candidate]

    enum CodingKeys: String, CodingKey {
        case text
        case confidence
        case boundingBox = "bounding_box"
        case candidates
    }
}

private struct OCRResponse: Codable {
    let page: Int
    let imagePath: String
    let imageSHA256: String
    let width: Int
    let height: Int
    let supportedLanguages: [String]
    let observations: [Observation]

    enum CodingKeys: String, CodingKey {
        case page
        case imagePath = "image_path"
        case imageSHA256 = "image_sha256"
        case width
        case height
        case supportedLanguages = "supported_languages"
        case observations
    }
}

private struct ErrorBody: Codable {
    let code: String
    let message: String
}

private struct ErrorResponse: Codable {
    let error: ErrorBody
}

private struct ProtocolError: Error {
    let code: String
    let message: String
}

private final class ControlledOutputDirectory {
    let url: URL
    let descriptor: Int32

    init(url: URL, descriptor: Int32) {
        self.url = url
        self.descriptor = descriptor
    }

    deinit {
        Darwin.close(descriptor)
    }
}

private let maximumDPI = 600.0
private let maximumPixelDimension = 20_000
private let maximumPixelCount = 80_000_000
private let maximumImageBytes = 320_000_000

private let encoder: JSONEncoder = {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return encoder
}()

private func fail(_ code: String, _ message: String) -> ProtocolError {
    ProtocolError(code: code, message: message)
}

private func require<T>(_ value: T?, field: String) throws -> T {
    guard let value else {
        throw fail("invalid_request", "missing required field: \(field)")
    }
    return value
}

private func render(page: PDFPage, dpi: Double) throws -> (Data, CGImage, Int, Int) {
    guard dpi.isFinite, dpi > 0, dpi <= maximumDPI else {
        throw fail("invalid_dpi", "dpi must be finite, greater than 0, and no more than 600")
    }

    guard let pdfPage = page.pageRef else {
        throw fail("render_failed", "could not access the PDF page")
    }
    let cropBox = pdfPage.getBoxRect(.cropBox)
    let rotation = ((pdfPage.rotationAngle % 360) + 360) % 360
    guard [0, 90, 180, 270].contains(rotation),
          cropBox.width.isFinite,
          cropBox.height.isFinite,
          cropBox.width > 0,
          cropBox.height > 0 else {
        throw fail("invalid_page_geometry", "PDF crop box or rotation is invalid")
    }

    let pointWidth = rotation == 90 || rotation == 270 ? cropBox.height : cropBox.width
    let pointHeight = rotation == 90 || rotation == 270 ? cropBox.width : cropBox.height

    let scale = CGFloat(dpi / 72.0)
    let scaledWidth = ceil(pointWidth * scale)
    let scaledHeight = ceil(pointHeight * scale)
    guard scaledWidth.isFinite,
          scaledHeight.isFinite,
          scaledWidth >= 1,
          scaledHeight >= 1,
          scaledWidth <= CGFloat(maximumPixelDimension),
          scaledHeight <= CGFloat(maximumPixelDimension) else {
        throw fail("resource_limit", "rendered image dimensions exceed the safe limit")
    }

    let width = Int(scaledWidth)
    let height = Int(scaledHeight)
    let (pixelCount, pixelOverflow) = width.multipliedReportingOverflow(by: height)
    let (bytesPerRow, rowOverflow) = width.multipliedReportingOverflow(by: 4)
    let (totalBytes, byteOverflow) = bytesPerRow.multipliedReportingOverflow(by: height)
    guard !pixelOverflow,
          !rowOverflow,
          !byteOverflow,
          pixelCount <= maximumPixelCount,
          totalBytes <= maximumImageBytes else {
        throw fail("resource_limit", "rendered image exceeds the safe memory budget")
    }

    let colorSpace = CGColorSpaceCreateDeviceRGB()
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: bytesPerRow,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        throw fail("render_failed", "could not allocate the page image")
    }

    context.setFillColor(NSColor.white.cgColor)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    let targetRect = CGRect(x: 0, y: 0, width: width, height: height)
    context.saveGState()
    context.concatenate(pdfPage.getDrawingTransform(
        .cropBox,
        rect: targetRect,
        rotate: 0,
        preserveAspectRatio: true
    ))
    context.drawPDFPage(pdfPage)
    context.restoreGState()

    guard let image = context.makeImage() else {
        throw fail("render_failed", "could not create the page image")
    }
    let representation = NSBitmapImageRep(cgImage: image)
    guard let png = representation.representation(using: .png, properties: [:]) else {
        throw fail("render_failed", "could not encode the page image")
    }
    return (png, image, width, height)
}

private func normalizedTopLeftBox(_ box: CGRect) -> BoundingBox {
    func clamp(_ value: CGFloat) -> Double {
        Double(min(1, max(0, value)))
    }

    let x = clamp(box.minX)
    let y = clamp(1 - box.maxY)
    let maxX = clamp(box.maxX)
    let maxY = clamp(1 - box.minY)
    return BoundingBox(x: x, y: y, width: max(0, maxX - x), height: max(0, maxY - y))
}

private func recognize(image: CGImage, languages: [String]) throws -> ([String], [Observation]) {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let supported = try request.supportedRecognitionLanguages().sorted()
    let requested = languages.isEmpty ? ["zh-Hans", "en-US"] : languages
    guard !requested.contains("zh-Hans") || supported.contains("zh-Hans") else {
        throw fail("zh_hans_unsupported", "Apple Vision accurate recognition does not support zh-Hans on this system")
    }

    let unsupported = requested.filter { !supported.contains($0) }
    guard unsupported.isEmpty else {
        throw fail("unsupported_language", "one or more recognition languages are unsupported")
    }
    request.recognitionLanguages = requested

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        throw fail("recognition_failed", "Apple Vision text recognition failed")
    }

    let observations = (request.results ?? []).compactMap { result -> Observation? in
        let candidates = result.topCandidates(3).map {
            Candidate(text: $0.string, confidence: $0.confidence)
        }
        guard let best = candidates.first else { return nil }
        return Observation(
            text: best.text,
            confidence: best.confidence,
            boundingBox: normalizedTopLeftBox(result.boundingBox),
            candidates: candidates
        )
    }
    return (supported, observations)
}

private func fileType(_ mode: mode_t) -> mode_t {
    mode & mode_t(S_IFMT)
}

private func controlledOutputDirectory(for request: OCRRequest) throws -> ControlledOutputDirectory {
    guard let requestedPath = request.outputDir, !requestedPath.isEmpty else {
        throw fail("missing_output_dir", "output_dir is required")
    }
    guard !(requestedPath as NSString).isAbsolutePath else {
        throw fail("invalid_output_dir", "output_dir must be a relative task directory")
    }

    let components = (requestedPath as NSString).pathComponents
    guard !components.isEmpty,
          components.allSatisfy({ component in
              !component.isEmpty && component != "." && component != ".." && component != "/" && !component.utf8.contains(0)
          }) else {
        throw fail("invalid_output_dir", "output_dir contains an invalid path component")
    }

    guard let rootPath = ProcessInfo.processInfo.environment["PDF2MD_VISION_OUTPUT_ROOT"],
          !rootPath.isEmpty,
          (rootPath as NSString).isAbsolutePath else {
        throw fail("output_root_unavailable", "controlled output root is unavailable")
    }

    var rootInfo = stat()
    guard rootPath.withCString({ lstat($0, &rootInfo) }) == 0,
          fileType(rootInfo.st_mode) == mode_t(S_IFDIR) else {
        throw fail("output_root_unavailable", "controlled output root is not a real directory")
    }

    guard let resolvedPointer = rootPath.withCString({ realpath($0, nil) }) else {
        throw fail("output_root_unavailable", "controlled output root cannot be resolved")
    }
    defer { free(resolvedPointer) }
    let resolvedRoot = String(cString: resolvedPointer)

    var directoryDescriptor = resolvedRoot.withCString {
        Darwin.open($0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
    }
    guard directoryDescriptor >= 0 else {
        throw fail("output_root_unavailable", "controlled output root cannot be opened")
    }

    var directoryURL = URL(fileURLWithPath: resolvedRoot, isDirectory: true)
    for component in components {
        var info = stat()
        let status = component.withCString {
            fstatat(directoryDescriptor, $0, &info, AT_SYMLINK_NOFOLLOW)
        }
        if status == 0 {
            guard fileType(info.st_mode) == mode_t(S_IFDIR) else {
                Darwin.close(directoryDescriptor)
                throw fail("invalid_output_dir", "output_dir must contain only real directories")
            }
        } else if errno == ENOENT {
            let mkdirStatus = component.withCString {
                mkdirat(directoryDescriptor, $0, S_IRWXU)
            }
            guard mkdirStatus == 0 || errno == EEXIST else {
                Darwin.close(directoryDescriptor)
                throw fail("invalid_output_dir", "output_dir could not be created safely")
            }
        } else {
            Darwin.close(directoryDescriptor)
            throw fail("invalid_output_dir", "output_dir could not be inspected safely")
        }

        let nextDescriptor = component.withCString {
            openat(directoryDescriptor, $0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        }
        guard nextDescriptor >= 0 else {
            Darwin.close(directoryDescriptor)
            throw fail("invalid_output_dir", "output_dir contains a symlink or non-directory")
        }
        Darwin.close(directoryDescriptor)
        directoryDescriptor = nextDescriptor
        directoryURL.appendPathComponent(component, isDirectory: true)
    }

    return ControlledOutputDirectory(url: directoryURL, descriptor: directoryDescriptor)
}

private func writeAll(_ data: Data, descriptor: Int32) throws {
    try data.withUnsafeBytes { rawBuffer in
        guard var pointer = rawBuffer.baseAddress else { return }
        var remaining = rawBuffer.count
        while remaining > 0 {
            let written = Darwin.write(descriptor, pointer, remaining)
            guard written > 0 else {
                throw fail("output_failed", "could not write image data")
            }
            pointer = pointer.advanced(by: written)
            remaining -= written
        }
    }
}

#if VISION_OCR_TESTING
private func waitForDirectorySwapTestHook() throws {
    let environment = ProcessInfo.processInfo.environment
    guard let readyPath = environment["PDF2MD_VISION_TEST_SWAP_READY"],
          let continuePath = environment["PDF2MD_VISION_TEST_SWAP_CONTINUE"],
          !readyPath.isEmpty,
          !continuePath.isEmpty,
          !FileManager.default.fileExists(atPath: readyPath) else {
        return
    }
    guard FileManager.default.createFile(atPath: readyPath, contents: Data()) else {
        throw fail("output_failed", "test synchronization failed")
    }
    let deadline = Date().addingTimeInterval(10)
    while !FileManager.default.fileExists(atPath: continuePath) {
        guard Date() < deadline else {
            throw fail("output_failed", "test synchronization timed out")
        }
        usleep(10_000)
    }
}
#else
private func waitForDirectorySwapTestHook() throws {}
#endif

private func sameIdentity(_ left: stat, _ right: stat) -> Bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

private func publishedPathMatchesDescriptors(
    directory: ControlledOutputDirectory,
    fileDescriptor: Int32,
    finalName: String,
    imageURL: URL
) -> Bool {
    var descriptorDirectoryInfo = stat()
    var pathDirectoryInfo = stat()
    guard fstat(directory.descriptor, &descriptorDirectoryInfo) == 0,
          directory.url.path.withCString({ fstatat(AT_FDCWD, $0, &pathDirectoryInfo, 0) }) == 0,
          fileType(descriptorDirectoryInfo.st_mode) == mode_t(S_IFDIR),
          fileType(pathDirectoryInfo.st_mode) == mode_t(S_IFDIR),
          sameIdentity(descriptorDirectoryInfo, pathDirectoryInfo) else {
        return false
    }

    var openFileInfo = stat()
    var descriptorFileInfo = stat()
    var pathFileInfo = stat()
    guard fstat(fileDescriptor, &openFileInfo) == 0,
          finalName.withCString({
              fstatat(directory.descriptor, $0, &descriptorFileInfo, AT_SYMLINK_NOFOLLOW)
          }) == 0,
          imageURL.path.withCString({ fstatat(AT_FDCWD, $0, &pathFileInfo, 0) }) == 0,
          fileType(openFileInfo.st_mode) == mode_t(S_IFREG),
          fileType(descriptorFileInfo.st_mode) == mode_t(S_IFREG),
          fileType(pathFileInfo.st_mode) == mode_t(S_IFREG),
          sameIdentity(openFileInfo, descriptorFileInfo),
          sameIdentity(descriptorFileInfo, pathFileInfo) else {
        return false
    }
    return true
}

private func removePublishedFile(_ name: String, directory: ControlledOutputDirectory) {
    name.withCString { _ = unlinkat(directory.descriptor, $0, 0) }
    _ = fsync(directory.descriptor)
}

private func publishImage(_ data: Data, page: Int, directory: ControlledOutputDirectory) throws -> URL {
    try waitForDirectorySwapTestHook()
    let token = UUID().uuidString
    let temporaryName = ".vision-ocr-\(token).tmp"
    let finalName = "page-\(page)-\(token).png"
    let descriptor = temporaryName.withCString {
        openat(
            directory.descriptor,
            $0,
            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
            S_IRUSR | S_IWUSR
        )
    }
    guard descriptor >= 0 else {
        throw fail("output_failed", "could not create a new image file")
    }

    var renamed = false
    defer {
        Darwin.close(descriptor)
        if !renamed {
            temporaryName.withCString { _ = unlinkat(directory.descriptor, $0, 0) }
        }
    }

    try writeAll(data, descriptor: descriptor)
    guard fsync(descriptor) == 0 else {
        throw fail("output_failed", "could not synchronize image data")
    }
    let renameStatus = temporaryName.withCString { temporaryPointer in
        finalName.withCString { finalPointer in
            renameatx_np(
                directory.descriptor,
                temporaryPointer,
                directory.descriptor,
                finalPointer,
                UInt32(RENAME_EXCL)
            )
        }
    }
    guard renameStatus == 0 else {
        throw fail("output_failed", "could not publish a new image file atomically")
    }
    renamed = true

    guard fsync(directory.descriptor) == 0 else {
        removePublishedFile(finalName, directory: directory)
        throw fail("output_failed", "could not synchronize output directory")
    }

    let imageURL = directory.url.appendingPathComponent(finalName)
    guard publishedPathMatchesDescriptors(
        directory: directory,
        fileDescriptor: descriptor,
        finalName: finalName,
        imageURL: imageURL
    ) else {
        removePublishedFile(finalName, directory: directory)
        throw fail("output_failed", "published image path changed during output")
    }
    return imageURL
}

private func process(_ request: OCRRequest) throws -> OCRResponse {
    guard request.command == "render_and_recognize" else {
        throw fail("unsupported_command", "unsupported command")
    }

    let pdfPath = try require(request.pdfPath, field: "pdf_path")
    let pageNumber = try require(request.page, field: "page")
    let dpi = try require(request.dpi, field: "dpi")
    let languages = request.languages ?? []
    let directory = try controlledOutputDirectory(for: request)

    guard let document = PDFDocument(url: URL(fileURLWithPath: pdfPath)) else {
        throw fail("pdf_open_failed", "could not open PDF for reading")
    }
    if document.isLocked {
        throw fail("pdf_locked", "PDF is locked")
    }
    if document.isEncrypted {
        throw fail("pdf_encrypted", "PDF is encrypted")
    }
    guard pageNumber >= 1, pageNumber <= document.pageCount,
          let page = document.page(at: pageNumber - 1) else {
        throw fail("page_out_of_range", "page must be between 1 and \(document.pageCount)")
    }

    let (png, image, width, height) = try render(page: page, dpi: dpi)
    let (supportedLanguages, observations) = try recognize(image: image, languages: languages)
    let imageURL = try publishImage(png, page: pageNumber, directory: directory)

    return OCRResponse(
        page: pageNumber,
        imagePath: imageURL.path,
        imageSHA256: SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined(),
        width: width,
        height: height,
        supportedLanguages: supportedLanguages,
        observations: observations
    )
}

private func emit(_ data: Data) {
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

while let line = readLine(strippingNewline: true) {
    autoreleasepool {
        do {
            guard let data = line.data(using: .utf8) else {
                throw fail("invalid_json", "request is not valid UTF-8")
            }
            let request: OCRRequest
            do {
                request = try JSONDecoder().decode(OCRRequest.self, from: data)
            } catch {
                throw fail("invalid_json", "request must be a valid JSON object")
            }
            let response = try process(request)
            let responseData = try encoder.encode(response)
            emit(responseData)
        } catch let error as ProtocolError {
            let response = ErrorResponse(error: ErrorBody(code: error.code, message: error.message))
            emit((try? encoder.encode(response)) ?? Data("{\"error\":{\"code\":\"internal_error\",\"message\":\"encoding failed\"}}".utf8))
            FileHandle.standardError.write(Data("vision-ocr request failed: \(error.code)\n".utf8))
        } catch {
            let response = ErrorResponse(error: ErrorBody(code: "internal_error", message: "unexpected helper failure"))
            emit((try? encoder.encode(response)) ?? Data("{\"error\":{\"code\":\"internal_error\",\"message\":\"encoding failed\"}}".utf8))
            FileHandle.standardError.write(Data("vision-ocr request failed: internal_error\n".utf8))
        }
    }
}
