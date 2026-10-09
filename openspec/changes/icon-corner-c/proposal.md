## Why
Zach picked corner shape **C** for the app icon on 2026-10-09 ("Sure C is fine"), from the preview sheet that compared:
- A: the approved circular-arc tile, 824 px with radius 185.4;
- B: Apple's Big Sur–Sequoia template, 824 px with continuous corners at radius 185.4;
- C: macOS 26's measured shape, 832 px with continuous corners at radius 216.

PR #29 shipped A. C matches the shape macOS 26 draws for its own apps, so Moonglade's icon lines up with the rest of the Dock.

## What Changes
- `harness/hslib/icon.py` gains a deterministic continuous-corner mask. It's a pure-Python rasterised outline, so `app.icon`'s pixel-for-pixel reproducibility doesn't depend on the macOS version. The mask is cross-checked against a Core Animation rendering.
- `branding/AppIcon.json`'s `tile` record changes to the continuous shape, body 832, radius 216. The PNG, the .icns and the json are regenerated.
- The logo stays the size it was in Zach's preview. The gradient, glow and shadow are unchanged.
- `app.icon` gains the shape assertions in the new scenario.
- The dev app is rebuilt once with `harness/hs app`. TCC shouldn't re-prompt, since the designated requirement is unchanged and the last rebuild didn't re-prompt.

## Sources
- C's geometry: the matplotlib issue 31895 measurement (comment by iccir), as recorded in the archived design `2026-10-09-release-signing/design.md`.
- Continuous corners: Core Animation's `CALayer.cornerCurve = .continuous`, used as the reference rendering.
- The curve construction: "Drawing Code for iOS 7 Rounded Rectangles" (PaintCode/PixelCut, 2013-11-18), with the outer curves' end points as Core Animation emits them on macOS 26 (design "As built").

## Spec-change log
- 2026-10-09: the "App bundle with the navy-tile icon" requirement now names the tile shape (832/216, continuous) and adds a scenario for it. This is a tightening.
