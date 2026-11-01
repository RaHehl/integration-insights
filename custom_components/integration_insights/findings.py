"""Findings: what the measurements mean, for someone who has to act on them.

The rules only read numbers the collector and the memory snapshots already
report. Thresholds are high on purpose: a false alarm costs a user more time
than a borderline case that is not mentioned.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import Any

PROBLEM = "problem"
NOTICE = "notice"
INFO = "info"
SEVERITIES = (PROBLEM, NOTICE, INFO)

# Per minute of wall time. One minute of loop CPU is the whole loop, and other
# integrations wait while one runs, so a share of it is felt everywhere.
LOOP_NOTICE_MS = 1_000
LOOP_PROBLEM_MS = 6_000
# A share of all loop CPU, so a busy system does not flag its biggest user alone.
LOOP_SHARE = 0.2
# Threads and executor jobs run beside the loop: they cost CPU and power, and
# hold the GIL in turns with it, but block nothing on their own.
BACKGROUND_NOTICE_MS = 300
BACKGROUND_PROBLEM_MS = 6_000
# Ten jobs a second is polling, not reacting.
JOBS_NOTICE_PER_MIN = 600
# Wall time on the loop beyond its CPU time: blocking calls or waits for the GIL.
LOOP_WAIT_NOTICE_MS = 3_000
LOOP_WAIT_PROBLEM_MS = 12_000
# The loop stood still this long at a stretch while the integration's code ran.
LOOP_BLOCK_NOTICE_MS = 2_000
LOOP_BLOCK_PROBLEM_MS = 10_000
# State changes per minute, attribute-only ones included. Each one reaches the
# recorder, the frontend and every listener of the entity. Ten a second is a
# device reporting, not changing. Writes that change nothing are cheap and do
# not count.
STATE_WRITES_NOTICE_PER_MIN = 600
STATE_WRITES_PROBLEM_PER_MIN = 3_000
# Attributes that changed, in KiB per minute. Each change is serialized for
# the recorder and sent to open dashboards. The recorder skips attributes over
# 16 KiB and logs a warning for each instead.
ATTRIBUTES_NOTICE_KIB = 100
ATTRIBUTES_PROBLEM_KIB = 1_000
# Warnings and errors per minute. Each one is formatted and written to the
# log, and a steady stream hides the messages that matter.
LOG_NOTICE_PER_MIN = 10
LOG_PROBLEM_PER_MIN = 100
# Debug lines per minute: debug logging that was left on. Info lines do not
# count: the root level is WARNING, so they only appear where a level was set
# on purpose.
DEBUG_NOTICE_PER_MIN = 300
# Files written through the storage helper per minute. Each write replaces a
# whole file, which wears out the SD card of a Raspberry Pi.
STORE_NOTICE_PER_MIN = 6
STORE_PROBLEM_PER_MIN = 60
# Shorter windows move too much to judge.
MIN_MINUTES = 5
# Lost samples are expected now and then, a steady share skews the numbers.
LOST_SHARE = 0.01
# Resources an owner may gain between two snapshots before it looks like a leak.
RESOURCE_GROWTH = 20
RESOURCES = ("listeners", "dispatcher", "timers", "tasks")
RESOURCE_NAMES = {"dispatcher": "dispatcher connections"}
# Built-in integrations run reviewed background work by design, such as the
# recorder writing to its database, so only a lot of it is worth a word.
BUILT_IN_BACKGROUND_FACTOR = 5
# Built-in integrations that run what a user configured: their states,
# attributes and log messages come from that configuration.
USER_CONFIGURED = frozenset(
    {"automation", "command_line", "rest", "scrape", "script", "sql", "template"}
)
# A finding that is shown stays until its number falls this far below the
# threshold, so a value close to it does not make the finding come and go.
# The same goes for its severity.
HOLD = 0.8
# Home Assistant itself, nothing a user can update or turn off, and this
# integration, whose own cost the panel shows as measuring cost.
NOT_AN_INTEGRATION = frozenset({"core", "python", "shared", "integration_insights"})
EVIDENCE_LINES = 3


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth a user's attention, with what backs it up."""

    key: str
    severity: str
    domain: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)


type Shown = Mapping[tuple[str, str | None], str]


