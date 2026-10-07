# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Project scaffold with src layout, uv lockfile and tool configuration.
- Pydantic schemas with `schema_version`.
- Config loading from YAML, with secrets read from the environment only.
- LLM wrapper: structured output, one repair retry, fail closed, backoff with jitter on
  transient errors, response cache and a fake transport for tests.
- CI workflow, pre-commit config and a Conventional Commits check.
