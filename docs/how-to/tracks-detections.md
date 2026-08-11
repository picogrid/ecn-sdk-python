---
title: Watch tracks and detections
tableOfContents: false
sidebar:
  hidden: true
---

Use this workflow to receive track or detection events from an integration you are
authorized to observe. After completing the
[observer quickstart](../quickstarts/observe-data.md), choose the runnable
[track watcher](../../examples/watch_tracks.py) or
[detection watcher](../../examples/watch_detections.py). Both open a lazy bounded
stream and close it in `finally`.

## Run the watchers

```bash
python examples/watch_tracks.py --check
export ECN_OBSERVED_INTEGRATION=authorized-source
export ECN_MAX_EVENTS=10
python examples/watch_tracks.py
```

```bash
python examples/watch_detections.py --check
export ECN_OBSERVED_INTEGRATION=authorized-source
export ECN_MAX_EVENTS=10
python examples/watch_detections.py
```

## Choose filters and delivery

Category filters are fixed in each script. Add the exact integration whenever it is
known, keep event counts bounded for validation, and use `DeliveryPolicy.LATEST` for
rendering consumers that prefer current state over backlog.

For the supported sensor publication pattern, continue with
[Sensor integration](../integrations/sensors.md).