class _Hold:
    """Thresholds, lowered for what the last call already showed."""

    def __init__(self, shown: Shown) -> None:
        self._shown = shown

    def __call__(self, key: str, domain: str | None = None) -> float:
        """Return the factor for the threshold of a finding."""
        return HOLD if (key, domain) in self._shown else 1

    def severity(
        self, key: str, domain: str | None, value: float, problem: float
    ) -> str:
        """Return the severity of a finding, holding a problem a little longer."""
        factor = HOLD if self._shown.get((key, domain)) == PROBLEM else 1
        return PROBLEM if value >= problem * factor else NOTICE


def find(
    summary: dict[str, Any],
    detail: Callable[[str], dict[str, Any] | None],
    memory: dict[str, Any],
    built_in: Callable[[str], bool] = lambda _domain: False,
    shown: Shown = {},  # noqa: B006
) -> list[Finding]:
    """Return the findings, the most severe first.

    `shown` maps (key, domain) of the findings of the last call to their
    severity. They only end, or drop in severity, below HOLD times the
    threshold.
    """
    hold = _Hold(shown)
    if summary["window_minutes"] < MIN_MINUTES:
        # Too short to judge, and nothing else would be true to say yet
        left = math.ceil(MIN_MINUTES - summary["window_minutes"])
        return [Finding("measuring", INFO, values={"minutes": str(left)})]
    findings: list[Finding] = []
    minutes = _number(summary["window_minutes"])
    lost = summary["samples_lost"]
    errors = summary["handler_errors"]
    if errors or lost > LOST_SHARE * summary["samples"]:
        findings.append(
            Finding(
                "measuring_incomplete",
                INFO,
                values={"minutes": minutes},
                evidence=[f"errors {errors}, lost {lost} of {summary['samples']}"],
            )
        )
    total_loop = summary["loop_cpu_ms_per_min"]
    for item in summary["integrations"]:
        if item["domain"] in NOT_AN_INTEGRATION:
            continue
        factor = BUILT_IN_BACKGROUND_FACTOR if built_in(item["domain"]) else 1
        findings.extend(_cpu_findings(item, total_loop, detail, factor, hold, minutes))
    waiting = summary["loop_busy_ms_per_min"] - total_loop
    if waiting >= LOOP_WAIT_NOTICE_MS * hold("loop_waits"):
        findings.append(
            Finding(
                "loop_waits",
                hold.severity("loop_waits", None, waiting, LOOP_WAIT_PROBLEM_MS),
                values={"ms": _number(waiting), "minutes": minutes},
            )
        )
    findings.extend(_memory_findings(memory))
    findings.sort(key=lambda finding: SEVERITIES.index(finding.severity))
    return findings


def verdict(findings: list[Finding]) -> str:
    """Return the most severe level among the findings, or "ok"."""
    for severity in (PROBLEM, NOTICE):
        if any(finding.severity == severity for finding in findings):
            return severity
    return "ok"


