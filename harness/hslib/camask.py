"""Core Animation reference for the icon tile's outline (openspec/changes/icon-corner-c; used only by app.icon).

`python3 -I camask.py <canvas> <x0> <body> <radius>` writes canvas×canvas 8-bit grey coverage to stdout: a CALayer
of side <body> at (<x0>, <x0>), white background, cornerRadius <radius>, cornerCurve = kCACornerCurveContinuous,
rendered (renderInContext) into a DeviceGray bitmap. Row order and y direction don't matter: the shape is symmetric.

The layer is not set to masksToBounds: its background is already drawn with the rounded outline, and a masked layer
is clipped as well as filled, so its edge pixels come out as coverage squared (measured 2026-10-09: half-covered
pixels read 0.29), which is Core Animation's compositing, not the shape.

The generator never uses this (it must be the same on every macOS); app.icon compares the generator's mask with it.
Runs in its own process so Core Animation is never loaded into the harness.
"""
import ctypes as C
import sys

V, D = C.c_void_p, C.c_double
objc = cg = qc = None                   # loaded by main(), never on import


class CGRect(C.Structure):
    _fields_ = [("x", D), ("y", D), ("w", D), ("h", D)]


def send(obj, sel, *args, restype=V, argtypes=()):
    f = C.CFUNCTYPE(restype, V, V, *argtypes)(("objc_msgSend", objc))
    return f(obj, objc.sel_registerName(sel.encode()), *args)


def main() -> int:
    global objc, cg, qc
    n, x0, body, radius = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    objc = C.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    cg = C.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    qc = C.cdll.LoadLibrary("/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
    objc.objc_getClass.restype = objc.sel_registerName.restype = V
    objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [C.c_char_p]
    cg.CGColorSpaceCreateDeviceGray.restype = V
    cg.CGColorCreate.restype = V
    cg.CGColorCreate.argtypes = [V, C.POINTER(D)]
    cg.CGBitmapContextCreate.restype = V
    cg.CGBitmapContextCreate.argtypes = [V, C.c_size_t, C.c_size_t, C.c_size_t, C.c_size_t, V, C.c_uint32]
    cg.CGContextTranslateCTM.argtypes = [V, D, D]
    gray = cg.CGColorSpaceCreateDeviceGray()
    layer = send(send(objc.objc_getClass(b"CALayer"), "alloc"), "init")
    if not layer:
        print("CALayer unavailable", file=sys.stderr)
        return 2
    send(layer, "setFrame:", CGRect(0, 0, body, body), restype=None, argtypes=(CGRect,))
    send(layer, "setBackgroundColor:", cg.CGColorCreate(gray, (D * 2)(1.0, 1.0)), restype=None, argtypes=(V,))
    send(layer, "setCornerRadius:", D(radius), restype=None, argtypes=(D,))
    send(layer, "setCornerCurve:", V.in_dll(qc, "kCACornerCurveContinuous"), restype=None, argtypes=(V,))
    buf = (C.c_uint8 * (n * n))()
    ctx = cg.CGBitmapContextCreate(buf, n, n, 8, n, gray, 0)
    if not ctx:
        print("CGBitmapContextCreate failed", file=sys.stderr)
        return 2
    cg.CGContextTranslateCTM(ctx, x0, x0)
    send(layer, "renderInContext:", V(ctx), restype=None, argtypes=(V,))
    sys.stdout.buffer.write(bytes(buf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
