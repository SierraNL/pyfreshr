@.github/copilot-instructions.md

## Workflow

### Starting a new feature
Always create a new branch before making changes:
```bash
git checkout -b <branch-name>
```

### Before committing
Run all checks and ensure they pass before committing:
```bash
python -m pytest tests/ --cov
python -m ruff check src/ tests/ examples/
python -m mypy src/pyfreshr/
```

Review the changes for correctness, edge cases, and consistency with the rest of the codebase before committing.

### Committing
Reference the issue number in the commit message:
```
Add efficiency as a calculated property

Closes #11
```

### Opening a PR
Rebase on `main` if it has moved on, then push and open a PR against `main`:
```bash
git rebase main
git push -u origin <branch-name>
```