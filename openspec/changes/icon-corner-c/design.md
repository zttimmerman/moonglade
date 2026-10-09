## Design
- **Mask.** Rasterise Apple's continuous-corner rounded rect as a closed outline. Use a well-documented reconstruction of the `cornerCurve = .continuous` path, such as the published UIBezierPath reverse-engineering curves (cite the source in the code), with analytic or supersampled coverage. This keeps the output deterministic across macOS versions.
- **Reference rendering.** A Core Animation render via ctypes, as in the preview script, is used only as the check's reference, with a tolerance. The generator doesn't use it.
- **Starting point.** The preview's helper scripts showed how variant C was rendered: a Core Animation mask via ctypes, passed to `render_tile(logo, mask=…)` with the default logo diameter.
- **What stays the same.** The navy gradient, glow, shadow parameters and logo diameter are unchanged from #29 (variant C used them). The shadow is derived from the mask, as `render_tile` already does.
- **Geometry from the record.** `app.icon` keeps taking its geometry from the `tile` record. Its `continuous…` envelope judgement stays, and it gains the reference-mask comparison and the 45° corner samples.
