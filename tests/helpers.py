"""Helpers for building code at fake paths."""

from types import CodeType, FunctionType
from typing import Any

SPIN = """\
def spin(seconds):
    import time
    end = time.thread_time() + seconds
    total = 0
    while time.thread_time() < end:
        total += 1
    return total
"""


def compile_at(filename: str, source: str = SPIN) -> dict[str, Any]:
    """Run source as if it lived at filename and return its namespace."""
    namespace: dict[str, Any] = {"__name__": "fake"}
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    return namespace


def spin_at(filename: str) -> FunctionType:
    """Return a CPU spin function whose frames carry filename."""
    return compile_at(filename)["spin"]


def offset_of_line(code: CodeType, line: int) -> int:
    """Return a bytecode offset that belongs to a line."""
    for start, _end, code_line in code.co_lines():
        if code_line == line:
            return start
    raise ValueError(line)


CALL = """\
def call(fn):
    return fn()
"""

HERE = """\
def here(sampler, cpu_ns=0):
    import sys
    return sampler.walk(sys._getframe(), cpu_ns)
"""


def walk_through(sampler: Any, *filenames: str) -> Any:
    """Walk a stack whose frames carry filenames, outermost first.

    The last filename is the innermost frame, which does the walk.
    """
    here = compile_at(filenames[-1], HERE)["here"]

    def chain(index: int) -> Any:
        if index == len(filenames) - 1:
            return here(sampler)
        call = compile_at(filenames[index], CALL)["call"]
        return call(lambda: chain(index + 1))

    return chain(0)