def _cpu_findings(
    item: dict[str, Any],
    total_loop: float,
    detail: Callable[[str], dict[str, Any] | None],
    factor: int,
    hold: _Hold,
    minutes: str,
) -> list[Finding]:
    domain = item["domain"]
    output_factor = 1 if domain in USER_CONFIGURED else factor
    loop = item["loop_cpu_ms_per_min"]
    background = item["background_cpu_ms_per_min"]
    background_notice = BACKGROUND_NOTICE_MS * factor
    background_problem = BACKGROUND_PROBLEM_MS * factor
    blocked = item.get("blocked_ms", 0)
    writes = item.get("state_changes_per_min", 0) + item.get(
        "state_attributes_only_per_min", 0
    )
    attribute_kib = item.get("attribute_kib_per_min", 0)
    # The recorder's warnings about attributes too large to store count
    # for the entity's integration, but it did not log them
    log_warnings = max(
        item.get("log_warnings_per_min", 0)
        + item.get("log_errors_per_min", 0)
        - item.get("log_oversized_per_min", 0),
        0,
    )
    log_debug = item.get("log_debug_per_min", 0)
    store_writes = item.get("store_writes_per_min", 0)
    if (
        loop < LOOP_NOTICE_MS * HOLD
        and background < background_notice * HOLD
        and item.get("jobs_per_min", 0) < JOBS_NOTICE_PER_MIN * factor * HOLD
        and blocked < LOOP_BLOCK_NOTICE_MS * HOLD
        and writes < STATE_WRITES_NOTICE_PER_MIN * output_factor * HOLD
        and attribute_kib < ATTRIBUTES_NOTICE_KIB * output_factor * HOLD
        and log_warnings < LOG_NOTICE_PER_MIN * HOLD
        and log_debug < DEBUG_NOTICE_PER_MIN * HOLD
        and store_writes < STORE_NOTICE_PER_MIN * output_factor * HOLD
    ):
        return []
    if (info := detail(domain)) is None:
        return []
    findings = []
    if writes >= STATE_WRITES_NOTICE_PER_MIN * output_factor * hold(
        "busy_states", domain
    ):
        states = info.get("state_writes", {})
        findings.append(
            Finding(
                "busy_states",
                hold.severity(
                    "busy_states",
                    domain,
                    writes,
                    STATE_WRITES_PROBLEM_PER_MIN * output_factor,
                ),
                domain,
                {
                    "per_min": _number(writes),
                    "attributes_only": _number(
                        item.get("state_attributes_only_per_min", 0)
                    ),
                    "minutes": minutes,
                },
                [
                    f"{entity_id}: {_rate(per_min)}/min"
                    for entity_id, per_min in list(states.get("entities", {}).items())[
                        :EVIDENCE_LINES
                    ]
                ],
            )
        )
    findings.extend(
        _output_findings(
            item,
            info,
            output_factor,
            hold,
            minutes,
            attribute_kib,
            log_warnings,
            log_debug,
        )
    )
    if blocked >= LOOP_BLOCK_NOTICE_MS * hold("loop_blocked", domain):
        findings.append(
            Finding(
                "loop_blocked",
                hold.severity("loop_blocked", domain, blocked, LOOP_BLOCK_PROBLEM_MS),
                domain,
                {
                    "ms": _number(blocked),
                    "function": item.get("blocked_function", ""),
                    "minutes": minutes,
                },
                [
                    f"{item.get('blocked_function', '')}: {_number(blocked)} ms,"
                    f" ended {_utc(item.get('blocked_at', 0))}"
                ],
            )
        )
    loop_hold = hold("busy_loop", domain)
    if (
        loop >= LOOP_NOTICE_MS * loop_hold
        and loop >= LOOP_SHARE * loop_hold * total_loop
    ):
        findings.append(
            Finding(
                "busy_loop",
                hold.severity("busy_loop", domain, loop, LOOP_PROBLEM_MS),
                domain,
                {
                    "ms": _number(loop),
                    "share": _number(100 * loop / total_loop),
                    "minutes": minutes,
                },
                _loop_evidence(info),
            )
        )
    threads = info.get("threads", {})
    thread_ms = threads.get("cpu_ms_per_min", 0)
    if thread_ms >= background_notice * hold("busy_thread", domain):
        findings.append(
            Finding(
                "busy_thread",
                hold.severity("busy_thread", domain, thread_ms, background_problem),
                domain,
                {"ms": _number(thread_ms), "minutes": minutes},
                [
                    f"thread {name}: {_number(ms)} ms/min"
                    for name, ms in list(threads["by_name"].items())[:EVIDENCE_LINES]
                ],
            )
        )
    executor = info.get("executor", {})
    executor_ms = executor.get("cpu_ms_per_min", 0)
    jobs = executor.get("jobs_per_min", 0)
    # Heavy work and many small jobs are different stories; one of them.
    if executor_ms >= background_notice * hold("heavy_executor", domain):
        key = "heavy_executor"
        severity = hold.severity(key, domain, executor_ms, background_problem)
    elif jobs >= JOBS_NOTICE_PER_MIN * factor * hold("busy_executor", domain):
        key = "busy_executor"
        severity = NOTICE
    else:
        return findings
    findings.append(
        Finding(
            key,
            severity,
            domain,
            {"ms": _number(executor_ms), "jobs": _number(jobs), "minutes": minutes},
            [
                f"{job['function']}: {_number(job['per_min'])}/min, {_number(job['cpu_ms_per_min'])} ms/min"
                for job in executor["jobs"][:EVIDENCE_LINES]
            ],
        )
    )
    return findings


