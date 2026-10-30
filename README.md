<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/dark_logo.svg">
  <img src="docs/brand/logo.svg" alt="Integration Insights" width="420">
</picture>

# Integration Insights

Finds the integration that slows Home Assistant down, keeps the CPU busy, or leaks memory, and tells you what to do about it.

![Overview with three findings, from three example integrations written for this screenshot](docs/overview.png)

## What it finds

- An integration that keeps the event loop busy. While it runs, automations, the dashboard and every other integration wait.
- Background threads that work around the clock, such as a file watcher that keeps polling.
- Integrations that hand a lot of work to the executor, usually polling more often than needed.
- An event loop that often waits, usually on a blocking call.
- Listeners, timers and tasks that pile up between two memory snapshots, a sign of a leak.

Each finding says what it means and what you can do: check for an update, look at the integration's issue tracker, or disable it for a while to compare. One button copies a report to paste into an issue.

Findings start once Home Assistant has settled, about ten minutes after a start. The thresholds are high on purpose: when the overview says nothing is unusual, there is nothing worth your time.

## Install

1. In HACS, add `https://github.com/RaHehl/integration-insights` as a custom repository of type Integration, and download Integration Insights.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > Integration Insights.

A sidebar entry "Integration Insights" appears for admins.

Needs Home Assistant OS or the container image on 64-bit Linux.

## For developers

The CPU view lists every integration with its time on the event loop and in the background, for the last 5, 15 or 60 minutes. Each one opens to:

- hot spots: functions and lines, with ms per minute
- the stacks behind them, innermost first
- what triggered the work (events, timers, service calls), config entries, devices and entities
- executor jobs with their callables, and threads by name, including what a pool thread runs

"Copy as text" gives all of it in one block for an issue or a profiler session. The diagnostics download has the same numbers as JSON.

The Memory view walks the Python heap on request and splits it by integration, library and Home Assistant itself, with listeners, dispatcher connections, timers and tasks per integration. A second snapshot shows what changed.

### How it measures

- A timer on the event loop thread's CPU clock samples the stack while the loop is busy, about every 10 ms of loop CPU. When the loop is idle, it does not fire.
- A sample goes to the integration whose code runs. Code in an entity platform base class goes to the integration that owns the entity. A library that only one loaded integration requires goes to that integration.
- Executor jobs are timed exactly with the worker thread's CPU clock. A job goes to the integration whose callable it runs, else to the one that submitted it. Other threads are read from `/proc` every 10 seconds. A pool thread is charged to what it runs, and for anyio to the task waiting for it.

### Cost

The panel shows its own cost under "Measuring costs". On a Raspberry Pi 3 the whole integration uses about 300 ms of CPU per minute once Home Assistant has started, half a percent of one core, less on faster hardware. During start-up it is higher while it maps libraries to integrations. A memory snapshot takes about half a minute on a Raspberry Pi 3 and needs memory for the walk. It is refused with a reason when memory is short.

## Privacy

Reports contain code locations (file, function, line), integration domains and the IDs of config entries, devices and entities. Entity IDs can say something about your home, such as `light.bedroom`, so look before you post one. Reports contain no arguments, values, entity states or local paths. Names of entries, devices and entities only appear in the panel and in "Copy as text", not in the diagnostics download.

## Limits

- The numbers are statistical. Integrations with few samples are marked.
- Linux checks CPU timers on the scheduler tick, so very short callbacks can be undercounted.
- Time in C or Rust code is charged to the Python line that called it.
- During a long block of the event loop the sampler slows down, so the last part of such a block can land on the code that runs next.
- A blocking call uses no CPU, so the CPU view cannot say which integration made the event loop wait. Home Assistant's log names it ("Detected blocking call").
- A memory snapshot charges an object that several integrations hold to the first one the walk reaches. Shared strings and bytes go to a shared pool.

## License

Apache-2.0
