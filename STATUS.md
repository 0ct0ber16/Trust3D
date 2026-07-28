# Trust3D Status

## Gate 0: Simulator smoke test

Status: PASS

Decision: GO to Gate 1.

Environment audit:

- Host: Ubuntu 22.04-compatible Linux, x86_64
- Simulator environment: `trust3d-sim`, Python 3.7.12
- Simulator: `ai2thor==2.1.0`
- Display: Xvfb 21.1.4
- Workspace filesystem: 639 TB available at audit time
- GPU: not required for Gate 0; existing GPU workloads are left untouched

Commands:

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

timeout 180s xvfb-run -a \
  /224010104/miniconda3/envs/trust3d-sim/bin/python -u \
  -m trust3d.sim.controller \
  --smoke-test \
  --scene FloorPlan1 \
  --output outputs/gate0
```

Acceptance results on 2026-07-28:

- Unit tests: 4 passed
- Final code commit under test: `1d9f2220f4808998a20a15c794eb63eab6d7e843`
- AI2-THOR build SHA256: `cc61de810ddb8d681e9f6a7e342dd2d2c8454db413dd681de6bc85d2b2f70459`
- RGB: 300x300x3, value range 64-255, non-black
- Depth: 300x300, 90,000 finite values, range 671.15-1199.56 mm
- Instance segmentation: 300x300x3, 3 colors in the initial view
- Metadata: Agent pose present, 75 objects present
- Legal action: `RotateRight`, success
- Determinism: four cold starts across two independent commands produced the same object-state hash
- Object-state hash: `57ad74a8e3b2ca5dfe909df906b5ca44fd3e373d4fb29e92cc85aa934979ef63`

Tracked reports:

- `outputs/gate0/env.json`
- `outputs/gate0/report.json`

Local-only artifacts:

- `outputs/gate0/rgb.png`
- `outputs/gate0/depth.npy`
- `outputs/gate0/instance_segmentation.png`
- `outputs/gate0/metadata.json`

Issues and resolutions:

1. The default Conda channels required acceptance of Anaconda terms. The environment uses `conda-forge` plus `nodefaults` instead; no terms were accepted automatically.
2. Unpinned Flask/Werkzeug 2.2.x deadlocked the AI2-THOR multipart image response. The fixed ALFRED commit officially pins Flask and Werkzeug 2.0.3, which resolved the deadlock.
3. A plain scene reset allowed movable objects to settle differently. `InitialRandomSpawn` now receives seed 17 with `placeStationary=true`; repeated full object-state hashes are stable.
4. Headless Unity reports missing audio and optional `ScreenSelector.so`. RGB, depth, segmentation, metadata, and actions are unaffected.

## Gate 1: ALFRED JSON candidate scan

Status: PASS

Decision: GO to Gate 2.

Data audit:

- ALFRED commit: `f91f4c0c96c7a29f33d0557f86b0a21035379b3b`
- Dataset: ALFRED `data/json_2.1.0`
- JSON files in manifest: 8,051 (1,401,308,505 bytes)
- Public trajectories with action plans: 7,080
- Hidden test trajectories without action plans: 971 (483 seen, 488 unseen)
- Dataset manifest SHA256: `a2546fc364e3d88f418f0ead85ebe73c0f19a21b1fed9877bfe8ed18dac29874`

Commands:

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.scan_alfred \
  --alfred-json external/alfred/data/json_2.1.0 \
  --events OpenObject CloseObject \
  --output outputs/gate1/candidates.jsonl \
  --stats outputs/gate1/stats.json
```

Acceptance results on 2026-07-28:

- Unit tests: 11 passed overall, including 7 Gate 1 scanner tests
- Final code commit under test: `9c2a12f`
- Candidate events: 14,306 (7,116 OpenObject, 7,190 CloseObject)
- Candidate splits: 13,205 train, 516 valid_seen, 585 valid_unseen
- MVP action/object whitelist matches: 14,212
- Distinct candidate trajectories/scenes: 3,420 / 95
- Parse errors: 0 of 7,080 processable trajectories (0.00%)
- Action validation errors: 0
- Traceability: all candidate records include source JSON, action index, and target instance ID
- Manual audit: 10 of 10 records matched the original action and target ID
- Manual audit coverage: 4 train, 3 valid_seen, 3 valid_unseen; both OpenObject and CloseObject
- Reproducibility: two complete scans produced byte-identical outputs
- Candidate output SHA256: `1e9c4544cd30f273fce9acfc404618e8871c5b5a4ffef7a4c0a917991fab734a`
- Statistics output SHA256: `7b79cd25bb9c53717c60927e1e936881ce0d9045ddb9fcd789b08333b9aaf12e`

