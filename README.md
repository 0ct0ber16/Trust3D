# Trust3D

Trust3D studies when a situated agent should trust persistent evidence and when it should reobserve the environment. Implementation follows the gates in `Trust3D_服务器最小可执行实验方案.md`; a gate must pass before work starts on the next one.

## Gate 0

System prerequisite: `xvfb-run` (Ubuntu packages `xvfb` and `xauth`).

Create the isolated simulator environment:

```bash
conda env create -f environment/trust3d-sim.yml
```

Run unit tests and the simulator smoke test:

```bash
conda run -n trust3d-sim pytest -q
xvfb-run -a conda run -n trust3d-sim python -m trust3d.sim.controller \
  --smoke-test \
  --scene FloorPlan1 \
  --output outputs/gate0
```

The smoke test launches two fresh simulator processes and fails unless their initial object-state hashes match. Generated RGB, depth, segmentation, and full metadata stay local; only the sanitized environment and validation reports are tracked.

## Gate 1

Place the pinned ALFRED checkout at `external/alfred`, then scan its trajectory JSON
without starting AI2-THOR:

```bash
python -m trust3d.data.scan_alfred \
  --alfred-json external/alfred/data/json_2.1.0 \
  --events OpenObject CloseObject \
  --output outputs/gate1/candidates.jsonl \
  --stats outputs/gate1/stats.json

pytest -q tests/test_scan_alfred.py
```

The scanner derives pre-action open state from preceding low-level actions and the
first open/close action's precondition. Scene-level counts come from ALFRED's pinned
`gen/layouts/*-openable.json` files, with movable openables counted from trajectory
object poses. If neither source lists an operated instance, action IDs provide an
explicitly marked lower bound that Gate 2 must verify against simulator metadata.
Official test splits are included in the manifest but skipped because ALFRED
intentionally withholds their action plans.
