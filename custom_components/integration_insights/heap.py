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
import time
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

# What the walk does with an object, decided once per type.
_OTHER, _LEAF, _CLASS, _CODE, _MODULE, _FRAME, _FUNCTION = range(7)


def _kind(cls: type) -> tuple[int, bool]:
    """Return what kind of object a type makes, and whether sizeof is safe.

    Only type(obj) is used, never isinstance: a proxy's __class__ is Python
    code. A class with its own __sizeof__ in Python (mocks, proxies) could do
    anything, even create objects while it is measured; such objects get the
    size of their base object instead.
    """
    safe = isinstance(inspect.getattr_static(cls, "__sizeof__", None), _C_IMPLEMENTED)
    if cls in _LEAVES:
        return _LEAF, safe
    if issubclass(cls, type):
        return _CLASS, safe
    if cls is CodeType:
        return _CODE, safe
    if cls is ModuleType:
        return _MODULE, safe
    if cls is FrameType:
        return _FRAME, safe
    if cls is FunctionType:
        return _FUNCTION, safe
    return _OTHER, safe


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

# The walk runs in an executor thread but holds the GIL almost all the time.
# Every few thousand objects it sleeps briefly so the event loop runs freely.
_YIELD_EVERY = 2048
_YIELD_SECONDS = 0.001


def walk(
    roots: list[Root],
    stoppers: Iterable[Any],
    unreached_owner: Callable[[type], str],
) -> Accounts:
    """Account everything reachable from roots, then what nobody reached."""
    accounts: Accounts = defaultdict(Tally)
    seen: set[int] = {id(obj) for obj in stoppers}
    seen.update(id(root.obj) for root in roots)
    # Module dicts are only ever walked as code roots.
    seen.update(id(m.__dict__) for m in list(sys.modules.values()) if m is not None)
    shared: set[int] = set()
    shared_tally = accounts[(SHARED, DATA)]
    kinds: dict[type, tuple[int, bool]] = {}
    getsizeof = sys.getsizeof
    base_sizeof = object.__sizeof__
    getrefcount = sys.getrefcount
    get_referents = gc.get_referents

    def size_of(obj: Any, safe: bool) -> int:
        return getsizeof(obj) if safe else base_sizeof(obj)

    stack: list[Any] = []
    # The walk can reach its own frame through the thread it runs in; its
    # working structures must not be walked or counted.
    seen.update(id(obj) for obj in (stack, seen, shared, accounts, roots, kinds))
    push = stack.extend
    pop = stack.pop
    mark = seen.add
    shared_size = shared_count = 0
    sleep = time.sleep
    budget = _YIELD_EVERY
    for root in roots:
        code_module = root.module
        root_cls = type(root.obj)
        if (info := kinds.get(root_cls)) is None:
            info = kinds[root_cls] = _kind(root_cls)
        # Sums stay in locals inside the loop, which runs once per object.
        size = size_of(root.obj, info[1])
        count = 1
        push(get_referents(root.obj))
        while stack:
            budget -= 1
            if not budget:
                sleep(_YIELD_SECONDS)
                budget = _YIELD_EVERY
            obj = pop()
            cls = type(obj)
            if (info := kinds.get(cls)) is None:
                info = kinds[cls] = _kind(cls)
            kind, safe = info
            if kind == _LEAF:
                if getrefcount(obj) > _OWNED_REFS:
                    if id(obj) not in shared:
                        shared.add(id(obj))
                        shared_size += getsizeof(obj)
                        shared_count += 1
                else:
                    size += getsizeof(obj)
                    count += 1
                continue
            oid = id(obj)
            if oid in seen or kind == _MODULE:
                continue
            if code_module is None:
                # Classes and code belong to the module that defines them.
                if kind in (_CLASS, _CODE):
                    continue
            elif kind in (_CLASS, _FUNCTION) and obj.__module__ != code_module:
                # Imported names belong to their own module.
                continue
            mark(oid)
            size += getsizeof(obj) if safe else base_sizeof(obj)
            count += 1
            if kind == _FRAME:
                # Locals of a suspended coroutine are data; the frame chain is
                # not, and a running frame may be this walk itself.
                if obj.f_back is None:
                    push(obj.f_locals.values())
                continue
            push(get_referents(obj))
        tally = accounts[(root.owner, root.category)]
        tally.size += size
        tally.count += count
    shared_tally.size += shared_size
    shared_tally.count += shared_count

    for obj in gc.get_objects():
        budget -= 1
        if not budget:
            sleep(_YIELD_SECONDS)
            budget = _YIELD_EVERY
        if id(obj) in seen:
            continue
        cls = type(obj)
        if (info := kinds.get(cls)) is None:
            info = kinds[cls] = _kind(cls)
        if info[0] in (_CLASS, _MODULE, _CODE):
            continue
        tally = accounts[(unreached_owner(cls), UNREACHED)]
        tally.size += size_of(obj, info[1])
        tally.count += 1
    return dict(accounts)
