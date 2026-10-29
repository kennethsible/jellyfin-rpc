# Contributing

Thank you for your interest in contributing to Jellyfin RPC! Please review these guidelines before starting work on the project.

## 1. Issue & Approval Process

To prevent wasted effort on unwanted features, architectural mismatches, or duplicate work:

- **Open an Issue First:** Every pull request must be linked to an existing GitHub issue created for discussion.
- **Wait for Approval:** Do not open a pull request until the issue has been discussed and explicitly approved by the repository owner (`@kennethsible`).
- **Unsolicited PRs:** Pull requests opened without an approved, associated issue may be closed without review.

## 2. Policy on AI-Assisted Code

We recognize that AI tools can enhance developer productivity, but we maintain strict standards regarding code quality and understanding:

- **Permitted:** AI assistance is allowed **only** if you have a foundational programming background and have thoroughly read, understood, tested, and validated every line of code generated.
- **Prohibited:**
  - **No "Vibe Coding":** Submitting unverified, untested, or poorly understood code generated purely through prompts is strictly forbidden.
  - **No Autonomous Agent Commits:** Direct commits or PRs generated end-to-end by automated AI coding agents will be rejected.
- **Ownership:** You are fully responsible for all code you submit. If you cannot explain the logic, design decisions, or edge cases during review, the contribution will be rejected.

## 3. Getting Started & Development

This project uses [uv](https://docs.astral.sh/uv/getting-started/installation/) to manage its Python environment and dependencies.

1. **Fork and Clone:** Fork the repository on GitHub and clone your fork locally:

   ```bash
   git clone https://github.com/<your-username>/jellyfin-rpc.git
   cd jellyfin-rpc
   ```

2. **Install Dependencies:** Sync all dependencies, including development packages:

   ```bash
   uv sync --all-extras
   ```

3. **Create a Feature Branch:** Create a descriptive feature branch off `main`:

   ```bash
   git checkout -b descriptive-branch-name
   ```

4. **Local Testing:** Test your changes against a running Jellyfin instance and Discord client to ensure the RPC functions as intended without regressions.

## 4. Code Standards & Quality Checks

All pull requests run automated checks in GitHub Actions. Run the following commands locally before staging or pushing your changes to ensure they pass:

- **Formatting (Ruff):**

  ```bash
  uv run ruff format --check src/
  ```

- **Linting (Ruff):**

  ```bash
  uv run ruff check src/
  ```

- **Type Checking (Mypy):**

  ```bash
  uv run mypy src/
  ```

### Commit Guidelines

- **Atomic Commits:** Keep commits focused and descriptive. You may use conventional commit prefixes where applicable (e.g., `feat:`, `fix:`, `docs:`, `refactor:`), though they are not strictly enforced.
- **No Secrets:** Never commit personal API keys, Jellyfin server URLs, Discord application secrets, or sensitive configuration data.

## 5. Submitting a Pull Request

Keep your pull request focused on the approved issue and avoid including unrelated changes. Once your issue has been approved, your changes are locally tested, and all quality checks pass:

1. Push your branch to your fork.
2. Open a Pull Request targeting the `main` branch.
3. Fill out the PR template completely:
   - Link the approved issue (e.g., `Fixes #12` or `Closes #45`).
   - Provide a concise summary of the changes and how you verified them.
   - Include screenshots if you are introducing GUI changes.
4. Address code review feedback promptly. Keep discussions polite and constructive.
