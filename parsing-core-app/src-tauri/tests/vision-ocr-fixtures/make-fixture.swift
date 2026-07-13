import AppKit
import CoreGraphics
import CoreText
import Foundation

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: make-fixture.swift OUTPUT.pdf\n".utf8))
    exit(64)
}

let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let pageRect = CGRect(x: 0, y: 0, width: 288, height: 144)
var mediaBox = pageRect
guard let consumer = CGDataConsumer(url: outputURL as CFURL),
      let context = CGContext(consumer: consumer, mediaBox: &mediaBox, nil) else {
    fatalError("could not create fixture PDF")
}

context.beginPDFPage(nil)
let attributes: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: 24, weight: .semibold),
    .foregroundColor: NSColor.black,
]
let chinese = CTLineCreateWithAttributedString(NSAttributedString(string: "中文测试", attributes: attributes))
let english = CTLineCreateWithAttributedString(NSAttributedString(string: "Vision OCR Test", attributes: attributes))
context.textPosition = CGPoint(x: 24, y: 88)
CTLineDraw(chinese, context)
context.textPosition = CGPoint(x: 24, y: 48)
CTLineDraw(english, context)
context.endPDFPage()
context.closePDF()
