# Contributing to Weirwise Token Router

Thank you for your interest in contributing to the **Weirwise Token Router**! We welcome contributions to provider adapters, routing intelligence, documentation, and security hardening.

---

## Code of Conduct

Please treat all contributors with respect and professionalism. We are committed to providing a welcoming, inclusive, and harassment-free experience for everyone.

---

## Development Workflow

1. **Fork & Branch**: Create a descriptive feature or fix branch from `develop`:
   ```bash
   git checkout -b feat/add-new-provider
   ```
2. **Environment Setup**:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. **Coding Standards**:
   - Format with `black` and `ruff`.
   - Maintain type annotations on all public functions and adapter methods.
   - Do not print or log secrets, tokens, or encryption keys under any circumstances.
4. **Writing Tests**:
   - Every new adapter must include unit tests with mocked upstream responses.
   - Core routing logic must verify reservation creation and safe rollback on failure.
   - Place unit tests in `tests/unit/` and integration flows in `tests/integration/`.
   - **Live upstream tests** must use `pytest.skip` if `RUN_LIVE_UPSTREAM_TESTS=true` is not set.

---

## Commit Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:
- `feat:` New capability, provider adapter, or routing strategy.
- `fix:` Bug fix or security hardening.
- `docs:` Documentation improvements or updates.
- `test:` Adding or updating test cases.
- `refactor:` Code reorganization without behavioral change.

---

## Pull Request Checklist

Before submitting a Pull Request:
- [ ] All automated tests pass: `pytest tests/`
- [ ] No plaintext API keys, passwords, or secrets exist in the diff.
- [ ] New configuration options are documented in `.env.example` and `README.md`.
- [ ] Database schema changes include a corresponding Alembic migration.
