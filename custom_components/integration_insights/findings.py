"""Findings: what the measurements mean, for someone who has to act on them.

The rules only read numbers the collector and the memory snapshots already
report. Thresholds are high on purpose: a false alarm costs a user more time
than a borderline case that is not mentioned.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
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
# Shorter windows move too much to judge.
MIN_MINUTES = 5
# Lost samples are expected now and then, a steady share skews the numbers.
LOST_SHARE = 0.01
# Resources an owner may gain between two snapshots before it looks like a leak.
RESOURCE_GROWTH = 20
RESOURCES = ("listeners", "dispatcher", "timers", "tasks")
# Built-in integrations run reviewed background work by design, such as the
# recorder writing to its database, so only a lot of it is worth a word.
BUILT_IN_BACKGROUND_FACTOR = 5
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


def find(
    summary: dict[str, Any],
    detail: Callable[[str], dict[str, Any] | None],
    memory: dict[str, Any],
    built_in: Callable[[str], bool] = lambda _domain: False,
) -> list[Finding]:
    """Return the findings, the most severe first."""
    findings: list[Finding] = []
    if summary["window_minutes"] < MIN_MINUTES:
        findings.append(
            Finding(
                "measuring",
                INFO,
                values={"minutes": _number(summary["window_minutes"])},
            )
        )
    sampler = summary["sampler"]
    if (
        sampler["handler_errors"]
        or sampler["samples_lost"] > LOST_SHARE * sampler["samples_total"]
    ):
        findings.append(
            Finding(
                "measuring_incomplete",
                INFO,
                evidence=[
                    f"errors {sampler['handler_errors']}, lost {sampler['samples_lost']} of {sampler['samples_total']}"
                ],
            )
        )
    total_loop = summary["loop_cpu_ms_per_min"]
    for item in summary["integrations"]:
        if item["domain"] in NOT_AN_INTEGRATION:
            continue
        factor = BUILT_IN_BACKGROUND_FACTOR if built_in(item["domain"]) else 1
        findings.extend(_cpu_findings(item, total_loop, detail, factor))
    waiting = summary["loop_busy_ms_per_min"] - total_loop
    if waiting >= LOOP_WAIT_NOTICE_MS:
        findings.append(
            Finding(
                "loop_waits",
                PROBLEM if waiting >= LOOP_WAIT_PROBLEM_MS else NOTICE,
                values={"ms": _number(waiting)},
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
) -> list[Finding]:
    domain = item["domain"]
    loop = item["loop_cpu_ms_per_min"]
    background = item["background_cpu_ms_per_min"]
    background_notice = BACKGROUND_NOTICE_MS * factor
    background_problem = BACKGROUND_PROBLEM_MS * factor
    if loop < LOOP_NOTICE_MS and background < background_notice:
        return []
    if (info := detail(domain)) is None:
        return []
    findings = []
    if loop >= LOOP_NOTICE_MS and loop >= LOOP_SHARE * total_loop:
        findings.append(
            Finding(
                "busy_loop",
                PROBLEM if loop >= LOOP_PROBLEM_MS else NOTICE,
                domain,
                {"ms": _number(loop), "share": _number(100 * loop / total_loop)},
                _loop_evidence(info),
            )
        )
    threads = info.get("threads", {})
    if (thread_ms := threads.get("cpu_ms_per_min", 0)) >= background_notice:
        findings.append(
            Finding(
                "busy_thread",
                PROBLEM if thread_ms >= background_problem else NOTICE,
                domain,
                {"ms": _number(thread_ms)},
                [
                    f"thread {name}: {_number(ms)} ms/min"
                    for name, ms in list(threads["by_name"].items())[:EVIDENCE_LINES]
                ],
            )
        )
    executor = info.get("executor", {})
    executor_ms = executor.get("cpu_ms_per_min", 0)
    jobs = executor.get("jobs_per_min", 0)
    if executor_ms >= background_notice or jobs >= JOBS_NOTICE_PER_MIN * factor:
        findings.append(
            Finding(
                "busy_executor",
                PROBLEM if executor_ms >= background_problem else NOTICE,
                domain,
                {"ms": _number(executor_ms), "jobs": _number(jobs)},
                [
                    f"{job['function']}: {_number(job['per_min'])}/min, {_number(job['cpu_ms_per_min'])} ms/min"
                    for job in executor["jobs"][:EVIDENCE_LINES]
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
                        "what": ", ".join(
                            f"{count} {name}" for name, count in grown.items()
                        )
                    },
                    [
                        f"{name}: {row[name]} now, +{count} since {latest['previous_taken']}"
                        for name, count in grown.items()
                    ],
                )
            )
    return findings


def _number(value: float) -> str:
    return f"{value:,.0f}"
