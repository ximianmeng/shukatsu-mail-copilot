# Shukatsu Mail Copilot

An AI-powered job hunting email assistant.

## Overview

Shukatsu Mail Copilot helps organize Japanese job hunting email workflows by reading messages from Apple Mail, extracting structured information with an OpenAI-compatible model, identifying action items, summarizing content, and syncing the results into Notion.

The project started as a practical automation tool for personal job-hunt operations and has been cleaned into a public engineering portfolio repository focused on pipeline design, structured extraction, and cautious automation.

## Features

- Email ingestion from Apple Mail selection or mailbox scan
- Demo-friendly local file ingestion for reviewers
- AI summarization for Japanese job-hunting emails
- Action item extraction and deadline parsing
- Priority and triage classification
- Notion integration for structured tracking
- Structured CSV export
- Safe mailbox routing with protection rules
- Undo and restore support for automated moves

## Tech Stack

- Python
- OpenAI API compatible client
- Notion API
- Apple Mail / AppleScript
- CSV processing with pandas

## Architecture

The workflow is:

1. Email ingestion from Apple Mail
2. AI extraction into structured JSON
3. Classification and normalization
4. CSV persistence
5. Optional Notion synchronization
6. Optional safe mailbox organization

More detail is in [docs/architecture.md](./docs/architecture.md).

## Repository Structure

```text
src/shukatsu_mail_copilot/   Core pipeline
scripts/                     Local helper launchers
tests/                       Lightweight normalization tests
docs/                        Architecture notes
examples/                    Example inputs
data/                        Generated runtime artifacts (gitignored)
```

## Setup

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### 2. Create your environment file

```bash
cp .env.example .env
```

Fill in:

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_BASE_URL` if using a non-default OpenAI-compatible endpoint
- `NOTION_API_KEY`
- `NOTION_DATA_SOURCE_ID`
- `APPLE_MAIL_SOURCE_MAILBOX`

### 3. Run the pipeline

Selected message mode:

```bash
python -m shukatsu_mail_copilot selected
```

Demo mode with a sample file:

```bash
python -m shukatsu_mail_copilot file examples/sample_mail.txt
```

Mailbox scan mode:

```bash
python -m shukatsu_mail_copilot mailbox
```

Dry-run classification:

```bash
python -m shukatsu_mail_copilot classify-dry-run
```

Safe move mode:

```bash
python -m shukatsu_mail_copilot safe-move
```

Undo last move:

```bash
python -m shukatsu_mail_copilot undo-last-move
```

Restore today:

```bash
python -m shukatsu_mail_copilot restore-today
```

## Notion Integration

The current implementation expects a Notion data source with fields for company, position, summaries, sender, category, deadlines, and action status. Optional fields such as confidence and triage category are added when the schema supports them.

## Safety and Privacy

- Secrets are loaded from `.env` and should never be committed.
- Runtime outputs are written to `data/` and ignored by Git.
- The safe move flow refuses low-confidence or protected messages.
- This public repository excludes personal logs, historical mailbox data, backups, and app bundles.

## Future Roadmap

- MCP integration
- Agent workflow orchestration
- Automatic mailbox organization improvements
- Multi-language support

## Recruiter Notes

This repository is strongest when framed as:

- A practical automation tool solving a real workflow problem
- An example of LLM extraction with normalization and safety checks
- A local-first integration project spanning Apple Mail, AI APIs, and Notion

To strengthen it further, consider adding:

- Real anonymized fixtures for repeatable evaluation
- Screenshots of the Notion sync result or launcher UI
- A small demo video or GIF
- CI for tests and linting
- A clearer separation between provider adapters and core domain logic
- A provider interface that cleanly separates Apple Mail ingestion from the extraction engine

## License

MIT
