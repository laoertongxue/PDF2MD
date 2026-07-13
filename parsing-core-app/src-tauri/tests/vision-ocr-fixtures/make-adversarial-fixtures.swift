import AppKit
import CoreGraphics
import CoreText
import Foundation
import PDFKit

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: make-adversarial-fixtures.swift OUTPUT_DIR\n".utf8))
    exit(64)
}

let outputDirectory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

func drawText(_ text: String, at point: CGPoint, size: CGFloat, context: CGContext) {
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: .bold),
        .foregroundColor: NSColor.black,
    ]
    context.textMatrix = .identity
    context.textPosition = point
    CTLineDraw(CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attributes)), context)
}

func makeSinglePagePDF(
    named name: String,
    mediaBox initialMediaBox: CGRect,
    options: CFDictionary? = nil,
    draw: (CGContext) -> Void = { _ in }
) {
    let url = outputDirectory.appendingPathComponent(name)
    var mediaBox = initialMediaBox
    guard let consumer = CGDataConsumer(url: url as CFURL),
          let context = CGContext(consumer: consumer, mediaBox: &mediaBox, options) else {
        fatalError("could not create \(name)")
    }
    context.beginPDFPage(nil)
    draw(context)
    context.endPDFPage()
    context.closePDF()
}

makeSinglePagePDF(
    named: "huge-media-box.pdf",
    mediaBox: CGRect(x: 0, y: 0, width: 100_000, height: 100_000)
)

let encryptedOptions = [
    kCGPDFContextUserPassword as String: "",
    kCGPDFContextOwnerPassword as String: "fixture-owner",
] as CFDictionary
makeSinglePagePDF(
    named: "encrypted.pdf",
    mediaBox: CGRect(x: 0, y: 0, width: 200, height: 100),
    options: encryptedOptions
)

let lockedOptions = [
    kCGPDFContextUserPassword as String: "fixture-user",
    kCGPDFContextOwnerPassword as String: "fixture-owner",
] as CFDictionary
makeSinglePagePDF(
    named: "locked.pdf",
    mediaBox: CGRect(x: 0, y: 0, width: 200, height: 100),
    options: lockedOptions
)

let unrotatedURL = outputDirectory.appendingPathComponent("crop-rotate-unrotated.pdf")
let cropRotateURL = outputDirectory.appendingPathComponent("crop-rotate.pdf")
var mediaBox = CGRect(x: 0, y: 0, width: 200, height: 120)
guard let consumer = CGDataConsumer(url: unrotatedURL as CFURL),
      let context = CGContext(consumer: consumer, mediaBox: &mediaBox, nil) else {
    fatalError("could not create crop-rotate.pdf")
}

let cropBox = CGRect(x: 20, y: 10, width: 100, height: 60)
for rotation in [90, 180, 270] {
    let pageInfo = [kCGPDFContextCropBox as String: cropBox] as CFDictionary
    context.beginPDFPage(pageInfo)
    context.saveGState()
    switch rotation {
    case 90:
        context.translateBy(x: cropBox.maxX, y: cropBox.minY)
        context.rotate(by: .pi / 2)
    case 180:
        context.translateBy(x: cropBox.maxX, y: cropBox.maxY)
        context.rotate(by: .pi)
    case 270:
        context.translateBy(x: cropBox.minX, y: cropBox.maxY)
        context.rotate(by: -.pi / 2)
    default:
        break
    }
    let visibleHeight: CGFloat = rotation == 180 ? cropBox.height : cropBox.width
    context.setFillColor(NSColor.black.cgColor)
    context.fill(CGRect(x: 4, y: visibleHeight - 12, width: 16, height: 8))
    drawText("UP \(rotation)", at: CGPoint(x: 8, y: visibleHeight / 2 - 8), size: 15, context: context)
    context.restoreGState()
    context.endPDFPage()
}
context.closePDF()

guard let document = PDFDocument(url: unrotatedURL) else {
    fatalError("could not reopen crop-rotate fixture")
}
for (index, rotation) in [90, 180, 270].enumerated() {
    guard let page = document.page(at: index) else {
        fatalError("missing crop-rotate fixture page")
    }
    page.setBounds(cropBox, for: .cropBox)
    page.rotation = rotation
}
guard document.write(to: cropRotateURL) else {
    fatalError("could not write crop-rotate fixture")
}
try FileManager.default.removeItem(at: unrotatedURL)
