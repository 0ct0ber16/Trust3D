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
