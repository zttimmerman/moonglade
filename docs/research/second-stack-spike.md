# Second harness stack: Phase 0 spike (ADR-0011)

- Date: 2026-10-08, main 64f4f9c, 16 GB Apple Silicon Mac (hw.memsize 17179869184).
- Two questions: (1) how much memory does one lane use, and do two lanes fit? (2) does the 3.3.5a client honour `set realmlist host:port`?
- Nothing in the repo was changed. The stack was down and no server, Wine or Wow processes were left running at the end.

## 1. Memory

### Method
- A sampler ran every 5 s. For each process it recorded `ps -o rss` and `footprint -p` (phys_footprint, which includes compressed pages). For the whole system it recorded `memory_pressure` free %, `vm_stat` (free/active/wired/compressor) and `sysctl vm.swapusage`.
- **Server run:** `harness/hs up` (m0 profile, 10 random bots; ready in 16 s), sampled for 6 min from cold start through bot login and the first re-rolls.
- **Client run:** `harness/hs run --tier full --only login.auto_login --only client.wording_addon`, which passed twice. Each check starts its own ./play stack and a real Wow.exe from the play client under Wine. `client.wording_addon` enters the world, waits 8 s there and logs out.
  - The first run's Wow.exe samples were lost to a sampler quoting bug, so the client numbers below come from the second run.
- **Baseline before the runs:** swap was already at 2.92 of 4.10 GB. Neither run added any swap.
  - With the stack down, free + inactive + speculative + purgeable was about 7.9 GB (free 4.0, inactive 3.7) and wired was 2.5 GB.

### Measured (MB, phys_footprint unless noted)
| Process | Peak | Steady (last 30 s) | Notes |
|---|---|---|---|
| mysqld | 1215 (RSS 831) | 805 | `innodb_buffer_pool_size=512M` |
| authserver | 7 | 7 | |
| worldserver, m0 idle | 3527 (RSS 2571) | 3518, still rising ~50 MB/min at 6 min | 715 MB at +6 s, 2.9 GB at +30 s (bots logging in) |
| **Stack total** | **4628** (RSS 3422) | ~4330 | |
| worldserver under ./play, client check | 3100 | 2975 | fresh stack each check |
| Wow.exe (Wine, Rosetta) | 448 | 280–300 at char list | in-world peak, measured only 8 s |
| Wine helpers (wineserver, explorer, services, winedevice×2, plugplay, svchost, rpcss, start) | ~130 | ~130 | |
| **Client total (Wine side)** | **577** | | |
| **One lane: stack + client** | **4400** under ./play; 4628 + 577 ≈ **5.2 GB** worst case | | |

**System effect of one lane.**
- **Server only:** free pages fell from 3.0 GB to about 0.5 GB and the compressor grew by about 1.3 GB (1.8 to 3.1 GB). `memory_pressure` free % went from 72 to a minimum of 62.
- **With the client:** free % fell to a minimum of 59 and the compressor reached 3.5 GB.
- No swap was added in either case, and `kern.memorystatus_vm_pressure_level` stayed at 1 (normal).

### Projection: lanes A and B together
- **Phase 1** (A runs the ./play and client checks; B runs the stack and stand-ins):
  - Footprint: A's ~5.2 GB plus B's ~4.6 GB comes to about **9.8 GB**.
  - Headroom: about 7.9 GB is reclaimable with nothing running, so the remaining ~2 GB has to come from compression, which today absorbs about 1.3–1.7 GB per lane.
  - Expected result: high compressor use, swap likely to grow a little, and the system probably staying out of the "warn" pressure level.
- **Phase 2** (a client in each lane):
  - Measured: 2 × 5.2 = **~10.4 GB**.
  - Conservative: a client that stays in the world longer, in a busier zone, plausibly reaches 1–1.5 GB. Our client was in the world for only 8 s. That gives **~11–12 GB**, which needs swap on this Mac.
- These figures match the ADR's estimates of ~4 GB per lane without a client and ~5.5 GB with one. The ADR's 10–11 GB peak holds for Phase 2.

