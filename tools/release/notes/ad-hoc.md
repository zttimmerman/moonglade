Prebuilt Moonglade for Apple Silicon Macs. Bring your own World of Warcraft 3.3.5a client; no game files are included.

**Opening it the first time.** macOS can't check apps that aren't registered with Apple, so it refuses the first open. Unzip Moonglade, move it to Applications and open it once. Then go to System Settings → Privacy & Security, scroll down and click **Open Anyway**, then confirm (your Mac may ask for your password). You do this once for each new version.

Prefer Terminal? After moving Moonglade to Applications, this line does the same:

```
xattr -dr com.apple.quarantine /Applications/Moonglade.app
```

It removes the "downloaded from the internet" mark from Moonglade only; nothing else on your Mac changes.

Source for everything in the zip is attached (see SOURCES.json); verify downloads with SHA256SUMS.
