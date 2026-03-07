#!/usr/bin/env bash
set -euo pipefail

# Mistral Action — Quick Setup
# Run this from the root of any GitHub repository to add the Mistral Action workflow.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/robin-afro/mistral-action/main/setup.sh | bash
#   # or
#   bash <(curl -fsSL https://raw.githubusercontent.com/robin-afro/mistral-action/main/setup.sh)
#   # or locally:
#   ./setup.sh

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
RESET='\033[0m'

info()  { printf "${CYAN}▸${RESET} %s\n" "$*"; }
ok()    { printf "${GREEN}✔${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${RESET} %s\n" "$*"; }
error() { printf "${RED}✘${RESET} %s\n" "$*" >&2; }
step()  { printf "\n${BOLD}%s${RESET}\n" "$*"; }

# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------

step "Mistral Action Setup"

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    error "Not inside a git repository. Run this from the root of your repo."
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
info "Repository root: $REPO_ROOT"

# Detect remote to extract owner/repo
REMOTE_URL="$(git remote get-url origin 2>/dev/null || echo "")"
if [[ -z "$REMOTE_URL" ]]; then
    warn "No 'origin' remote found. You'll need to push this repo to GitHub."
    REPO_NWO=""
else
    # Extract owner/repo from SSH or HTTPS URL
    REPO_NWO="$(echo "$REMOTE_URL" | sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##')"
    info "GitHub repo: $REPO_NWO"
fi

# ---------------------------------------------------------------------------
# Check if workflow already exists
# ---------------------------------------------------------------------------

WORKFLOW_DIR=".github/workflows"
WORKFLOW_FILE="$WORKFLOW_DIR/mistral.yml"

if [[ -f "$WORKFLOW_FILE" ]]; then
    warn "Workflow already exists at $WORKFLOW_FILE"
    printf "  Overwrite? [y/N] "
    read -r REPLY
    if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
        info "Keeping existing workflow. Done."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Ask: which trigger phrase?
# ---------------------------------------------------------------------------

step "Configuration"

DEFAULT_TRIGGER="@mistral"
printf "  Trigger phrase ${DIM}[${DEFAULT_TRIGGER}]${RESET}: "
read -r TRIGGER_PHRASE
TRIGGER_PHRASE="${TRIGGER_PHRASE:-$DEFAULT_TRIGGER}"

# ---------------------------------------------------------------------------
# Ask: auto-review PRs?
# ---------------------------------------------------------------------------

printf "  Auto-review PRs on open? ${DIM}[Y/n]${RESET}: "
read -r AUTO_REVIEW
if [[ "$AUTO_REVIEW" =~ ^[Nn]$ ]]; then
    REVIEW_ENABLED=false
else
    REVIEW_ENABLED=true
fi

# ---------------------------------------------------------------------------
# Write the workflow file
# ---------------------------------------------------------------------------

step "Creating workflow"

mkdir -p "$WORKFLOW_DIR"

# Build the `on:` triggers
PR_TRIGGER=""
if [[ "$REVIEW_ENABLED" == true ]]; then
    PR_TRIGGER="
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]"
fi

cat > "$WORKFLOW_FILE" << YAML
name: Mistral

on:
  issue_comment:
    types: [created]
  issues:
    types: [opened, edited, labeled, assigned]${PR_TRIGGER}
  pull_request_review_comment:
    types: [created]
  pull_request_review:
    types: [submitted]

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  mistral:
    runs-on: ubuntu-latest
    # Only run when the trigger phrase is present (for comment events)
    # or always run for PR/issue lifecycle events
    if: >
      github.event_name == 'pull_request' ||
      github.event_name == 'issues' ||
      contains(github.event.comment.body, '${TRIGGER_PHRASE}') ||
      contains(github.event.review.body, '${TRIGGER_PHRASE}')
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Run Mistral
        uses: robin-afro/mistral-action@main
        with:
          mistral_api_key: \${{ secrets.MISTRAL_API_KEY }}
          trigger_phrase: "${TRIGGER_PHRASE}"
YAML

ok "Created $WORKFLOW_FILE"

# ---------------------------------------------------------------------------
# Create a starter AGENTS.md if none exists
# ---------------------------------------------------------------------------

