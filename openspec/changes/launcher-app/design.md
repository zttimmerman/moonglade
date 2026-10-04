# Design: Launcher .app

## Decisions
- **Bundle shape:** `Contents/Info.plist` (CFBundleIdentifier `local.hearthstone.app`, CFBundleIconFile `AppIcon`, LSMinimumSystemVersion matching the host), `Contents/MacOS/Hearthstone` (launcher executable), `Contents/Resources/AppIcon.icns`, plus a small `Contents/Resources/repo-path` file written at build time. Ad-hoc codesign (`codesign -s -`) so Gatekeeper doesn't block a locally built app; verify with `codesign --verify`.
- **Launcher:** a tiny executable that resolves the repo path and runs the play loop (`./play` semantics) without a Terminal window. Output goes to `env/logs/app.log`. Status and errors use stdlib-reachable macOS UI (`osascript` display notification/dialog). A test hook (env var) suppresses dialogs and records what would have been shown, so checks can assert them headlessly.
- **Icon pipeline:** stdlib-only harness, so the image work uses macOS tools (`sips`, `iconutil`, and a short Swift/CoreImage script if masking needs it). The circular mask's centre and radius are derived from the image (the gold ring), not hardcoded guesses; the generator documents how. Output is deterministic for a given source.
- **Single instance:** reuse the play loop's existing lock; the app maps "already running" to a dialog and exits 0.
- **Future:** the front menu (next change) will open from this app instead of going straight into the game.

## Risks
- Gatekeeper/quarantine on a locally built bundle → ad-hoc signing, no quarantine xattr (built locally, never downloaded).
- osascript dialogs need the app to be allowed to send Apple Events → a first-run prompt is possible; documented as human-verify.
