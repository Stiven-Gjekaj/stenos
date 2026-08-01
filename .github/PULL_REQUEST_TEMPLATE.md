## Summary

Describe what this pull request changes and why.

## Related issue

Link the issue this addresses, if any (for example, "Closes #12").

## Changes

- 

## Testing

Explain how you verified the change. All of the following should pass locally:

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy src/`
- [ ] `uv run pytest`
- [ ] `uv run pytest tests/compat -m compat`

## Checklist

- [ ] I added or updated tests for my change, and no test loads a real model or
      opens a Discord connection.
- [ ] I bumped the version in `pyproject.toml` in the same commit as the change,
      ran `uv lock`, and used that version as the commit subject prefix.
- [ ] I updated `docs/` and the README if behaviour or configuration changed.
- [ ] I added a `CHANGELOG.md` entry if a user would notice this.
- [ ] My commits have clear messages with a body, and no co-author or tool
      attribution trailers.
