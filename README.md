# Integration Insights

A custom integration for Home Assistant that shows how much CPU time each integration uses, and which integration blocked the event loop.

**Status: early development, not usable yet.**

## How it works

- A timer on the CPU clock of the event loop thread takes stack samples while the loop is busy. Each sample is attributed to the integration whose code is running. When the loop is idle, the timer does not fire.
- Code in an entity platform base class (for example `light`) is charged to the integration that owns the entity. Code in a library that only one loaded integration requires is charged to that integration.
- The diagnostics download lists, per integration, the loop CPU in ms per minute over the last hour, the hottest code locations and stacks, which integration triggered the work, and the config entries.

The report contains code locations only (file, function, line). It contains no arguments, local variables or local paths.

Planned: CPU of executor jobs and threads, a watchdog for event loop blocks, and a report with code excerpts and hints.

Linux only, 64-bit (Home Assistant OS, Container, Core on Linux).

## Limits

- The numbers are statistical. Integrations with few samples are marked `low_confidence`.
- Linux checks CPU timers on the scheduler tick. Callbacks shorter than about 1 ms are undercounted by about 15 to 20 percent.
- Time in C code is charged to the Python line that called it. For native profiles use py-spy.

## License

Apache-2.0
