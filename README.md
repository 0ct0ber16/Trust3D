# Trust3D

Trust3D studies when a situated agent should trust persistent evidence and when it should reobserve the environment. Implementation follows the gates in `Trust3D_服务器最小可执行实验方案.md`; a gate must pass before work starts on the next one.

## Gate 0

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
