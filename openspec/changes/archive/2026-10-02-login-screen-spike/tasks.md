# Tasks

## Phase A: read-only, no stack (builder, worktree)
- [x] A.1 `hs mpq list|extract` (read-only) + check `re.mpq_readonly` — verify: `re.mpq_readonly`
- [x] A.2 Q2: find login backdrop models and music for WotLK/TBC/Vanilla in the 3.3.5a MPQs (listing evidence)
- [x] A.3 Q1 analysis: Ghidra xrefs/decompile of the GlueXML signature checks; what they verify, when, and against what (e.g. a signature file in a specific MPQ)
## Phase B: launch test (needs the stack free)
- [x] B.1 Controlled test: throwaway patch MPQ with a trivially modified GlueXML in a scratch copy of the Data folder (or added and then removed, with `client.inspect` proving restoration); observe the result; screenshots and logs
## C. Report
- [x] C.1 `docs/research/login-screen-spike.md` with a recommendation
