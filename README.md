<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/brand/dark_logo.svg">
  <img src="https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/brand/logo.svg" alt="Integration Insights" width="420">
</picture>

# Integration Insights

Finds the integration that slows Home Assistant down, keeps the CPU busy, or leaks memory, and tells you what to do about it.

![Overview with findings, from example integrations written for these screenshots](https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/overview.png)

## What it finds

- An integration that keeps the event loop busy. While it runs, automations, the dashboard and every other integration wait.
- Background threads that work around the clock, such as a file watcher that keeps polling.
- Integrations that hand a lot of work to the executor, usually polling more often than needed.
- The event loop standing still for seconds, named by the integration and function that ran at that moment, the first time it happens.
- An integration that changes the state or attributes of its entities many times a second. Each change is recorded and reaches the dashboard and every automation that watches the entity.
- Entities whose attributes change by a lot of data. The recorder serializes them again for every change, and open dashboards receive what changed. Above 16 KiB the recorder does not store them and logs a warning for every change instead.
- An integration that logs many warnings or errors, or has debug logging left on.
- An integration that saves its files every few seconds, which wears out the SD card of a Raspberry Pi.
- An event loop that often waits, usually on a blocking call.
- Listeners, dispatcher connections, timers and tasks that pile up between two memory snapshots, a sign of a leak.

Each finding says what it means and what you can do: check for an update, look at the integration's issue tracker, or disable it for a while to compare. One button copies a report to paste into an issue.

Measuring begins once Home Assistant has started, so the set-up of integrations is not counted as load. Findings start about ten minutes later. The thresholds are high on purpose: when the overview says nothing is unusual, there is nothing worth your time.

## Install

1. Search for Integration Insights in HACS and download it. Until it is listed there, add `https://github.com/RaHehl/integration-insights` as a custom repository of type Integration first.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > Integration Insights.

A sidebar entry "Integration Insights" appears for admins.

Needs Home Assistant on 64-bit Linux, such as Home Assistant OS or the container image.

## Views

The overview lists the findings. The other views show the numbers behind them.

| CPU | Memory | States |
|---|---|---|
| ![CPU view with one integration opened](https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/cpu.png) | ![Memory view after a snapshot](https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/memory.png) | ![State writes per integration](https://raw.githubusercontent.com/RaHehl/integration-insights/main/docs/states.png) |

## For developers

The CPU view lists every integration with its time on the event loop and in the background, for the last 5, 15 or 60 minutes. Each one opens to:

- hot spots: functions and lines, with ms per minute
- the stacks behind them, innermost first
- what triggered the work (events, timers, service calls), config entries, devices and entities
- executor jobs with their callables, and threads by name, including what a pool thread runs

"Copy as text" gives all of it in one block for an issue or a profiler session. The diagnostics download has the same numbers as JSON.

The Logs view lists warnings, errors and debug lines per integration, with the loggers behind them, and the files each integration saves through Home Assistant's storage helper.

The Memory view walks the Python heap on request and splits it by integration, library and Home Assistant itself, with listeners, dispatcher connections, timers and tasks per integration. A second snapshot shows what changed.

### How it measures

- Every callback on the event loop is timed exactly, by wall clock, and charged to the module of its function or class, else to the integration whose config entry context it runs in. This sees time spent waiting, which the sampler cannot. The same code is proposed for Home Assistant's profiler integration.
- When a callback runs for a second, a watchdog thread looks once at the stack of the event loop thread. The finding names the code running at that moment, not the callback that started it.
- A timer on the event loop thread's CPU clock samples the stack while the loop is busy, about every 10 ms of loop CPU. When the loop is idle, it does not fire. This is what names the lines inside a callback.
- A sample goes to the integration whose code runs. Code in an entity platform base class goes to the integration that owns the entity. A library that only one loaded integration requires goes to that integration. Core and shared library code with no integration on the stack, such as aiohttp parsing a response, goes to the config entry whose context it runs in, which Home Assistant sets while an entry is set up and every task and connection started from there inherits.
- A finalizer running inside the garbage collector is charged to the owner of the object being destroyed, not to whoever allocated enough to trigger the collection.
- State writes are counted per integration in the bus event filters, at the cost of one dictionary update per write. A write is a change, a change of attributes only, or a repeat that changes nothing. Repeats are cheap: Home Assistant only moves the last reported time and fires `state_reported`.
- The size of attributes is measured only when they change, as the recorder serializes them: attributes an integration leaves out of the history and entities the recorder excludes do not count. Home Assistant keeps the old attributes object when nothing in it changed, so unchanged writes cost nothing. The measuring runs in a callback of its own, so it counts as measuring cost and not as the integration's.
- Log records are counted by a handler on the root logger, by logger and level. They are not formatted or written a second time. The recorder's warning about attributes too large to store counts for the integration of the entity.
- Files saved through Home Assistant's storage helper are counted with their size, by storage key.
- Executor jobs are timed exactly with the worker thread's CPU clock. A job goes to the integration whose callable it runs, else to the one that submitted it. Other threads are read from `/proc` every 10 seconds. A pool thread is charged to what it runs, and for anyio to the task waiting for it.

### Cost

The panel shows its own cost under "Measuring costs": sampling, timing the callbacks, and its own work. The cost grows with the number of callbacks on the event loop. On a Raspberry Pi 3 with 250 callbacks a second it is about 2 s of CPU per minute, 3 % of one core, and a fraction of that on faster hardware. A memory snapshot takes about half a minute on a Raspberry Pi 3 and needs memory for the walk. It is refused with a reason when memory is short.

## Privacy

Reports contain code locations (file, function, line), integration domains, thread names, logger names, storage keys and the IDs of config entries, devices and entities, including the entities that write state most. Entity IDs can say something about your home, such as `light.bedroom`. Thread names are chosen by integrations and libraries and can contain a host name or address. Look before you post a report. Reports contain no arguments, values, entity states or local paths. Names of entries, devices and entities only appear in the panel and in "Copy as text", not in the diagnostics download.

Nothing leaves Home Assistant. The integration sends no data anywhere.

## Limits

- The numbers are statistical. Integrations with few samples are marked.
- Linux checks CPU timers on the scheduler tick, so very short callbacks can be undercounted.
- Time in C or Rust code is charged to the Python line that called it.
- During a long block of the event loop the sampler slows down, so the last part of such a block can land on the code that runs next.
- A blocking call uses no CPU, so the sampler cannot point at the line. The watchdog names the function that ran when the loop had stood still for a second, and Home Assistant's log may name the call ("Detected blocking call").
- A memory snapshot charges an object that several integrations hold to the first one the walk reaches. Shared strings and bytes go to a shared pool.

## License

Apache-2.0
