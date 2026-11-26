import AppKit
import CoreGraphics
import Foundation

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: generate_icon.swift <output_iconset_dir>\n", stderr)
    exit(1)
}

let iconsetDir = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: iconsetDir, withIntermediateDirectories: true)

func renderIcon(pixelSize: Int) -> Data? {
    let s = CGFloat(pixelSize)
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixelSize,
        pixelsHigh: pixelSize,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else { return nil }

    NSGraphicsContext.saveGraphicsState()
    guard let nsCtx = NSGraphicsContext(bitmapImageRep: rep) else { return nil }
    NSGraphicsContext.current = nsCtx
    let ctx = nsCtx.cgContext
    ctx.setAllowsAntialiasing(true)
    ctx.setShouldAntialias(true)

    let pad = s * 0.095
    let rect = CGRect(x: pad, y: pad, width: s - pad * 2, height: s - pad * 2)
    let radius = rect.width * 0.225
    let bgPath = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)

    // Deep obsidian-indigo squircle backdrop
    ctx.saveGState()
    ctx.addPath(bgPath)
    ctx.clip()
    let cs = CGColorSpaceCreateDeviceRGB()
    let bgGrad = CGGradient(
        colorsSpace: cs,
        colors: [
            CGColor(red: 0.12, green: 0.13, blue: 0.22, alpha: 1.0),
            CGColor(red: 0.06, green: 0.07, blue: 0.13, alpha: 1.0)
        ] as CFArray,
        locations: [0.0, 1.0]
    )!
    ctx.drawLinearGradient(bgGrad, start: CGPoint(x: s * 0.2, y: s * 0.92), end: CGPoint(x: s * 0.85, y: s * 0.08), options: [])

    // Subtle radial top-left luminescence
    let glow = CGGradient(
        colorsSpace: cs,
        colors: [
            CGColor(red: 0.49, green: 0.42, blue: 0.96, alpha: 0.32),
            CGColor(red: 0.49, green: 0.42, blue: 0.96, alpha: 0.0)
        ] as CFArray,
        locations: [0.0, 1.0]
    )!
    ctx.drawRadialGradient(glow, startCenter: CGPoint(x: s * 0.34, y: s * 0.72), startRadius: 0,
                           endCenter: CGPoint(x: s * 0.34, y: s * 0.72), endRadius: s * 0.52, options: [])
    ctx.restoreGState()

    // Inner glass border
    ctx.saveGState()
    ctx.addPath(bgPath)
    ctx.setStrokeColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.16))
    ctx.setLineWidth(max(1.0, s * 0.012))
    ctx.strokePath()
    ctx.restoreGState()

    let cx = s * 0.5
    let cy = s * 0.5
    let rOuter = s * 0.275
    let ringW = s * 0.062

    // Background track ring
    ctx.saveGState()
    ctx.setStrokeColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.11))
    ctx.setLineWidth(ringW)
    ctx.addArc(center: CGPoint(x: cx, y: cy), radius: rOuter, startAngle: 0, endAngle: .pi * 2, clockwise: false)
    ctx.strokePath()
    ctx.restoreGState()

    // Active work-hours gradient arc (270 degrees clockwise from 12 o'clock)
    ctx.saveGState()
    let arcPath = CGMutablePath()
    arcPath.addArc(center: CGPoint(x: cx, y: cy), radius: rOuter,
                   startAngle: .pi / 2, endAngle: .pi / 2 - .pi * 1.48, clockwise: true)
    let stroked = arcPath.copy(strokingWithWidth: ringW, lineCap: .round, lineJoin: .round, miterLimit: 2)
    ctx.addPath(stroked)
    ctx.clip()
    let arcGrad = CGGradient(
        colorsSpace: cs,
        colors: [
            CGColor(red: 0.55, green: 0.48, blue: 0.98, alpha: 1.0),
            CGColor(red: 0.22, green: 0.74, blue: 0.97, alpha: 1.0),
            CGColor(red: 0.13, green: 0.77, blue: 0.48, alpha: 1.0)
        ] as CFArray,
        locations: [0.0, 0.55, 1.0]
    )!
    ctx.drawLinearGradient(arcGrad, start: CGPoint(x: cx - rOuter, y: cy + rOuter),
                           end: CGPoint(x: cx + rOuter, y: cy - rOuter), options: [])
    ctx.restoreGState()

    // Crisp clock hands
    ctx.saveGState()
    ctx.setStrokeColor(CGColor(red: 0.96, green: 0.97, blue: 1.0, alpha: 0.96))
    ctx.setLineCap(.round)
    ctx.setLineWidth(max(1.5, s * 0.044))
    ctx.move(to: CGPoint(x: cx, y: cy))
    ctx.addLine(to: CGPoint(x: cx, y: cy + rOuter * 0.58))
    ctx.strokePath()

    ctx.setLineWidth(max(1.5, s * 0.038))
    ctx.move(to: CGPoint(x: cx, y: cy))
    ctx.addLine(to: CGPoint(x: cx + rOuter * 0.46, y: cy - rOuter * 0.22))
    ctx.strokePath()

    // Center live emerald dot
    let dotR = s * 0.042
    ctx.setFillColor(CGColor(red: 0.13, green: 0.77, blue: 0.37, alpha: 1.0))
    ctx.fillEllipse(in: CGRect(x: cx - dotR, y: cy - dotR, width: dotR * 2, height: dotR * 2))
    ctx.restoreGState()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

let specs: [(String, Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024)
]

for (name, px) in specs {
    if let png = renderIcon(pixelSize: px) {
        try png.write(to: iconsetDir.appendingPathComponent(name))
    }
}
print("Generated iconset at \(iconsetDir.path)")
