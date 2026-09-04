# RuView — repository analysis

*Analyzed from a fresh clone of `github.com/Ananthapadmanabhan333/RuView` (mirror of `ruvnet/RuView`), HEAD `a3b6e1d`, last commit 2026-08-19, MIT license, ~172 MB working tree.*

---

## 1. What it is

RuView (formerly *wifi-densepose*) is a **camera-free RF perception platform**. It takes Channel State Information (CSI) — the per-subcarrier complex channel response that a WiFi radio already computes — off cheap ESP32 boards, and turns it into presence, motion, breathing/heart rate, fall events, person counts, and (experimentally) 17-keypoint pose. Everything is designed to run at the edge: no cameras, no cloud, no wearables.

Around that core sits an unusually large amount of scaffolding: a home-automation runtime (HOMECORE), Home Assistant / Matter / HomeKit bridges, an edge-module ("cog") catalog, a governance layer (evidence, witness chains, capability certificates, OOD gating), and an AI contributor harness.

**Scale:** 901 Rust files / ~322k LOC · 269 Python files / ~73k LOC · ~396 JS/TS · 92 C files (firmware) · 251 ADRs · 31 GitHub workflows · ~5,300 test functions across 619 files.

---

## 2. Layout — where the real code lives

| Path | What it is | Weight |
|---|---|---|
| **`v2/crates/`** | **The active implementation.** ~70 Rust crates in one Cargo workspace. | Primary |
| `firmware/esp32-csi-node/` | ESP-IDF C firmware for ESP32-S3 / C6: CSI capture, edge DSP, mesh, WASM runtime, OTA, provisioning | Primary |
| `archive/v1/` | Original Python FastAPI pipeline. **Deprecated** — kept for the deterministic proof (`data/proof/verify.py`) and as reference | Reference |
| `python/` | PyO3 bindings → the `ruview` / `wifi-densepose` PyPI wheels + asyncio WS/MQTT clients | Supporting |
| `harness/ruview`, `harness/homecore` | `@ruvnet/ruview` npm CLI + MCP servers — agent guidance, "brain" corpus, claim-checking | Supporting |
| `ui/`, `dashboard/` | Browser observatory (vanilla JS + PWA) and a Vite/Lit dashboard for the nvsim simulator | Supporting |
| `docs/adr/` | 251 ADRs — the real design record; the README and CLAUDE.md both say ADR status beats summaries | Authoritative |
| `vendor/` (submodules) | `ruvector`, `rvcsi`, `rufield`, `midstream`, `metaharness`, `sublinear-time-solver` | External |
| `aether-arena/`, `benchmarks/`, `semconv/`, `plans/` | Benchmark harness, semantic conventions, planning docs | Peripheral |

Nine git submodules (`.gitmodules`) — including `v2/crates/ruv-neural`, `ruview-swarm`, `worldgraph` — so a plain `git clone` gives you an incomplete workspace. Clone with `--recursive`.

---

## 3. The data path (this is the part worth reusing)

```
ESP32-S3/C6  ──UDP──▶  aggregator  ──▶  signal DSP  ──▶  inference  ──▶  server  ──▶  consumers
 csi_collector.c        esp32_parser.rs   wifi-densepose-signal   -nn/-vitals   sensing-server   REST/WS/MQTT/HA/Matter
```

**Wire format (ADR-018)** — a 20-byte header plus raw I/Q, deliberately tiny:

```
0   4  magic 0xC5110001 (LE)
4   1  node id
5   1  n_antennas
6   2  n_subcarriers (u16 LE, ≤512 enforced)
8   4  frequency
12  4  sequence
16  1  RSSI (i8)      17  1  noise floor (i8)
18  1  PPDU type      19  1  flags (bw40 / STBC / LDPC / 802.15.4-sync)
20  N  I/Q pairs, (i8,i8) per subcarrier per antenna
```
3 antennas × 56 subcarriers = 356 bytes/frame. The same UDP port multiplexes sibling packet types (`0xC5110002` = 32-byte edge-vitals, etc.), so a CSI-only reader must skip, not choke. The parser has a stated **no-mock guarantee**: it parses real bytes or returns a typed `ParseError` — it never synthesizes data.

