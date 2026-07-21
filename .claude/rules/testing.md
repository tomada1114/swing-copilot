---
paths:
  - "tests/**/*.py"
---

## Structure and Organization

- File structure mirrors source: `tests/test_<module>.py`
- Shared fixtures go in `tests/conftest.py`; use the narrowest fixture scope possible
- Function names: `test_<what>_<scenario>_<expected_result>` (e.g., `test_parse_config_empty_string_raises_value_error`)
- Follow Arrange-Act-Assert: set up data, execute the behavior, verify the outcome
- One logical assertion per test; multiple `assert` statements are fine if they verify one behavior

## What to Test

- Test *behavior and contracts*, not implementation details
- Never test private methods directly; test through the public interface
- Always test the happy path AND the error path for every public function
- If a function can raise, test that it raises the right exception with the right message

## Edge Cases (always consider these)

- **Empty inputs**: empty string, empty list, empty dict, None where optional
- **Boundary values**: 0, 1, -1, max int, min int, float('inf'), float('nan')
- **Type boundaries**: very long strings, unicode/emoji, mixed encodings
- **Collection boundaries**: single element, duplicate elements, max expected size
- **Concurrent scenarios**: if the code is async, test cancellation and timeout behavior
- **State transitions**: initial state, after one operation, after repeated operations, after error recovery

## Error and Exception Testing

- Use `pytest.raises(XError, match=r"expected message")` -- always verify the message pattern
- Test that errors propagate correctly through the call chain
- Test that cleanup/teardown runs even when exceptions occur (context managers, finally blocks)
- Test invalid argument combinations that should be rejected
- Test error recovery: after an error, does the object remain in a consistent state?

## Parametrize and Data-Driven Tests

- Use `@pytest.mark.parametrize` for input/output variations; don't copy-paste test bodies
- Group related parametrize cases with `pytest.param(..., id="descriptive-name")`
- For complex parametrized data, use a helper function or fixture to build test cases
- Consider property-based testing with `hypothesis` for functions with well-defined invariants (add to dev deps if using)

## Fixtures

- Prefer factory fixtures over static fixtures: `def make_user(**overrides)` returns a customizable object
- Use `tmp_path` for filesystem tests; never write to real directories
- Use `monkeypatch` for environment variables, not direct `os.environ` manipulation
- Fixtures that open resources must clean up with `yield` + teardown or `addfinalizer`
- Scope fixtures appropriately: `function` (default) for isolation, `session` only for truly expensive setup

## Mocking Strategy

- Mock at boundaries only: I/O, network, clock (`freezegun`/`time_machine`), external services
- Never mock the unit under test
- Prefer fakes (in-memory implementations) over mocks for repositories/stores
- Use `unittest.mock.AsyncMock` for async callables
- Assert on behavior and outputs, not on how many times a mock was called
- If you need to mock more than 2 things in one test, the code under test may have too many dependencies

## Test Independence and Reliability

- Tests must be independent: no shared mutable state, no ordering dependency
- Each test must pass when run alone (`pytest tests/test_foo.py::test_specific`)
- No `@pytest.mark.skip` or TODO tests on main -- delete them or fix them
- No `time.sleep` in tests; design tests to be event-driven or use deterministic fakes for time
- Flaky tests must be fixed immediately, not ignored

## Coverage Philosophy

- Coverage is a *floor*, not a *ceiling* -- 80% minimum, but aim for meaningful coverage not percentage
- Branch coverage matters more than line coverage; always test both sides of conditionals
- Missing coverage should prompt "is this code reachable?" -- if not, delete it
- Don't write trivial tests just to hit numbers; cover edge cases and error paths instead

## Anti-Patterns

- Don't test getters/setters while missing business logic edge cases
- Don't use `assert True` or `assert result is not None` when a specific value should be checked
- Don't share mutable test data between tests (use fresh fixtures)
- Don't test that a dependency's library works (e.g., testing that `json.loads` parses JSON)
- Don't mock everything -- integration tests with real dependencies catch real bugs
