"""Account the Python heap to integrations, code and Home Assistant core.

One walk over everything reachable, every object counted once, in this order:

1. Data an integration holds: its config entries' runtime_data, its keys in
   hass.data, its entities and its running tasks.
2. Code: per module, its functions, classes, code objects and globals.
3. Home Assistant core: states, the rest of hass.data, the event bus and hass.
4. Objects the garbage collector tracks that no root reached.

Shared objects go to the first owner that reaches them. Strings and bytes with
more than one reference go to a shared pool, so a key used everywhere does not
land on whoever happened to come first. Sizes are shallow (sys.getsizeof), so
memory outside Python objects (native libraries, allocator) is not seen; the
caller compares the total with the resident size.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import gc
import inspect
import sys
from types import (
    BuiltinFunctionType,
    CodeType,
    FrameType,
    FunctionType,
    MethodDescriptorType,
    ModuleType,
    WrapperDescriptorType,
)
from typing import Any

DATA = "data"
CODE = "code"
SHARED = "shared"
UNREACHED = "unreached"

_LEAVES = (str, bytes, int, float, complex)
# A leaf referenced only by its container, the walk's local and getrefcount.
_OWNED_REFS = 3


_C_IMPLEMENTED = (WrapperDescriptorType, MethodDescriptorType, BuiltinFunctionType)


def _make_sizer() -> Callable[[Any], int]:
    """Return a sizeof that never runs Python code.

    A class with its own __sizeof__ in Python (mocks, proxies) could do
    anything, even create objects while it is measured; such objects get the
    size of their base object instead.
    """
    safe: dict[type, bool] = {}
    getsizeof = sys.getsizeof
    base_sizeof = object.__sizeof__

    def size(obj: Any) -> int:
        cls = type(obj)
        if (ok := safe.get(cls)) is None:
            ok = safe[cls] = isinstance(
                inspect.getattr_static(cls, "__sizeof__", None), _C_IMPLEMENTED
            )
        try:
            return getsizeof(obj) if ok else base_sizeof(obj)
        except Exception:  # noqa: BLE001
            return 0

    return size


@dataclass(frozen=True, slots=True)
class Root:
    """Where the walk starts for one owner."""

    owner: str  # integration domain, "core:<part>" or a library
    category: str  # DATA or CODE
    obj: Any
    module: str | None = None  # for CODE: the module whose definitions count


@dataclass(slots=True)
class Tally:
    """Bytes and objects for one owner and category."""

    size: int = 0
    count: int = 0


type Accounts = dict[tuple[str, str], Tally]


def walk(
    roots: list[Root],
    stoppers: Iterable[Any],
    unreached_owner: Callable[[type], str],
) -> Accounts:
    """Account everything reachable from roots, then what nobody reached."""
    accounts: Accounts = defaultdict(Tally)
    module_dicts = {id(m.__dict__) for m in list(sys.modules.values()) if m is not None}
    seen: set[int] = {id(obj) for obj in stoppers}
    seen.update(id(root.obj) for root in roots)
    shared: set[int] = set()
    shared_tally = accounts[(SHARED, DATA)]
    getsizeof = _make_sizer()
    getrefcount = sys.getrefcount
    get_referents = gc.get_referents

    stack: list[Any] = []
    # The walk can reach its own frame through the thread it runs in; its
    # working structures must not be walked or counted.
    seen.update(id(obj) for obj in (stack, seen, shared, accounts, roots, module_dicts))
    for root in roots:
        tally = accounts[(root.owner, root.category)]
        code_module = root.module
        tally.size += getsizeof(root.obj)
        tally.count += 1
        stack.extend(get_referents(root.obj))
        while stack:
            obj = stack.pop()
            cls = type(obj)
            if cls in _LEAVES:
                if getrefcount(obj) > _OWNED_REFS:
                    if id(obj) not in shared:
                        shared.add(id(obj))
                        shared_tally.size += getsizeof(obj)
                        shared_tally.count += 1
                else:
                    tally.size += getsizeof(obj)
                    tally.count += 1
                continue
            oid = id(obj)
            if oid in seen or oid in module_dicts or cls is ModuleType:
                continue
            # type(obj), never isinstance: a proxy's __class__ is Python code.
            is_class = issubclass(cls, type)
            if code_module is None:
                # Classes and code belong to the module that defines them.
                if is_class or cls is CodeType:
                    continue
            elif (is_class or cls is FunctionType) and obj.__module__ != code_module:
                # Imported names belong to their own module.
                continue
            seen.add(oid)
            tally.size += getsizeof(obj)
            tally.count += 1
            if cls is FrameType:
                # Locals of a suspended coroutine are data; the frame chain is
                # not, and a running frame may be this walk itself.
                if obj.f_back is None:
                    stack.extend(obj.f_locals.values())
                continue
            stack.extend(get_referents(obj))

    for obj in gc.get_objects():
        if id(obj) in seen or issubclass(type(obj), (type, ModuleType, CodeType)):
            continue
        tally = accounts[(unreached_owner(type(obj)), UNREACHED)]
        tally.size += getsizeof(obj)
        tally.count += 1
    return dict(accounts)
