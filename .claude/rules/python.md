---
paths:
  - "src/**/*.py"
  - "scripts/**/*.py"
---

## Design

- Keep modules under 300 lines; one logical concern per module
- Keep functions under 40 lines; prefer 3 or fewer parameters (group related params with dataclass or TypedDict)
- Google-style docstrings (Args/Returns/Raises) on all public functions; document *why*, not what the type signature already says; don't document obvious code

## Error Handling

- Define a package-level base exception; derive all specific errors from it
- Catch the most specific exception possible
- Use `logging.exception()` in catch blocks (auto-includes traceback), never `logger.error(str(e))`
- Never swallow exceptions silently; if catching, handle meaningfully or re-raise
- Never use exceptions for control flow
- Return `None` or a sentinel only when the caller expects it; prefer raising for true errors

## Type System

- Prefer `@dataclass(frozen=True, slots=True)` for internal value objects
- Use Pydantic (`BaseModel`) only at serialization/deserialization boundaries
- Use `TypedDict` for structured dict shapes (API responses, config dicts)
- Use `Protocol` for structural subtyping instead of ABC when possible
- Avoid `Any`; when unavoidable, add a comment explaining why (e.g., `# Any: third-party lib has no stubs`)

## Performance

- Use generator expressions and `itertools` for large sequences; avoid materializing unnecessary lists
- Use `__slots__` on frequently instantiated classes (dataclass `slots=True`)
- Use `functools.lru_cache` or `functools.cache` for expensive pure functions
- Prefer `str.join()` over `+=` concatenation in loops
- Use `collections.defaultdict`, `Counter`, `deque` instead of hand-rolled equivalents
- Avoid repeated attribute lookups in tight loops; bind to local variable
- Use `dict`/`set` for O(1) membership tests instead of lists
- Lazy-import heavy optional dependencies inside functions to reduce import time

## Pythonic Patterns

- EAFP (try/except) over LBYL (if-check) when dealing with duck typing or I/O
- Use context managers (`with`) for all resource management (files, connections, locks)
- Prefer comprehensions over `map()`/`filter()` for readability
- Use `enum.Enum` for fixed sets of values instead of string constants
- Use `walrus operator` (:=) for assign-and-test when it improves clarity
- Use structural pattern matching (`match/case`) for complex dispatch
- Use `*args` unpacking and `**kwargs` deliberately; avoid passing them blindly through call chains

## Security

- Sanitize file paths to prevent directory traversal (`pathlib.Path.resolve()` then check prefix)
- Ruff's bandit rules (`S`) cover eval/exec/pickle/random misuse — do not suppress them with `noqa` without a written justification

## Constants and Naming

- Use `UPPER_SNAKE_CASE` named constants instead of magic numbers/strings
- Boolean variables/params: prefix with `is_`, `has_`, `can_`, `should_`
- Private helpers: prefix with `_`; reserve `__` (name mangling) only for avoiding conflicts in subclass hierarchies