### Memory preflight: viable, with these thresholds
- **Before starting B** (A's stack already up): require all three:
  - `kern.memorystatus_vm_pressure_level == 1`;
  - free + inactive + speculative + purgeable pages (from `vm_stat`) **≥ 5.0 GB**, which is B's measured 4.6 GB plus a margin;
  - swap free **≥ 1 GB**, or swap used no more than 1 GB above where the run started.
  - For Phase 2 (a client in B), raise the page threshold to **≥ 6.0 GB**.
- **During the run:** if the pressure level reaches 2 (warn), or swap grows by more than 1.5 GB over the run's start, let B finish its current check, then fall back to one lane.
- `memory_pressure`'s free % works as a cheap cross-check. With A up the floor was 59–63 %; refuse B below about 55 %.
- Two caveats:
  - worldserver is still growing at 6 min (~50 MB/min). A long lane-B session should be re-measured in the first nightly runs with lane mode on.
  - The 2.9 GB of swap left over from earlier sessions means a reboot gives more headroom than these numbers show.

## 2. Realmlist port: the 3.3.5a client honours `host:port` (static analysis, high confidence)

### Method
- Read-only analysis per ADR-0005, on the copy `env/re/bin/Wow.exe`. Its SHA-256 is aa63a5750d60ef16746c686b3d5e26876d98953eab08b1c026cd0faf78e88cb8, the same as `client/Wow.exe`.
- Tools: raw string and xref scans, plus `objdump -d` (Apple LLVM, coff-i386).
- `hs re decompile` didn't work: Ghidra 12.1.4 ships no `os/mac_arm_64/decompile` ("DecompileProcessFactory: ... does not exist"). Disassembly was enough to answer the question.

### Auth connection (the `realmList` CVar)
1. **The CVar is registered with a port in its default value.**
   - At 0x6b0d6b: name "realmList" @0x9fe580, help text "Address of realm list server" @0xa3099c, default **"us.logon.worldofwarcraft.com:3724"** @0xa309bc. The handle is stored at 0xc79d00.
   - Right after registration the client runs "%srealmlist.wtf" @0xa3098c.
2. **The CVar's string is passed to the login object.**
   - At 0x6b2cab: `mov edx,[0xc79d00]; mov eax,[edx+0x28]` reads the CVar's string, which is then passed to virtual slot +0x5c of the login object at [0xc79cec].
   - That object's vtable is 0xa94928, set in its constructor at 0x8cab8d; slot 0xa94984 (+0x5c) holds **0x8ca740**.
3. **0x8ca740 (GruntLogin) falls back to the built-in default.** If the argument is null it substitutes the default string at 0x8ca7cc (`mov eax,0xa309bc`), then calls **0x8cbb40**.
4. **0x8cbb40 (the Grunt client connect; it allocates using ".\Grunt.cpp" @0xa94ac0) checks for a colon:**
   - `push 0x3a; push edi; call 0x76e6e0` looks for ':' in the address.
   - **If there is a colon:** it calls 0x468e50(addr, 5000).
   - **If not:** at 0x8cbbd2 it runs `push 0xe8c` (**3724**) and calls 0x468d20(addr, 3724, 5000).
5. **0x468e50 splits "host:port":**
   - It finds ':' again and runs `call 0x76f0d0` (atoi) on the text after it, storing the result as `word [this+0x70]` (the port).
   - It copies the host up to the colon into a 256-byte buffer, then calls 0x468d20(host, port, timeout), the same socket-connect routine the default path uses.
6. **Conclusion:** `set realmlist 127.0.0.1:3734` makes the client connect to the authserver on 3734. Without a colon the port is hard-coded to 3724.
   - Confidence: high. Every step from the CVar to the socket call is traced by address, and the shipped default value itself contains `:3724`.
   - What isn't covered: no client has been run against port 3734. Phase 2's first check will cover that.

### World connection (the realm list entry's address)
- **0x6323c0 (NetClient):**
  - It copies the realm address string into a 1024-byte buffer and sets the default port `mov edi,0x2382` (9090).
  - It finds ':', writes a NUL there, converts the rest with atoi (0x40bc0b) to get the port, and calls 0x631e10(host, port).
  - 0x631e10 asserts "Expected (m_netState == NS_INITIALIZED)" @0xa26d04 (".\NetClient.cpp" @0xa26cf4) and calls 0x468d20(host, port, -1).
- So **the world port comes from the realm list entry's address string**. AzerothCore's authserver builds that string from `realmlist.address` and `realmlist.port`. Today `client.realmlist` checks it is 127.0.0.1:8085.
- Lane B's realm row should therefore be **127.0.0.1:8095**. Confidence: high (this is also how every private server with a non-default world port works).
- A related path: 0x8c6d80 formats "%d.%d.%d.%d:%d" @0xa31cb4 into "Connecting to %s" @0xa94694 and calls the same host:port splitter, 0x468e50. Port parsing is shared code.

## Recommendation
- **Phase 1: GO**, behind the memory preflight above. The two lanes fit with compression on this 16 GB Mac. Population checks stay in lane A, as the ADR already says.
- **Phase 2: GO technically.** The client honours `host:port` for auth, and the realm entry controls the world port.
- **Phase 2 on memory: conditional.**
  - Two clients come to about 10.4 GB measured and 11–12 GB with a conservative client budget, which means swap on this Mac.
  - Turn Phase 2 on only after Phase 1 nightly runs show the preflight holding, with the 6 GB threshold for a client in B. Keep the one-lane fallback.
