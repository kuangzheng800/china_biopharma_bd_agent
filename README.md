# China Biopharma Deal Research Agent

A command-line research agent that finds, structures and tracks biopharma deals involving Chinese
companies (out-licensing, in-licensing, NewCo spin-outs, M&A, co-development and more). It uses Claude
to run multi-round web searches, extracts each deal into a consistent schema, stores everything in a
persistent JSON database, and generates a sortable, filterable HTML dashboard plus a CSV export.

Each run **adds** to the database. Old entries are never re-researched or overwritten.

> **Data disclaimer:** deal information is AI-parsed from public sources with limited manual review.
> Terms, dates, values and classifications may contain errors or omissions. Verify against primary
> sources before relying on this data for investment or business decisions.

---

## Contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Common workflows](#common-workflows)
- [Files the agent reads and writes](#files-the-agent-reads-and-writes)
- [The dashboard](#the-dashboard)
- [Deal schema](#deal-schema)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)

---

## Requirements

- Python 3.9 or newer (3.11+ recommended)
- Two third-party packages: `anthropic` and `requests` (everything else is standard library)
- An **Anthropic API key**, which runs the agent
- A **Tavily API key** ([tavily.com](https://tavily.com), free tier available), which provides web search.
  Required for `discover` and `target`. Not needed for `import`, `remove`, `rebuild` or `--log`.
- Internet access when you open the dashboard, so the charts (Chart.js) and fonts can load from CDNs

## Setup

```bash
mkdir biopharma && cd biopharma
# put biopharma_agent.py in this folder

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install anthropic requests

export ANTHROPIC_API_KEY="sk-ant-..."
export TAVILY_API_KEY="tvly-..."
```

Windows PowerShell:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:TAVILY_API_KEY    = "tvly-..."
```

`export` only lasts for the current terminal session. Add the two lines to `~/.zshrc` or `~/.bashrc`
to make them permanent, and keep keys out of version control.

**Always run the script from the same folder.** All data files use relative paths, so running from
somewhere else silently starts a second, empty database.

## Quick start

```bash
# First build: sweep a year of deals
python biopharma_agent.py discover -w 2025

# Later: bring the database up to date (auto window, see below)
python biopharma_agent.py discover

# Open the dashboard
open index.html                      # macOS; or just double-click the file
```

On startup, `discover` and `target` run a one-query **preflight check**. If a key is missing or
Tavily rejects it, the run stops immediately with a clear message instead of spending API credits.

---

## Commands

```
python biopharma_agent.py <mode> [options]
```

| Mode | What it does | Needs API keys |
|---|---|---|
| `discover` | Wide search for all China biopharma deals in a time window | Anthropic + Tavily |
| `target` | Look up specific deals from a CSV of leads or a free-text description | Anthropic + Tavily |
| `import` | Load a CSV straight into the database, with no searching | None |
| `remove` | Delete deals by ID and regenerate outputs | None |
| `rebuild` | Regenerate the dashboard and CSV from the existing database | None |
| `--log` | Print the run history | None |

### `discover`: find new deals in a time window

```bash
python biopharma_agent.py discover                      # auto window
python biopharma_agent.py discover -w 2025              # a full year
python biopharma_agent.py discover -w 2025-Q1           # one quarter
python biopharma_agent.py discover -w 2024-Q3 2024-Q4   # several periods, merged into one range
python biopharma_agent.py discover -w 2026 --steps 100  # larger search budget
```

- **Window format:** `YYYY` or `YYYY-Q1` … `YYYY-Q4`.
- **Auto window (no `-w`):** from one month *before* the latest deal in the database up to today. The
  one-month overlap catches stragglers, and duplicates are skipped automatically. On an empty database
  it defaults to a broad 2024–2025 sweep.
- **`--steps N`:** the search budget (default 60). Only search calls count against it; saving a deal is free.

### `target`: look up specific deals

```bash
python biopharma_agent.py target leads.csv
python biopharma_agent.py target leads.csv -s 5
python biopharma_agent.py target "Hengrui licensed SHR-4849 to Ideaya for $1.2B"
```

Use this when you already know which deals you want. For a CSV it processes one row at a time:
a tight search (Chinese party + foreign party + asset + year), then a broader fallback, then a
Mandarin-language fallback. If every search fails, the row is saved from the CSV's own fields, with
unconfirmed fields marked "Not disclosed". This mode uses web searches and API credits. To load a CSV
without searching, use `import` instead.

- **`-s N`:** maximum searches per deal (default 3).
- **`--steps N`:** override the total step budget.
- Column headers in the leads CSV are matched flexibly (e.g. "Licensor", "Counterparty", "Upfront").

### `import`: load a CSV directly (no search, no cost)

```bash
python biopharma_agent.py import old_deals.csv --dry-run   # preview only, writes nothing
python biopharma_agent.py import old_deals.csv
```

Accepts this tool's own exports (current format, and older exports whose header row was misaligned),
plus the dashboard's "Export CSV" download. Every row passes through the same validation,
normalisation and de-duplication as a live save.

- Existing deals are kept; duplicates are skipped, so importing the same file twice adds nothing.
- Rows with no Chinese party or asset, or with a date that can't be read, are **rejected and listed**
  rather than guessed. Accepted date forms: `March 2024`, `Mar 2024`, `2024-03`, `24/03`.
- Before changing anything, it writes timestamped backups (`deals_database.bak-<time>.json` and
  `china_biopharma_deals.bak-<time>.csv`) next to the originals.

### `remove`: delete deals by ID

```bash
python biopharma_agent.py remove 9eeb8d3633 a41c07b2de
```

Removes the deals from `deals_database.json`, then regenerates `index.html` and the CSV. IDs come from
the dashboard's Delete mode (see below), which builds this command for you.

### `rebuild`: regenerate outputs only

```bash
python biopharma_agent.py rebuild
```

No searches. Rebuilds `index.html` and the CSV from the current database. Use it after hand-editing
`deals_database.json`, or to apply dashboard code changes to existing data.

### `--log`

```bash
python biopharma_agent.py --log
```

Prints the last 20 runs: timestamp, mode, window, deals added and database total.

---

## Common workflows

**Update an existing database to today**

```bash
cp deals_database.json deals_database.bak.json     # discover does not make its own backup
python biopharma_agent.py discover --steps 100
python biopharma_agent.py --log
```

For a long gap, running one quarter at a time is more thorough than a single pass:
`discover -w 2026-Q2`, then `-w 2026-Q3`, and so on.

**Restore from old CSV exports**

```bash
python biopharma_agent.py import old_export.csv --dry-run
python biopharma_agent.py import old_export.csv
```

**Clean up duplicates**

1. Open `index.html` and click **Delete mode**.
2. Tick the rows to remove and click **Copy remove command**.
3. Paste and run the command in your terminal. The database and dashboard update in one step.

**Check what the agent knows before a run**

```bash
python3 -c "import json,datetime as d;x=json.load(open('deals_database.json'))['deals'];print(len(x),'deals; latest:',max(d.datetime.strptime(i['announcement_month_year'],'%B %Y') for i in x).strftime('%B %Y'))"
```

The auto window starts from the latest deal's month, so check this if the window looks wrong.

---

## Files the agent reads and writes

All paths are relative to the folder you run the script from.

| File | Purpose |
|---|---|
| `deals_database.json` | **Source of truth.** Every mode loads it and saves back to it. |
| `index.html` | Interactive dashboard. Regenerated on every run. |
| `china_biopharma_deals.csv` | Flat export of the database. **Output only**, never read back by the agent. |
| `run_log.jsonl` | One JSON line per run (mode, window, deals added, total). |
| `deals_database.bak-*.json`, `china_biopharma_deals.bak-*.csv` | Backups made by `import`. |

If you lose `deals_database.json` but still have CSV exports, restore with `import`.

---

## The dashboard

Open `index.html` in any browser. It shows:

- **KPI tiles:** total deals and total disclosed deal value for the four most recent years
- **Four charts:** by therapeutic area, by month, top Chinese parties, top foreign parties. A toggle
  switches between deal counts and total value ($M).
- **A deal table** with date, parties, asset, drug name, therapeutic area, deal type, stage, modality,
  total value, upfront, territory, source link and highlights (hover to read the full text)
- **Sorting:** click any column header. The default is newest first, sorted on a numeric `YYYYMM`
  key so months order correctly.
- **Filtering:** by deal type, by therapeutic area, and a free-text search across the whole row
- **Export CSV:** downloads the table as it was generated
- **Delete mode:** adds checkboxes, strikes through selected rows, and copies the matching
  `remove` command to your clipboard

The page is static. It cannot change the database by itself, which is why deletions go through the
copied terminal command.

---

## Deal schema

Each deal in `deals_database.json` has these fields.

| Field | Notes |
|---|---|
| `announcement_month_year` | Always `Month YYYY`, e.g. `March 2024` |
| `deal_type` | licensing-out, licensing-in, option-to-license, newco-spinout, platform-deal, co-development, partnership, M&A, acquisition |
| `chinese_party`, `foreign_party` | Company names, normalised to a preferred display name |
| `asset` | Drug code, target and indication, e.g. `SHR-1819 (TSLP mAb, asthma)` |
| `drug_name` | Code only, e.g. `SHR-1819` |
| `therapeutic_area` | One of 22 flat categories (Oncology split into Lung, Breast, Hematologic, Other; plus Immunology, Neurology, Metabolic, and others) |
| `stage` | Preclinical, Phase 1, Phase 2, Phase 3, Approved, Platform |
| `modality` | Small Molecule, Monoclonal Antibody, Bispecific Antibody, ADC, Cell Therapy, Gene Therapy, siRNA, mRNA, Fusion Protein, Peptide, Oligonucleotide, Other |
| `total_value_usd`, `upfront_usd` | Single clean figure such as `$1.2B`, `$300M`, or `Not Disclosed` |
| `territory` | Global, Global ex-China, Greater China, US & Europe, and others |
| `equity_component` | Free text |
| `chinese_hq` | Yes, No or Unknown |
| `highlights` | One sentence on the most notable fact |
| `source_url`, `source_name` | Where the deal was found |
| `_id`, `_added_on` | Internal ID (10-character hash) and the date added |

The allowed values for the categorical fields are defined once, near the top of the script
(`DEAL_TYPE_ENUM`, `MODALITY_ENUM`, `STAGE_ENUM`, `TA_ENUM`, `TERRITORY_ENUM`). Edit them there only.

---

## How it works

1. **Search loop.** In `discover` mode, Claude is given a search window and a multi-round strategy and
   calls three search tools: open web (`search_web_wide`), priority English outlets
   (`search_web`: FierceBiotech, Endpoints News, BioPharma Dive, Reuters, Bloomberg, STAT), and
   Mandarin-language sources (`search_web_cn`: 医药魔方, 药渡, 生物谷 and stock-exchange filings).
   Rounds cover monthly sweeps, wide structural queries, modalities, partners, deal structures,
   Chinese-language queries, geography and VC ecosystem, and a priority-source clean-up.
2. **Save.** When Claude finds a deal it calls `save_deal` with the structured fields.
3. **Validate.** A deal is rejected if it has no Chinese party or asset, if the party reads like a
   headline, or if the source URL looks like a non-deal article (policy, earnings, trial results, and so on).
4. **Normalise.** Company-name variants are collapsed to one display name, and therapeutic area,
   territory and dollar values are mapped to the standard formats above.
5. **De-duplicate.** A deal is skipped if it matches an existing one on canonical Chinese party,
   canonical foreign party and month, plus matching deal value (within 20%) or a matching drug name,
   or if it matches on the exact party, asset, type and month.
6. **Output.** The database is saved after each new deal, and the dashboard and CSV are regenerated
   at the end of the run.

The step budget counts searches only. If the budget runs out or the API errors, the script saves what it
has collected so far.

---

## Configuration

Settings live at the top of `biopharma_agent.py`.

| Setting | Default | Meaning |
|---|---|---|
| `MODEL` | `claude-opus-4-6` | Claude model used by the agent. Change it if you get a model-not-found error or want a different model. |
| `MAX_STEPS` | `60` | Default search budget per run (override with `--steps`) |
| `PRIORITY_SOURCES` | six English outlets | Domains used by `search_web` |
| `CHINESE_SOURCES` | Chinese outlets and exchanges | Domains used by `search_web_cn` |
| `_COMPANY_ALIASES`, `_DISPLAY_NAME` | | Company-name variants and their preferred display names. Add entries here when the same company appears under several names. |

Environment variables: `ANTHROPIC_API_KEY` (required for `discover` and `target`) and
`TAVILY_API_KEY` (required for `discover` and `target`).

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Run ends with 0 deals added | Check the `<-` lines in the output. "No Tavily API key" means the key isn't set in this terminal. `Search error: Tavily HTTP 401` means the key was rejected, so check it on your Tavily dashboard. The preflight check now catches both before the run starts. |
| `[ERROR] ... is not set in this terminal session` | Export the key in the same terminal you run the script from. |
| "Existing deals in DB: 0" when you expect hundreds | You're in the wrong folder, or `deals_database.json` is missing. Move to the right folder, or restore with `import`. |
| Auto window looks too short | The window starts from the latest deal's month. A deal with a wrong future date shrinks it. Check the latest month (see [Common workflows](#common-workflows)) and use `-w` to set the window explicitly. |
| Auto window crashes on Windows | The auto-window code uses a date format Windows doesn't support. Use `-w`, for example `-w 2026`. |
| Charts are blank | The dashboard loads Chart.js from a CDN. Open it with an internet connection. |
| Model-not-found or API error | Update `MODEL` near the top of the script. |
| Old CSV columns look shifted | Early exports had 14 header labels over 16 columns. `import` reads those files by position and handles them correctly. |

---

## Limitations

- Results depend on what the search tool surfaces. Some deals will be missed, and some will be parsed
  imperfectly. Spot-check new entries against their source links.
- The step budget is a cap, not a guarantee of coverage. Long windows are better split into quarters.
- The dashboard is read-only. Edits and deletions happen through the command line.
- `chinese_hq` is stored in the database but not included in the CSV export, so deals restored through
  `import` won't have it.
