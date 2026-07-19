# Pinscope

Pinscope reviews schematics the way a good senior engineer does: with the datasheets open.

<img width="1912" height="1080" alt="pinscope-screenrecording" src="https://github.com/user-attachments/assets/7e9e4002-08df-423f-93c9-eefd52e88700" />


Give it a netlist, a BOM, and your datasheet PDFs. It builds a graph of your design, reads each IC's datasheet, and checks the circuit around every part against what the manufacturer actually specifies — reference application, pin functions, absolute maximums, recommended operating conditions. Every finding points at the datasheet page that backs it up, so you can judge the call yourself instead of trusting a black box.

The reason it exists: ERC passes boards that don't work. Your EDA tool has no idea that the CH340E you powered from 5 V drives its TXD at 4.5 V into an MCU pin that maxes out at 3.6 V, or that the net you labeled `UART5_TX` lands on a pin whose alternate-function table only offers `UART5_RX`, or that the LDO's bypass pin you left floating costs you an order of magnitude in output noise. None of that is an electrical *rule* violation. All of it is in the datasheet, and nobody has time to re-read 400 pages per part on every revision.

## How it works

<p align="center">
  <img src="docs/how-it-works.svg" width="920" alt="Pipeline: the netlist and BOM are parsed into a design graph; datasheet PDFs are extracted into pin tables and specs; a per-IC review reads both and files findings cited to datasheet pages; the derating table and BOM roll-up are computed straight from the graph, no model involved.">
</p>

1. **Parse** the BOM (CSV/XLSX) and netlist (PADS-PCB `.asc` or EDIF 2.0.0 `.edn` — exportable from KiCad, Altium, OrCAD, Allegro, Xpedition, EasyEDA, Eagle) into a queryable bipartite graph of components and nets.
2. **Extract** pin tables and specs from the PDFs. Large datasheets are trimmed to the relevant pages first, and every extraction is cached in a shared library, so a given part number is only ever processed once.
3. **Review** each IC in isolation. The model gets the trimmed datasheet plus that IC's circuit neighborhood, can query the graph (`find_connected_components`, `get_net_for_pin`, `get_pintable`) and pull pages from a *connected* part's datasheet when a finding spans an interface. It files findings with severity, reasoning, and page citations.
4. **Compute** the deterministic parts deterministically — BOM roll-up and a capacitor voltage-derating table come straight from the graph, no model involved.

A post-pass normalizes findings conservatively: it can merge duplicates and downgrade severity, never upgrade. If the reviewer hedged, the report hedges.

It's a reviewer, not an oracle. It misses things, and it will occasionally question a choice you made on purpose — that's what the citations are for.

## LLM Setup

Pinscope uses [LiteLLM](https://docs.litellm.ai/) to talk to any LLM. Set `DEFAULT_MODEL` to a model string with a provider prefix, and your API key in the corresponding environment variable.

### Supported providers

| Provider | Model prefix | API key env var | Notes |
|---|---|---|---|
| Anthropic | `anthropic/...` | `ANTHROPIC_API_KEY` | Default provider |
| OpenAI | `openai/...` | `OPENAI_API_KEY` | |
| Google Gemini | `gemini/...` | `GOOGLE_API_KEY` | |
| Ollama | `ollama/...` | *(none)* | Runs locally; see setup below |
| llama.cpp / llamafile | `openai/...` (via local server) | *(none)* | Runs locally; see setup below |

**Default model:** `anthropic/claude-sonnet-4-6`

### Provider-specific setup

**Anthropic** — [Get an API key](https://console.anthropic.com/) and set `ANTHROPIC_API_KEY`.

**OpenAI** — [Get an API key](https://platform.openai.com/api-keys) and set `OPENAI_API_KEY`.

**Google Gemini** — [Get an API key](https://aistudio.google.com/apikey) and set `GOOGLE_API_KEY`.

**Ollama** — Install [Ollama](https://ollama.com), pull a model, and run it:
```bash
# Install Ollama (macOS/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model (Sonnet-class quality recommended)
ollama pull llama3.1:70b

# Run the server (default: http://localhost:11434)
ollama serve
```
Then set `DEFAULT_MODEL=ollama/llama3.1:70b`. No API key needed. (If Ollama runs on a non-default port, set `OLLAMA_BASE_URL=http://localhost:<port>`.)

**llama.cpp / llamafile** — Download a [llamafile](https://github.com/Mozilla-Ocho/llamafile), make it executable, and run it:
```bash
chmod +x llama-3.1-70b-instruct.Q4_K_M.llamafile
./llama-3.1-70b-instruct.Q4_K_M.llamafile --server --host 0.0.0.0 --port 8080
```
Then set:
```bash
DEFAULT_MODEL=openai/llama-3.1-70b-instruct
OPENAI_API_KEY=anything   # llamafile accepts any key
OPENAI_BASE_URL=http://localhost:8080/v1
```
The `OPENAI_BASE_URL` points to the local llamafile server; `OPENAI_API_KEY` can be any string since llamafile doesn't require authentication.

### Per-stage model overrides

Each pipeline stage can use a different model. Set `MODEL_<STAGE>` to override the default for that stage:
- `MODEL_VALIDATION` — IC review (the most expensive stage)
- `MODEL_PINTABLE` — IC pin-table extraction
- `MODEL_PATTERN` — passive component pattern extraction
- `MODEL_SPECS` — simple component spec extraction
- `MODEL_AUTO_RESOLVE` — DigiKey MPN auto-resolution
- `MODEL_NORMALIZE` — findings normalization

Example: use Sonnet for review but Haiku for auto-resolve:
```bash
DEFAULT_MODEL=anthropic/claude-sonnet-4-6
MODEL_AUTO_RESOLVE=anthropic/claude-haiku-4-5-20251001
```

## Try it on the bundled design

`simple_project/` is a small MSPM0G3507 board with a CH340E USB-UART bridge and an SPX3819 LDO. Run it through and Pinscope flags, among other things, the LDO's bypass pin left unconnected (~300 µV<sub>RMS</sub> output noise instead of ~40) and the 5 V-powered CH340E driving the 3.3 V MCU directly — each with the page reference to check its work.

You need Python 3.12+, Node 20+, and an API key for your chosen provider (see above).

```bash
pip install -r backend/requirements.txt
cp backend/.env.example .env                 # set your API key and model
python3 -m uvicorn backend.main:app --reload
cd frontend && npm install && npm run dev
```

Open http://localhost:3000, create a project, and feed it the netlist and BOM from `simple_project/` plus datasheet PDFs for the ICs — grab those from the manufacturers, or set the optional DigiKey API keys and let it fetch them. Everything runs locally against your own key; projects and the extraction library live in `data/`. Architecture notes are in [CLAUDE.md](CLAUDE.md).

## Hosted version

This repo is the product minus accounts and billing. If you'd rather not run it yourself, [pinscope.ai](https://pinscope.ai) is the same code, hosted, with team workspaces and a shared parts library that's already warm.

## License

AGPL-3.0. For commercial licensing, write to dev@faradworks.com.
