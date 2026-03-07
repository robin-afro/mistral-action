# Mistral Action

GitHub automation powered by [Mistral Vibe](https://github.com/mistralai/mistral-vibe). Brings Mistral's AI coding agent into your GitHub workflow — PR reviews, issue resolution, and custom automation via `@mistral` mentions.

Built as a direct analog to [anthropics/claude-code-action](https://github.com/anthropics/claude-code-action), but for Mistral models.

## How it works

```
Workflow YAML → action.yml → Python Orchestrator
                                  ├── Parse GitHub event context
                                  ├── Detect mode (tag / review / agent)
                                  ├── Check actor permissions
                                  ├── Post progress comment (🔄)
                                  ├── Assemble rich prompt
                                  │     ├── Issue/PR title + body
                                  │     ├── PR diff + file list
                                  │     ├── Comment thread
                                  │     ├── Review comments
                                  │     └── AGENTS.md project instructions
                                  ├── Create branch (for issues)
                                  ├── Run `vibe --auto-approve --prompt <mega_prompt>`
                                  ├── Commit + push changes
                                  ├── Create PR (if from issue)
                                  ├── Update progress comment (✅ / ❌)
                                  └── Set outputs (conclusion, branch, pr_url)
```

The agent knows how to spin up databases, Redis, and other services via Docker when it needs to run and test the project. See [System Prompt](#system-prompt) for details.

## Quick start

### 1. Add your Mistral API key as a repository secret

Go to **Settings → Secrets and variables → Actions** and add `MISTRAL_API_KEY`.

You can get one at [console.mistral.ai](https://console.mistral.ai).

### 2. Create the workflow file

Create `.github/workflows/mistral.yml`:

```yaml
name: Mistral

on:
  issue_comment:
    types: [created]
  issues:
    types: [opened, edited, labeled, assigned]
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  pull_request_review_comment:
    types: [created]

# Required permissions
permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  mistral:
    runs-on: ubuntu-latest
    # Only run if the comment contains @mistral (for comment events)
    # or always run for PR/issue events
    if: >
      github.event_name == 'pull_request' ||
      github.event_name == 'issues' ||
      contains(github.event.comment.body, '@mistral')
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Run Mistral Action
        uses: robin-afro/mistral-action@main
        with:
          mistral_api_key: ${{ secrets.MISTRAL_API_KEY }}
```

### 3. Use it

- **Comment `@mistral` on an issue** — Mistral reads the issue, creates a branch, implements the changes, and opens a PR.
- **Comment `@mistral` on a PR** — Mistral reads the diff and conversation, then pushes fixes or answers questions.
- **Open a PR** — Mistral automatically reviews the diff and posts feedback.

## Modes

The action auto-detects which mode to run based on the GitHub event:

| Mode | Trigger | What happens |
|------|---------|-------------|
| **Tag** | `@mistral` in a comment or issue body | Reads context, makes changes, commits, pushes. Creates a PR if triggered from an issue. |
| **Review** | PR opened / synchronized / reopened | Reads the diff and posts a code review. |
| **Agent** | Custom `prompt` input (e.g. via `workflow_dispatch`) | Runs the prompt directly with optional entity context. |

## Usage examples

### Issue resolution (tag mode)

Comment on an issue:

> @mistral implement this feature. Make sure to add tests.

Mistral will:
1. Read the issue description and all comments
2. Create a branch (`mistral/issue-42-1720000000`)
3. Explore the codebase
4. Implement the changes
5. Run tests (spinning up Docker services if needed)
6. Commit, push, and open a PR that references the issue

### PR review (review mode)

Automatic — just open a PR and the action runs a review:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
```

### PR fix request (tag mode)

Comment on a PR:

> @mistral the error handling in `parse_config` doesn't account for missing keys. Fix it and add a test.

Mistral will push a commit directly to the PR branch.

### Custom automation (agent mode)

Use `workflow_dispatch` with a custom prompt:

```yaml
on:
  workflow_dispatch:
    inputs:
      task:
        description: "Task for Mistral"
        required: true

jobs:
  mistral:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: robin-afro/mistral-action@main
        with:
          mistral_api_key: ${{ secrets.MISTRAL_API_KEY }}
          prompt: ${{ github.event.inputs.task }}
```

### Scheduled maintenance

```yaml
on:
  schedule:
    - cron: "0 9 * * 1" # Every Monday at 9am

jobs:
  mistral:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: robin-afro/mistral-action@main
        with:
          mistral_api_key: ${{ secrets.MISTRAL_API_KEY }}
          prompt: |
            Review the codebase for TODO comments and outdated dependencies.
            Create a summary issue with your findings.
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `mistral_api_key` | **Yes** | — | Mistral API key |
| `trigger_phrase` | No | `@mistral` | Phrase that triggers the action in comments |
| `assignee_trigger` | No | — | Username that triggers when assigned |
| `label_trigger` | No | `mistral` | Label that triggers when added |
| `prompt` | No | — | Custom prompt / additional instructions |
| `model` | No | *(Vibe default)* | Model name (e.g. `devstral-small-2`) |
| `max_turns` | No | — | Max assistant turns |
| `max_price` | No | — | Max cost in dollars |
| `vibe_args` | No | — | Extra CLI args for Vibe |
| `timeout_seconds` | No | `1800` | Max runtime in seconds |
| `output_format` | No | `text` | Vibe output: `text`, `json`, `streaming` |
| `github_token` | No | `github.token` | GitHub token for API calls |
| `base_branch` | No | *(repo default)* | Base branch for new branches |
| `branch_prefix` | No | `mistral/` | Prefix for created branches |
| `allowed_users` | No | — | Comma-separated allowed usernames, or `*` |
| `allowed_bots` | No | — | Comma-separated allowed bot usernames |
| `bot_name` | No | `mistral[bot]` | Git committer name |
| `bot_email` | No | `mistral[bot]@users.noreply.github.com` | Git committer email |
| `system_prompt_path` | No | — | Path to custom system prompt |
| `install_python` | No | `true` | Install Python on the runner |
| `python_version` | No | — | Custom Python version |
| `log_level` | No | `INFO` | Orchestrator log level |

## Outputs

| Output | Description |
|--------|-------------|
| `conclusion` | `success`, `failure`, `timeout`, or `skipped` |
| `branch_name` | Branch created or used by the action |
| `pr_url` | URL of the created PR (if any) |

## System prompt

The built-in system prompt (`prompts/system.md`) teaches Vibe how to operate as a GitHub-native agent. Key behaviors:

- **Explores the project first** — reads `README.md`, `AGENTS.md`, `Makefile`, `docker-compose.yml`, `pyproject.toml`, etc. to understand the stack and conventions.
- **Spins up services** — if the project needs a database, Redis, or other services, the agent will:
  1. Use existing `docker-compose.yml` if present
  2. Start individual Docker containers (`postgres`, `redis`, `mongo`, etc.)
  3. Fall back to in-memory mocks (`fakeredis`, SQLite, `mongomemoryserver`)
- **Runs tests** — always runs the test suite to verify changes work.
- **Runs linters** — checks for and runs existing lint/format tooling.
- **Runs migrations** — handles database migrations if the project uses them.
- **Never commits secrets** — uses dummy values and environment variables for test credentials.
- **Never pushes to main** — always works on the assigned branch.

### Project instructions

The agent reads project-specific instructions from these files (in order):

1. `AGENTS.md`
2. `.vibe/AGENTS.md`
3. `.github/AGENTS.md`
4. `CLAUDE.md` (for compatibility with Claude Code conventions)

Use these files to define your project's coding standards, test conventions, and any special setup instructions.

## Permissions

The action checks that the actor (the person who triggered it) has **write** permission to the repository. This prevents unauthorized users from triggering expensive API calls.

You can override this with the `allowed_users` input:

```yaml
with:
  allowed_users: "alice,bob"  # Only these users can trigger
  # or
  allowed_users: "*"  # Anyone can trigger (use with caution)
```

## Architecture

```
mistral-action/
├── action.yml                          # Composite GitHub Action definition
├── pyproject.toml                      # uv project with dependencies
├── prompts/
│   └── system.md                       # System prompt for the agent
└── src/
    └── mistral_action/
        ├── __init__.py
        ├── main.py                     # Main orchestrator (entrypoint)
        ├── context.py                  # GitHub event context parser
        ├── modes.py                    # Mode detection (tag/review/agent)
        ├── github_api.py               # GitHub API via `gh` CLI
        ├── prompt_builder.py           # Rich prompt assembly
        └── run_vibe.py                 # Vibe CLI installation and execution
```

### How it compares to Claude Code Action

| Feature | Claude Code Action | Mistral Action |
|---------|-------------------|----------------|
| Language | TypeScript + Bun | Python + uv |
| CLI agent | Claude Code (`claude -p`) | Mistral Vibe (`vibe --prompt`) |
| Mode detection | Tag / Agent | Tag / Review / Agent |
| Permission checks | ✅ | ✅ |
| Progress comments | ✅ | ✅ |
| Branch creation | ✅ | ✅ |
| PR creation | ✅ | ✅ |
| PR review | ✅ (via tag) | ✅ (dedicated review mode) |
| Project instructions | `CLAUDE.md` | `AGENTS.md` (+ `CLAUDE.md` compat) |
| Service orchestration | Via Claude's tools | Via system prompt (Docker, mocks) |
| GitHub API | Custom TypeScript client | `gh` CLI (pre-authenticated) |

## Development

```bash
# Clone
git clone https://github.com/robin-afro/mistral-action
cd mistral-action

# Install dependencies
uv sync

# Run tests
uv run pytest

# Run the orchestrator locally (needs GitHub event context)
GITHUB_EVENT_NAME=issue_comment \
GITHUB_EVENT_PATH=test-event.json \
MISTRAL_API_KEY=your-key \
uv run python -m mistral_action.main
```

## License

MIT