**Signal layer** (`wifi-densepose-signal`, ~27k LOC) is the densest genuinely valuable code: Hampel outlier filtering, phase sanitization, CSI-ratio (cross-antenna phase-offset cancellation), subcarrier selection, Fresnel-zone geometry, BVP (body-velocity profile), spectrograms, motion features.

**Vitals** (`wifi-densepose-vitals`): breathing = bandpass 0.1–0.5 Hz on wrapped phase + circular variance + zero-crossing; heart rate = bandpass 0.8–2.0 Hz. Classic, defensible DSP — not ML.

**Server** (`wifi-densepose-sensing-server`, ~44k LOC — the biggest crate) is an Axum app exposing ~75 REST routes and 8 WebSocket endpoints: `/api/v1/vital-signs`, `/pose/current`, `/sensing/latest`, `/models/{load,unload,lora/activate}`, `/train/*`, `/calibration/*`, `/recording/*`, `/rf/vendors/*`, `/ws/sensing`, `/ws/field`. Bearer auth + a WS-ticket flow + Cognitum OAuth (ES256/JWKS, verification-only) via `ruview-auth`.

**Key binaries:** `sensing-server`, `wifi-densepose` (CLI), `homecore-server`, `cog-pose-estimation`, `cog-person-count`, `cog-ha-matter`, `nvsim-server`, `train`/`verify-training`, `veil`.

---

## 4. Architectural character

Three concentric rings, and they are not equally mature:

1. **Physics + DSP core** — ESP32 firmware, frame parsing, signal processing, vitals. Deterministic, well-tested, hardware-grounded. This is the load-bearing part.
2. **Learned perception** — pose, person-count, encoders, OccWorld world model, LoRA/SONA profile switching, on-device training. Real code paths, but accuracy is mostly data- or hardware-gated (see §5).
3. **Governance / platform** — ~20 `ruview-*` crates from ADR-300 onward (ontology, attestation, evidence ledger, OOD gating, witness chain, capability certificates, policy authorization, digital RF twin, spatial memory, counterfactual inference, information-gain scheduling). Recent, ADR-driven, largely scaffolding-first: the interfaces exist and are tested, the science behind several of them is synthetic.

Plus a parallel HOMECORE stack (state machine, automations, WASM plugin runtime, REST/WS API, HAP bridge, recorder, migration-from-Home-Assistant) — effectively a second product inside the repo.

---

## 5. Credibility: how the project handles its own claims

This is the most distinctive thing about the repo, and worth copying regardless of whether you use the code.

`PROOF.md` grades every headline claim as **MEASURED** (reproduced, with a pinned test that fails on pre-fix code), **CLAIMED** (cited, not reproduced here), or **DATA/HARDWARE-GATED** (code path real, number not obtainable without a GPU/dataset/silicon). `scripts/prove.sh` exits non-zero only if a non-gated claim fails. `CLAUDE.md` makes the tagging mandatory for contributors and forbids presenting WiFi sensing as camera-grade.

The published negatives are the credible part:

- **Person identity from WiFi: not achieved.** Cardiac + respiratory channels give a separation gap of ~0.0005 — a committed test asserts the *failure*.
- **On-device pose is first-cut.** The shipped `pose_v1` is PCK@20 = 3.0% against a ≥35% target, and its runtime path still returns `confidence=0`. The 82.69% MM-Fi torso-PCK number is a *separate* published benchmark model, not the live cog.
- The old "100% presence accuracy" was **retracted**; the honest label-free figure is 82.3% held-out temporal-triplet accuracy.
- OccWorld carries `weights_trained=false` until a real checkpoint is loaded.
- Edge "skills" (seizure, weapon, affect detection…) are disclaimer-gated as unvalidated.
- 802.11bf is a forward-compat protocol model, not a certified implementation.

