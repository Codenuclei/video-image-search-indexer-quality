#!/usr/bin/env swift

import AppKit
import Foundation
import PDFKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

guard CommandLine.arguments.count >= 3 else {
    fail("usage: render-pdf-pages.swift INPUT.pdf OUTPUT_DIR [scale]")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
let scale = CommandLine.arguments.count > 3
    ? max(0.5, Double(CommandLine.arguments[3]) ?? 2.0)
    : 2.0

guard let document = PDFDocument(url: inputURL) else {
    fail("could not open \(inputURL.path)")
}

do {
    try FileManager.default.createDirectory(
        at: outputURL,
        withIntermediateDirectories: true
    )
} catch {
    fail("could not create output directory: \(error)")
}

let baseName = inputURL.deletingPathExtension().lastPathComponent
for index in 0..<document.pageCount {
    guard let page = document.page(at: index) else { continue }
    let box = page.bounds(for: .mediaBox)
    let width = max(1, Int(ceil(box.width * scale)))
    let height = max(1, Int(ceil(box.height * scale)))

    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: width,
        pixelsHigh: height,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        fail("could not allocate bitmap for page \(index + 1)")
    }

    bitmap.size = NSSize(width: box.width, height: box.height)
    NSGraphicsContext.saveGraphicsState()
    guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        fail("could not create graphics context for page \(index + 1)")
    }
    NSGraphicsContext.current = context
    NSColor.white.setFill()
    NSRect(x: 0, y: 0, width: width, height: height).fill()

    context.cgContext.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context.cgContext)
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()

    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        fail("could not encode page \(index + 1)")
    }
    let filename = String(format: "%@-page-%02d.png", baseName, index + 1)
    let destination = outputURL.appendingPathComponent(filename)
    do {
        try png.write(to: destination, options: .atomic)
        print(destination.path)
    } catch {
        fail("could not write page \(index + 1): \(error)")
    }
}
