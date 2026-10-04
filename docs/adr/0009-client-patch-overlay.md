# ADR-0009: Revertable client patches on a derived "play client"

- Status: **accepted** (2026-10-04). Zach's explicit yes, given in chat on the plain-language summary of this ADR (pristine `client/` as the base, a derived play client with a hash-checked patch queue, one-step revert, a minimal first patch, each later patch needing its own ADR and yes).
- Scope of this ADR: the *mechanism*, plus the *first* patch (GlueXML signature check) needed for the in-client front menu. Every later binary patch gets its own short ADR (or a dated appendix here) and its own yes.

## Context
- North star: one single-player app whose front end is the game's own login screen turned into a profile/save/options menu (front-menu vision; Zach wants it as a fast follow, M5 slot 2).
- The login-screen spike (`docs/research/login-screen-spike.md`) proved that the 3.3.5a client RSA/MD5-verifies the whole GlueXML tree on every load. `XML_VerifyTocSignature` (@0x008165e0) returns 0 missing / 1 corrupt / 2 modified / 3 OK. The caller's switch (@0x004da7f1, @0x004da7fe, …) quits on anything but 3, and Phase B run 2 confirmed a one-line GlueXML edit makes the client quit. Blizzard's private key is unavailable, so re-signing is impossible. **An in-client menu therefore needs one small Wow.exe change.** Models, music and DBCs are not hashed, so the backdrop/music picker stays data-only.
- Spike Phase B already used an APFS clone (`cp -cR client/ env/spike-client/`, ~zero extra disk) and left `client/` untouched (`client.inspect` passed).

## Decision (proposed)
1. **`client/` stays pristine, always.** It is the clean base, verified by `client.inspect` against `env/client-manifest.json`. Nothing writes into it.
2. **A derived play client** at `env/client-play/`, built by a new `hs client-build`:
   - an APFS clone of `client/` (copy-on-write: unchanged files share storage);
   - then the patch queue `patches/client/` is applied in order. Two kinds:
     - **binary patches:** declarative records (file, offset, expected original bytes, replacement bytes, SHA-256 of the input and of the output). The tool refuses unless the input hash and the original bytes match exactly, so a wrong client or a double apply can't happen;
     - **data patches:** patch MPQs (e.g. `patch-enUS-4.MPQ` holding our GlueXML) built from sources in the repo by `hs mpq write`.
   - The build is deterministic: same queue + same base gives the same output hashes, which are recorded.
3. **Revert is trivial:** delete `env/client-play/` (or rebuild it without a patch), or switch the launcher setting `client = stock | patched`. Stock mode launches the pristine `client/` exactly as today.
4. **First patch (this ADR):** make the GlueXML signature result count as OK. The candidate is a minimal edit at the verify-result switch @0x004da7f1/0x004da7fe, so a missing/corrupt/modified GlueXML loads instead of quitting. The exact bytes are to be pinned by a builder from the disassembly and recorded with before/after hashes. Nothing else in the exe changes (FrameXML/AddOn handling untouched).
5. **Checks** (in the change that implements this):
   - the pristine `client/` is byte-identical to its manifest before and after a build;
   - the patched exe's hash equals the recorded output;
   - a build refuses a mismatched base or a double apply;
   - the patched client launches and reaches the login screen with a custom GlueXML (runtime, via the existing launch checks);
   - stock mode still launches unmodified;
   - removing the patch from the queue gives an exe identical to stock.
6. Personal use only; nothing produced is redistributed (CLAUDE.md §1).

## Alternatives considered
- **Patch `client/` in place.** Rejected: not revertable, destroys the clean base, and makes `client.inspect` meaningless.
- **Runtime memory patching or DLL injection** under Wine. Rejected for now: harder to verify, more moving parts, and fragile across Wine versions. A file patch is static and hash-checkable.
- **Re-signing GlueXML.** Impossible (needs Blizzard's RSA private key).
- **Launcher-side menu only (front menu step 1).** Works without patches, but isn't the target experience; it stays as a fallback.

## Consequences
- Disk: the clone costs ~nothing; the patched exe and our MPQ are a few MB.
- `client.inspect` keeps guarding `client/`; a new check guards `env/client-play/`.
- A foundation for later patches (e.g. WoWSilicon perf patches, other UI work), each needing its own ADR and Zach's yes.
- Risk: the patched client could behave differently in ways the checks don't see. Mitigated by keeping stock mode one setting away, plus human-verify play sessions.