Tracked reports:

- `outputs/gate1/candidates.jsonl`
- `outputs/gate1/stats.json`

Interpretation notes:

1. ALFRED intentionally withholds `plan` from tests_seen and tests_unseen. These 971 files remain covered by the dataset manifest but are reported as unannotated skips, not parser failures.
2. Openable instance counts use `gen/layouts/*-openable.json`, then trajectory object poses for movable openables. For 250 Cabinet/Drawer candidates whose operated ID is missing from the layout table, the reported count is explicitly marked as a lower bound from observed action IDs.
3. `mvp_whitelist` means only that action and target type are in the Gate 1 whitelist. Visibility, exact same-type counts, state change, and replay success remain Gate 2 simulator checks.
4. Initial open state is derived from preceding low-level actions, or from the first successful OpenObject/CloseObject action precondition when the object has not appeared earlier.

## Gate 2: Reproducible 20-event branch replay

Status: PASS

Decision: GO to Gate 3 when simulator resources are available.

Selection and generation:

- Source events: 20 from 20 trajectories and 14 FloorPlans
- Splits: 10 valid_unseen, 5 valid_seen, 5 train
- Actions: 10 OpenObject, 10 CloseObject
- Object types: 4 each Cabinet, Drawer, Fridge, Microwave, and Safe
- Branches: 20 each Fresh-Stable, Risk-Stable, and Risk-Stale
- Outputs: 60 public episodes, 60 private oracle records, and 120 independent replay records
- Checkpoints: one atomic context plus two atomic branch-round records per branch and source event

Commands:

```bash
/224010104/miniconda3/envs/trust3d-sim/bin/python -m pytest -q

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.build_branches \
  --candidates outputs/gate1/candidates.jsonl \
  --limit 20 \
  --branches fresh_stable risk_stable risk_stale \
  --seed 17 \
  --replay-runs 2 \
  --output data/episodes/pilot

/224010104/miniconda3/envs/trust3d-sim/bin/python \
  -m trust3d.data.validate_dataset \
  --public data/episodes/pilot/episodes_public.jsonl \
  --private data/episodes/pilot/oracle_private.jsonl \
  --replay-twice \
  --report outputs/gate2/validation.json
```

Acceptance results on 2026-07-28:

- Unit tests: 20 passed
- Complete source events: 20/20; required minimum: 19/20
- Replay state-hash consistency: 60/60 episodes (100%); required minimum: 95%
- Query pose consistency: all three branches agree in every group
- Answer relations: all Fresh/Risk-Stable answers unchanged and all Risk-Stale answers changed
- Symbolic answer versus simulator metadata: 60/60
- Reachable verification pose: 60/60 (100%); required minimum: 90%
- Query target hidden: 60/60
- Public/private leaks: 0; public/private episode IDs match one-to-one
- Manifest artifact hashes: all match
- Validation result: `gate2_pass=true`

Reproducibility and safety checks:

1. Two contexts initially failed because AI2-THOR rejected `TeleportFull` while the Agent held an object. Query teleport now uses `forceAction=True`; resume rebuilt only the two failed units and retained every validated checkpoint.
2. One Risk-Stable/Risk-Stale query-frame pair had different raw hashes. Direct pixel audit found only 6 of 90,000 pixels changed (0.0067%), each by exactly one intensity level; the target is hidden and all public metadata is identical. This is quantization-level renderer drift, not an observable intervention cue.
3. A checkpoint-only full resume finished without starting Unity or Xvfb. SHA256 comparison of all 247 local pilot files before and after resume was byte-identical.
4. Existing VLLM jobs occupied both GPUs during the final audit. No GPU process was stopped or modified, and the checkpoint-only validation used CPU resources only.

Tracked report:

- `outputs/gate2/validation.json`: `659a39e13dac4a477b2f55bd72a6be7bfe138786c4985ba82efb2cccf5fe0c83`

Local-only artifacts and logs:

- Dataset and atomic checkpoints: `data/episodes/pilot/`
- Resume hash audit: `/224010104/Jerry/logs/gate2/checkpoint-resume.log`
- Final tests: `/224010104/Jerry/logs/gate2/tests-final.log`
- Final validation: `/224010104/Jerry/logs/gate2/validation-final.log`
