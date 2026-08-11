---
title: Check ECN-relative time
sidebar:
  order: 10
---

Use the optional clock diagnostic to measure the configured ECN's NTP service from
the machine running the SDK. A positive offset means ECN time is ahead of local
time; a negative offset means it is behind.

## Check a saved profile

The CLI exits successfully only when the absolute selected offset is within the
chosen tolerance:

```bash
picogrid-ecn clock check --profile NAME --max-offset 1.0
```

Exit status `0` means the measurement is within tolerance. Status `3` means a valid
measurement exceeded tolerance; status `2` means the command input, configuration,
or diagnostic operation failed.

The configured ECN host is also the NTP host by default, on UDP port 123. Set an
alternate `ntp_host` or `ECN_NTP_HOST` only when Picogrid provided a different
endpoint. `ntp_port` and `ECN_NTP_PORT` provide the corresponding bounded port
override. Use the CLI's `--samples` and `--timeout` flags to change its measurement
bounds.

## Use the typed client

Clock measurement is independent of the application-data connection and works
before `client.start()`:

```python
report = await client.clock.measure(samples=3, timeout=5)
print(report.offset_seconds)  # ECN time minus local time, in seconds

report = await client.clock.require_within(
    max_offset_seconds=1.0,
    samples=3,
    timeout=5,
)
```

Each report identifies the configured endpoint and includes the selected offset,
round-trip delay, conservative local timing uncertainty, offset jitter and spread,
sample counts, server stratum and leap state, and measurement time. The sample with
the lowest round-trip delay is selected. Treat `samples` as a target. Transport-level
timeouts and endpoint errors retry sequentially while the ten-attempt budget and
overall deadline remain. If the attempt cap is reached after a valid response, compare
`samples_completed` with `samples_requested` to identify the preserved usable subset.
Zero valid responses, the overall deadline, and protocol-invalid responses remain
errors. Delay uses the local monotonic interval
minus the server receive-to-transmit interval. One report measures offset at one
point in time; it is not a drift estimate. `require_within` conservatively includes
the bound on local offset error attributable to paired-read capture timing and
tolerated within-sample wall/monotonic divergence in its tolerance decision.

Run the repository [`check_clock.py`](../../examples/check_clock.py) example against
the installed wheel with a profile or `ECN_PROFILE`. For that example only,
`ECN_CLOCK_SAMPLES` requests a target of 1-10 samples, `ECN_CLOCK_TIMEOUT_SECONDS`
bounds the complete measurement, and `ECN_CLOCK_MAX_OFFSET_SECONDS` enables the
tolerance check:

```bash
python examples/check_clock.py --check
python examples/check_clock.py --profile NAME
ECN_CLOCK_MAX_OFFSET_SECONDS=1.0 python examples/check_clock.py --profile NAME
```

`--check` is an offline example self-check. It requires no credentials or network
access and does not measure an ECN clock, so `ECN_CLOCK_MAX_OFFSET_SECONDS` does not
apply in that mode.

## Diagnostic boundary

This feature sends only valid client-mode NTPv4 requests to the configured ECN NTP
service. It does not start MQTT, publish application data, change either clock,
rewrite event timestamps, or supply an automatically corrected time. Its result is
operational timing evidence only—not evidence of authentication, certificate or
token validity, authorization, or downstream data persistence.
