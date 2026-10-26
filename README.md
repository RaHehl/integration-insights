# Integration Insights

A custom integration for Home Assistant that shows how much CPU time each integration uses, and which integration blocked the event loop.

**Status: early development, not usable yet.**

## How it works

- A timer on the CPU clock of the event loop thread takes stack samples while the loop is busy. Each sample is attributed to the integration whose code is running. When the loop is idle, the timer does not fire.
- CPU time of executor jobs and of threads that integrations start is measured per thread.
- A watchdog notices when the event loop stops responding and records which integration was running.

Linux only (Home Assistant OS, Container, Core on Linux).

## License

Apache-2.0
