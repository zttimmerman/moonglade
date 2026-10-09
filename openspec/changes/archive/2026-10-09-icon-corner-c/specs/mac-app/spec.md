## MODIFIED Requirements

### Requirement: App bundle with the navy-tile icon
`harness/hs app` SHALL build `dist/Moonglade.app`, a valid, ad-hoc-signed macOS app bundle whose icon is Zach's logo on a dark navy macOS rounded-square tile (Zach, 2026-10-08) whose outline is macOS 26's app-icon shape: an 832 px body on the 1024 px canvas with continuous-curvature corners of radius 216 px (Zach picked option C, 2026-10-09). Its Info.plist SHALL carry no "not for redistribution" (or other "redistribution") wording: the source is public and releases are intended (ADR-0012).

#### Scenario: Bundle is valid and signed
- **GIVEN** a clean checkout with the committed branding assets
- **WHEN** `harness/hs app` runs
- **THEN** `dist/Moonglade.app` exists; `plutil -lint` passes on its Info.plist; the plist names the executable, the bundle identifier and the icon, and no value in it contains "redistribut" (any case); the executable is present and executable; `codesign --verify --deep --strict` passes; and nothing outside the repo was written
- Check: `app.bundle_valid`

#### Scenario: Icon is the logo on a navy tile
- **GIVEN** the committed `AppIcon.icns` and its PNG
- **WHEN** the harness decodes them
- **THEN** the .icns contains every standard size from 16 to 1024 px (incl. @2x); in the 1024 px image, with the tile's geometry taken from AppIcon.json's `tile` record (never hard-coded in the check), the canvas corners and every pixel outside the tile's rounded square and its recorded shadow band are fully transparent (alpha 0); a ring of sample points just inside the tile's edge, including its four rounded corners, is opaque and within a small colour distance of the recorded navy; the centre is opaque; the gold ring is present at the recorded scale (sampled points on the ring are gold, not navy); and re-running the generator on the logo source reproduces the committed PNG pixel for pixel. A scratch icon rendered with the old transparent layout, and one with a white tile, each fail
- Check: `app.icon`

#### Scenario: The tile has macOS 26's continuous-corner shape
- **GIVEN** the committed `AppIcon.png` and AppIcon.json's `tile` record
- **WHEN** the harness reads the record and renders the tile mask with the generator
- **THEN** the record names a continuous-corner shape with body 832 px and corner radius 216 px, centred on the 1024 px canvas (x0 = y0 = 96 px), and the logo diameter shown in Zach's approved preview (unchanged from the circular-arc tile); the generator's mask matches a Core Animation `cornerCurve = continuous` rendering of the same body and radius to within 1/255 mean and 64/255 at any pixel (anti-aliasing only), and a mask whose corners are circular arcs of the same radius fails that comparison; the committed PNG's tile-edge samples (incl. the corners at 45°) lie inside the continuous outline, not the circular one
- Check: `app.icon`