**How to read this as an integrator:** trust the DSP and the plumbing; treat every ML accuracy number as unverified until you reproduce it on your own data; assume the governance crates are interfaces rather than proven capability.

---

## 6. Risks and frictions

- **Claim-to-substance gradient.** The README is enormous (69 KB) and markets far ahead of what is validated. The honest caveats are there, but they are inline and easy to skim past. Read `PROOF.md` first, README second.
- **Surface area.** ~70 crates, two harnesses, HOMECORE, cogs, swarm, nvsim, SAR, privshield. Most of it is not needed for a sensing product; deciding what to *exclude* is the main integration task.
- **Submodule fragility.** Nine submodules across separate repos, several pinned to `main`. Non-recursive clones and drifting submodules are the likeliest first build failure.
- **Single-author velocity.** Every commit here is authored by `rUv`, ~1,650 PRs in, moving fast. Little external review pressure on the newer governance layers.
- **Hardware dependency.** Everything meaningful needs real CSI. Docker/simulated mode is for evaluation only; consumer laptops give RSSI-only presence.
- **Legal/ethical surface.** Through-wall sensing of people in homes, vitals, "possible-distress" states. The repo has a privacy shield (`privshield`/VEIL) and evidence/consent layers, but jurisdictional and consent questions are yours to answer.
- **Not a medical device.** Breathing/HR are contactless estimates with no clinical validation in-repo, and the code enforces disclaimers on medical modules.

---

## 7. If SenseWave AI builds on this

**Highest-value, lowest-risk to adopt:**

1. `firmware/esp32-csi-node` + the **ADR-018 wire format** — a working, cheap CSI capture path. Even if you rewrite everything above it, this saves months.
2. `wifi-densepose-signal` — the DSP is the real IP here.
3. `wifi-densepose-vitals` — deterministic breathing/HR, no model dependency.
4. The **claim-grading discipline** (`PROOF.md` + `MEASURED/CLAIMED/SYNTHETIC` tags + tests that fail on pre-fix code). Cheap to adopt, and it is what makes an RF-sensing claim believable to a customer or reviewer.

**Adopt selectively:** `sensing-server` (useful API shape, but 44k LOC of coupling), HA/Matter/HomeKit bridges (only if smart-home is your channel), the harness (only if you want agent-assisted contribution).

**Treat as research, not foundation:** pose estimation on-device, the ADR-300+ governance crates, world-model prediction, SAR/tomography, swarm.

**First three steps I'd take:**

1. `git clone --recursive`, then `cd v2 && cargo test --workspace --no-default-features` and `python archive/v1/data/proof/verify.py` (must print `VERDICT: PASS`). Establish that the claimed baseline actually reproduces on your machine before designing anything on top.
2. Buy two ESP32-S3 boards (~$18), flash the csi-node firmware, and capture your own CSI. Verify breathing rate against a manual count — that single experiment tells you more than the whole README.
3. Decide the boundary: does SenseWave consume RuView as (a) a fork, (b) the `ruview` PyPI wheel + `sensing-server` over its REST/WS API, or (c) just the firmware + signal crate with your own stack on top? Option (b) is the cheapest to start; (c) is the cheapest to maintain long-term.

---

### Key files to read, in order

`PROOF.md` → `CLAUDE.md` → `docs/adr/ADR-018` (wire format) → `v2/crates/wifi-densepose-signal/src/` → `v2/crates/wifi-densepose-sensing-server/src/lib.rs` → `firmware/esp32-csi-node/main/csi_collector.c` → `docs/user-guide.md`.

**Sources:** [RuView repo](https://github.com/Ananthapadmanabhan333/RuView) · upstream [ruvnet/RuView](https://github.com/ruvnet/RuView)