def _output_findings(
    item: dict[str, Any],
    info: dict[str, Any],
    factor: int,
    hold: _Hold,
    minutes: str,
    attribute_kib: float,
    log_warnings: float,
    log_debug: float,
) -> list[Finding]:
    """Findings about what an integration writes: attributes, logs, files."""
    domain = item["domain"]
    findings = []
    if attribute_kib >= ATTRIBUTES_NOTICE_KIB * factor * hold("big_attributes", domain):
        entities = info.get("state_writes", {}).get("attribute_entities", {})
        findings.append(
            Finding(
                "big_attributes",
                hold.severity(
                    "big_attributes",
                    domain,
                    attribute_kib,
                    ATTRIBUTES_PROBLEM_KIB * factor,
                ),
                domain,
                {"kib": _number(attribute_kib), "minutes": minutes},
                [
                    f"{entity_id}: {_rate(kib)} KiB/min"
                    for entity_id, kib in list(entities.items())[:EVIDENCE_LINES]
                ]
                + (
                    [
                        "recorder warnings about attributes too large to store: "
                        f"{_rate(oversized)}/min"
                    ]
                    if (oversized := item.get("log_oversized_per_min", 0))
                    else []
                ),
            )
        )
    loggers = info.get("logs", {}).get("loggers", {})
    if log_warnings >= LOG_NOTICE_PER_MIN * hold("noisy_logs", domain):
        findings.append(
            Finding(
                "noisy_logs",
                hold.severity("noisy_logs", domain, log_warnings, LOG_PROBLEM_PER_MIN),
                domain,
                {
                    "per_min": _rate(log_warnings),
                    "errors": _rate(item.get("log_errors_per_min", 0)),
                    "minutes": minutes,
                },
                [
                    f"{name}: {_rate(per_min)}/min"
                    for name, per_min in list(loggers.items())[:EVIDENCE_LINES]
                ],
            )
        )
    if log_debug >= DEBUG_NOTICE_PER_MIN * hold("debug_logging", domain):
        findings.append(
            Finding(
                "debug_logging",
                NOTICE,
                domain,
                {"per_min": _number(log_debug), "minutes": minutes},
            )
        )
    store = info.get("storage", {})
    store_writes = item.get("store_writes_per_min", 0)
    if store_writes >= STORE_NOTICE_PER_MIN * factor * hold("busy_storage", domain):
        findings.append(
            Finding(
                "busy_storage",
                hold.severity(
                    "busy_storage", domain, store_writes, STORE_PROBLEM_PER_MIN * factor
                ),
                domain,
                {
                    "per_min": _rate(store_writes),
                    "kib": _rate(item.get("store_kib_per_min", 0)),
                    "minutes": minutes,
                },
                [
                    f"{key}: {_rate(per_min)}/min"
                    for key, per_min in list(store.get("files", {}).items())[
                        :EVIDENCE_LINES
                    ]
                ],
            )
        )
    return findings


def _loop_evidence(info: dict[str, Any]) -> list[str]:
    lines = []
    for spot in info.get("hot_spots", [])[:EVIDENCE_LINES]:
        hottest = max(spot["lines"].items(), key=lambda item: item[1], default=None)
        where = f" (line {hottest[0]})" if hottest else ""
        lines.append(
            f"{spot['function']}{where}: {_number(spot['loop_cpu_ms_per_min'])} ms/min"
        )
    lines.extend(
        f"triggered by {trigger}: {_number(ms)} ms/min"
        for trigger, ms in list(info.get("triggered_by", {}).items())[:EVIDENCE_LINES]
    )
    return lines


def _memory_findings(memory: dict[str, Any]) -> list[Finding]:
    latest = memory.get("latest")
    if latest is None or "previous_taken" not in latest:
        return []
    findings = []
    for row in latest["owners"]:
        if row["owner"] in NOT_AN_INTEGRATION or row["group"] != "integration":
            continue
        change = row.get("change", {})
        grown = {
            name: change[name]
            for name in RESOURCES
            if change.get(name, 0) >= RESOURCE_GROWTH
        }
        if grown:
            findings.append(
                Finding(
                    "growing_resources",
                    NOTICE,
                    row["owner"],
                    {
                        "what": _listed(
                            [
                                f"{count} {RESOURCE_NAMES.get(name, name)}"
                                for name, count in grown.items()
                            ]
                        )
                    },
                    [
                        f"{name}: {row[name]} now, +{count} since {latest['previous_taken']}"
                        for name, count in grown.items()
                    ],
                )
            )
    return findings


def _listed(items: list[str]) -> str:
    """Join items as a sentence does: "a, b and c"."""
    return " and ".join([", ".join(items[:-1]), items[-1]] if len(items) > 1 else items)


def _number(value: float) -> str:
    return f"{value:,.0f}"


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _rate(value: float) -> str:
    """Format a rate; small ones keep a decimal so 2 in 6 minutes is not 0."""
    if value == 0:
        return "0"
    if 0 < value < 0.1:
        return "<0.1"
    return f"{value:.1f}" if value < 10 else _number(value)
