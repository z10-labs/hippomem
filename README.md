# Hippomem

**A team's engineering memory for AI coding agents.** Hippomem records why the team's software is the way it is: the designs it chose and the ones it rejected, the decisions and the constraints behind them. Agents read it before they decide something and write to it as they work.

> **Status: proof of concept, being built.** Hippomem runs on one machine while we prove it works. The goal is a remote memory the whole team's agents share. See [docs/hippomem.md](docs/hippomem.md) for what it is and the plan, and [docs/build-decisions.md](docs/build-decisions.md) for decisions made during the build.

## The problem

Coding agents make many design decisions a day inside conversations nobody rereads. Git keeps the result. The reasoning, and the options tried and dropped, disappear when the session goes idle. The next agent re-argues a settled question, reaches for the library the team already ruled out, or breaks a rule that covers a whole service it has never heard of.

Hippomem keeps that reasoning and returns it at the moment someone is about to decide the same thing again.

## How it works, briefly

- **One memory for a team, not a repo.** Decisions attach to *systems* (a service, a module, a shared library), and one system can span many repos.
- **Decisions are drafted during work and confirmed later.** Work on a branch leaves a draft trail, reversals included. At merge, drafts that were actually built become proposals, and reversed ones become rejected alternatives. Decisions covering a whole module or service are confirmed directly by an engineer.
- **Agents propose; acceptance follows rules.** Engineers and agents can review. Heavy decisions need an engineer, and nobody approves their own proposal.
- **Reading is triggered.** A digest at session start, a lookup before each edit, and server instructions, so the agent doesn't have to remember to ask.

## Docs

- [docs/hippomem.md](docs/hippomem.md): definition, scope, decision lifecycle, plan
- [docs/using-hippomem.md](docs/using-hippomem.md): how a team installs and uses it
- [docs/build-decisions.md](docs/build-decisions.md): decisions made while building it

## Development

```bash
git clone https://github.com/z10-labs/hippomem.git
cd hippomem
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full dev loop.

## History

Hippomem started as **Hippocampus**, a per-repo MCP server that stored decision records as markdown in `.decisions/records/`. That proof of concept is preserved on branch [`markdown-poc`](https://github.com/z10-labs/hippomem/tree/markdown-poc) (tag `v0.1-markdown-poc`). Research and validation: [hippocampus-research](https://github.com/z10-labs/hippocampus-research), [hippocampus-validation](https://github.com/z10-labs/hippocampus-validation).

## License

MIT, see [LICENSE](LICENSE).