if [[ ! -f "AGENTS.md" ]]; then
    step "Creating AGENTS.md"
    cat > "AGENTS.md" << 'AGENTS'
# Agent Instructions

<!-- This file tells AI coding agents (Mistral Vibe, Claude Code, etc.)
     about your project's conventions. Customize it for your codebase. -->

## Project overview

<!-- Describe your project in 1-2 sentences. -->

## Tech stack

<!-- List your languages, frameworks, and key dependencies. -->

## Development setup

<!-- How to install dependencies, set up the database, run the dev server, etc. -->

## Testing

<!-- How to run tests. Any conventions (e.g., table-driven tests, fixtures, factories). -->

## Code style

<!-- Formatting, linting, naming conventions, file organization. -->

## Important rules

<!-- Things the agent should never do, or must always do. -->
AGENTS

    ok "Created AGENTS.md (customize it for your project)"
else
    info "AGENTS.md already exists — skipping"
fi

# ---------------------------------------------------------------------------
# Set up the secret (if gh CLI is available)
# ---------------------------------------------------------------------------

step "API Key Setup"

HAS_GH=false
if command -v gh &>/dev/null; then
    # Check if authenticated
    if gh auth status &>/dev/null 2>&1; then
        HAS_GH=true
    fi
fi

if [[ "$HAS_GH" == true && -n "$REPO_NWO" ]]; then
    # Check if secret already exists
    SECRET_EXISTS=false
    if gh secret list --repo "$REPO_NWO" 2>/dev/null | grep -q "MISTRAL_API_KEY"; then
        SECRET_EXISTS=true
    fi

    if [[ "$SECRET_EXISTS" == true ]]; then
        ok "MISTRAL_API_KEY secret already configured"
    else
        printf "  Enter your Mistral API key ${DIM}(from console.mistral.ai)${RESET}: "
        read -rs API_KEY
        echo

        if [[ -n "$API_KEY" ]]; then
            echo "$API_KEY" | gh secret set MISTRAL_API_KEY --repo "$REPO_NWO"
            ok "MISTRAL_API_KEY secret set"
        else
            warn "No API key entered. You'll need to add it manually:"
            info "  gh secret set MISTRAL_API_KEY --repo $REPO_NWO"
            info "  or: Settings → Secrets and variables → Actions → New repository secret"
        fi
    fi
else
    warn "gh CLI not available or not authenticated."
    echo
    info "Add your Mistral API key as a repository secret:"
    if [[ -n "$REPO_NWO" ]]; then
        info "  gh secret set MISTRAL_API_KEY --repo $REPO_NWO"
    else
        info "  gh secret set MISTRAL_API_KEY"
    fi
    info "  or go to: Settings → Secrets and variables → Actions → New repository secret"
    info "  Get a key at: https://console.mistral.ai"
fi

# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

step "Finishing up"

printf "  Commit and push changes now? ${DIM}[Y/n]${RESET}: "
read -r DO_COMMIT
if [[ ! "$DO_COMMIT" =~ ^[Nn]$ ]]; then
    git add "$WORKFLOW_FILE"
    [[ -f "AGENTS.md" ]] && git add "AGENTS.md"

    git commit -m "ci: add Mistral Action workflow

Adds GitHub Actions workflow for Mistral Vibe AI agent.
Trigger: ${TRIGGER_PHRASE} in issues/PRs$(if [[ "$REVIEW_ENABLED" == true ]]; then echo "
Auto-review: enabled for new PRs"; fi)"

    git push origin "$(git branch --show-current)" 2>&1 || {
        warn "Push failed. You can push manually: git push"
    }
    ok "Committed and pushed"
else
    info "Changes staged but not committed. Run:"
    info "  git add $WORKFLOW_FILE AGENTS.md && git commit -m 'ci: add Mistral Action' && git push"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
printf "${GREEN}${BOLD}Setup complete!${RESET}\n"
echo
info "Try it out:"
info "  1. Create an issue and comment: ${BOLD}${TRIGGER_PHRASE} implement a hello world endpoint${RESET}"
info "  2. Open a PR to get an automatic review"
info "  3. Comment on a PR: ${BOLD}${TRIGGER_PHRASE} fix the failing test${RESET}"
echo
info "Customize your agent's behavior by editing AGENTS.md"
echo
