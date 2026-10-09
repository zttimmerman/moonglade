## 1. Shape C
- [ ] 1.1 Deterministic continuous-corner mask in `harness/hslib/icon.py`; `tile_geometry` reads shape/body/radius from constants that `generate()` records; logo diameter unchanged
- [ ] 1.2 Regenerate `branding/AppIcon.{png,icns,json}` (`tile`: continuous, body 832, x0=y0=96, radius 216); confirm it matches preview variant C to within anti-aliasing
- [ ] 1.3 `app.icon`: shape assertions per the new scenario (record values, Core Animation reference comparison, circular-arc counterexample fails, 45° corner samples)
- [ ] 1.4 Rebuild the dev app once (`harness/hs app`); confirm the designated requirement is unchanged; report any TCC prompt (never answer one)
- [ ] 1.5 Docs: design "As built"; send the orchestrator a fresh 3-size preview PNG path
