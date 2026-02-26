"""
=============================================================
  BIOPHARMA DEAL RESEARCH AGENT — Powered by Claude
  v2 — with persistence, dashboard output, enriched fields
=============================================================

WHAT THIS AGENT DOES:
  Researches China biopharma deals and:
  - Extracts: month/year, therapeutic area, deal highlights, source link
  - Prioritizes: FierceBiotech, Endpoints News, BioPharma Dive, Reuters
  - Saves results to deals_database.json (persistent across runs)
  - Generates/updates dashboard.html — open in any browser

HOW TO USE IT:
  1. Install:    pip install anthropic requests
  2. Set key:    export ANTHROPIC_API_KEY="sk-ant-..."
  3. Optional:   export TAVILY_API_KEY="tvly-..."  (tavily.com — free tier)
  4. Run:        python biopharma_agent.py
  5. Open:       dashboard.html in your browser

  Each run ADDS new deals to the database without re-researching old ones.
  The dashboard always reflects the full historical database.
=============================================================
"""

import os
import json
import argparse
import anthropic
import requests
import hashlib
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_KEY_HERE")
TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY", "")

client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MODEL    = "claude-opus-4-6"
MAX_STEPS = 15

# Persistent database file
DB_PATH        = Path("deals_database.json")
DASHBOARD_PATH = Path("dashboard.html")
CSV_PATH       = Path("china_biopharma_deals.csv")

PRIORITY_SOURCES = [
    "fiercebiotech.com",
    "endpointsnews.com",
    "biopharmadive.com",
    "reuters.com",
    "bloomberg.com",
    "statnews.com",
]

# ── Persistent Database ────────────────────────────────────────────────────────

def load_database() -> dict:
    if DB_PATH.exists():
        with open(DB_PATH) as f:
            return json.load(f)
    return {"last_updated": None, "total_deals": 0, "deals": []}

def save_database(db: dict, mark_run: bool = False):
    db["last_updated"] = datetime.now().isoformat()
    db["total_deals"]  = len(db["deals"])
    if mark_run:
        db["last_run_date"] = datetime.now().strftime("%Y-%m-%d")
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)

def make_deal_id(deal: dict) -> str:
    key = f"{deal.get('chinese_party','')}{deal.get('asset','')}{deal.get('deal_type','')}".lower().strip()
    return hashlib.md5(key.encode()).hexdigest()[:10]

def is_duplicate(deal: dict, existing_deals: list) -> bool:
    new_id = make_deal_id(deal)
    for d in existing_deals:
        if d.get("_id") == new_id:
            return True
        if (d.get("chinese_party","").lower() == deal.get("chinese_party","").lower()
                and d.get("asset","").lower() == deal.get("asset","").lower()
                and d.get("deal_type","").lower() == deal.get("deal_type","").lower()):
            return True
    return False

# ── Tool Definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_web",
        "description": (
            "Search the web for recent biopharma deal news. "
            "Prefer fiercebiotech.com, endpointsnews.com, biopharmadive.com, reuters.com."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_deal",
        "description": "Save one structured deal record. Call once per deal found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "announcement_month_year": {
                    "type": "string",
                    "description": "Month and year e.g. 'March 2024'"
                },
                "deal_type": {
                    "type": "string",
                    "description": "licensing-out, licensing-in, M&A, partnership, co-development, acquisition"
                },
                "chinese_party": {"type": "string"},
                "foreign_party":  {"type": "string", "description": "Non-Chinese company or 'N/A'"},
                "asset":          {"type": "string", "description": "Drug name or platform"},
                "modality": {
                    "type": "string",
                    "description": "e.g. Small Molecule, Monoclonal Antibody, Bispecific Antibody, ADC, Cell Therapy, Gene Therapy, siRNA, mRNA, Fusion Protein, Peptide, Oligonucleotide"
                },
                "therapeutic_area": {
                    "type": "string",
                    "description": "e.g. Oncology, CNS, Rare Disease, Immunology, Metabolic"
                },
                "stage": {
                    "type": "string",
                    "description": "Preclinical, Phase 1, Phase 2, Phase 3, Approved, Platform"
                },
                "total_value_usd": {"type": "string", "description": "e.g. '$1.2B' or 'Not disclosed'"},
                "upfront_usd":     {"type": "string", "description": "e.g. '$100M' or 'Not disclosed'"},
                "territory":       {"type": "string", "description": "e.g. 'Global ex-China', 'US & Europe'"},
                "highlights": {
                    "type": "string",
                    "description": "2-3 sentence analyst commentary: why is this deal notable? strategic rationale? standout terms?"
                },
                "source_url": {
                    "type": "string",
                    "description": "URL — prefer FierceBiotech, Endpoints News, BioPharma Dive, Reuters"
                },
                "source_name": {
                    "type": "string",
                    "description": "Publication e.g. 'FierceBiotech', 'Endpoints News', 'Reuters'"
                }
            },
            "required": [
                "announcement_month_year", "deal_type", "chinese_party",
                "asset", "therapeutic_area", "highlights", "source_url", "source_name"
            ]
        }
    },
    {
        "name": "finish",
        "description": "Call when done with all searches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"}
            },
            "required": ["summary"]
        }
    }
]

# ── Tool Implementations ───────────────────────────────────────────────────────

new_deals_this_run = []
db = load_database()

def fetch_article(url: str, max_chars: int = 3000) -> str:
    """Fetch article text from a URL, return truncated content."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BiopharmaBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        text = resp.text
        # Strip HTML tags crudely
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[fetch failed: {e}]"


def search_web(query: str) -> str:
    if TAVILY_API_KEY:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "advanced",   # upgraded: gets fuller content
                    "max_results": 5,
                    "include_domains": PRIORITY_SOURCES
                },
                timeout=15
            )
            results = resp.json().get("results", [])
            if not results:
                resp2 = requests.post(
                    "https://api.tavily.com/search",
                    json={"api_key": TAVILY_API_KEY, "query": query,
                          "search_depth": "advanced", "max_results": 5},
                    timeout=15
                )
                results = resp2.json().get("results", [])
            if not results:
                return "No results found."
            out = []
            for r in results:
                domain   = r.get("url","").split("/")[2] if "/" in r.get("url","") else ""
                priority = "★ PRIORITY SOURCE" if any(p in domain for p in PRIORITY_SOURCES) else ""
                # Use Tavily's full content field (advanced search returns more)
                body = r.get("content","") or r.get("raw_content","") or ""
                out.append(
                    f"{priority}\nTITLE: {r.get('title','')}\n"
                    f"URL: {r.get('url','')}\n"
                    f"CONTENT: {body[:1500]}\n"
                )
            return "\n---\n".join(out)
        except Exception as e:
            return f"Search error: {e}"
    else:
        # Mock data — remove once you have Tavily
        return """
★ PRIORITY SOURCE
TITLE: BeiGene licenses zanubrutinib to Novartis in $3.1B deal
URL: https://www.fiercebiotech.com/biotech/beigene-novartis-zanubrutinib
SNIPPET: BeiGene struck a landmark licensing deal granting Novartis global rights (ex-China) to zanubrutinib. $300M upfront, up to $2.8B milestones. The BTK inhibitor is approved for CLL and MCL.

---
★ PRIORITY SOURCE
TITLE: Hengrui out-licenses TROP2 ADC to Merck KGaA for $1.4B
URL: https://endpointsnews.com/2024/03/hengrui-merck-adc
SNIPPET: Hengrui granted US/EU rights for SHR-A1921 to Merck KGaA. $100M upfront + $1.3B milestones. Phase 1 in NSCLC with strong early signals.

---
★ PRIORITY SOURCE
TITLE: Akeso PD-1/VEGF bispecific lands $500M Summit deal
URL: https://endpointsnews.com/2024/akeso-summit-ivonescimab
SNIPPET: Akeso partnered with Summit Therapeutics to co-develop ivonescimab (AK112) outside Greater China. $500M upfront — largest ever for a Chinese biotech asset.

---
TITLE: LaNovation signs ADC licensing deal with AstraZeneca
URL: https://www.biopharmadive.com/news/lanovation-astrazeneca-adc-2024
SNIPPET: LaNovation Pharma licensed its HER3-targeting ADC to AstraZeneca for global rights. Deal includes $75M upfront and $900M in milestones. Preclinical stage. Part of AZ's accelerating ADC strategy.
"""

MONTH_NAMES = {"January","February","March","April","May","June",
               "July","August","September","October","November","December"}

def sanitize_date(raw: str) -> str:
    """Ensure announcement_month_year is 'Month YYYY'. Fallback to current month."""
    if not raw:
        return datetime.now().strftime("%B %Y")
    parts = raw.strip().split()
    if len(parts) == 2 and parts[0] in MONTH_NAMES:
        try:
            int(parts[1])   # year must be numeric
            return raw.strip()
        except ValueError:
            pass
    # Try to salvage a 4-digit year anywhere in the string
    import re
    yr_match = re.search(r"\b(20\d{2})\b", raw)
    yr = yr_match.group(1) if yr_match else datetime.now().strftime("%Y")
    return datetime.now().strftime(f"%B {yr}")

def save_deal(deal_data: dict) -> str:
    global db, new_deals_this_run
    deal_data["announcement_month_year"] = sanitize_date(
        deal_data.get("announcement_month_year", ""))
    deal_data["_id"]       = make_deal_id(deal_data)
    deal_data["_added_on"] = datetime.now().strftime("%Y-%m-%d")

    if is_duplicate(deal_data, db["deals"]):
        return f"Duplicate skipped: {deal_data.get('chinese_party')} / {deal_data.get('asset')}"

    db["deals"].append(deal_data)
    new_deals_this_run.append(deal_data)
    save_database(db)
    return f"Saved #{len(db['deals'])}: {deal_data.get('chinese_party')} — {deal_data.get('asset')} [{deal_data.get('source_name')}]"

def finish(summary: str) -> str:
    return f"AGENT_DONE: {summary}"

def run_tool(name: str, inp: dict) -> str:
    if name == "search_web": return search_web(**inp)
    if name == "save_deal":  return save_deal(inp)
    if name == "finish":     return finish(**inp)
    return f"Unknown tool: {name}"

# ── System Prompt ──────────────────────────────────────────────────────────────

def build_system_prompt(existing_deals: list, search_window: str = "") -> str:
    recent = "\n".join(
        f"- {d.get('chinese_party')} / {d.get('asset')} / {d.get('deal_type')} ({d.get('announcement_month_year','')})"
        for d in existing_deals[-15:]
    ) or "None yet — this is the first run."

    window_str = search_window if search_window else "Search for deals from all time."
    return f"""You are a senior biopharma deal analyst specializing in China life science transactions.

DATABASE: {len(existing_deals)} deals already saved. Do NOT re-save these:
{recent}

YOUR MISSION: Find NEW deals not listed above and save them using save_deal().

SEARCH WINDOW: {window_str}

## CRITICAL RULES — READ CAREFULLY

**SAVE AGGRESSIVELY.** After EVERY search, immediately call save_deal() for each deal found.
Do NOT wait to gather more info. Do NOT skip a deal because some fields are uncertain.
Use "Not disclosed" for any financial fields you cannot find.

**SAVE AFTER EVERY SEARCH.** The pattern must be:
  search → save all deals found → search → save all deals found → ... → finish()

**MINIMUM 2 SEARCHES before calling finish(). Target 5-8 searches total.**

**ONLY save deals where a Chinese company is a party** (Chinese biotech licensing OUT to Western pharma,
or Western pharma licensing IN from China, or M&A involving Chinese company).

**For each deal, fill in what you know:**
- announcement_month_year: "Month YYYY" (e.g. "March 2024") — use the article date if deal date unclear
- deal_type: licensing-out, licensing-in, M&A, partnership, co-development, acquisition
- chinese_party: the Chinese company name
- foreign_party: the non-Chinese company, or "N/A"
- asset: drug name or target (e.g. "ivonescimab", "TROP2 ADC SHR-A1921")
- modality: drug class — Small Molecule / Monoclonal Antibody / Bispecific Antibody / ADC / Cell Therapy / Gene Therapy / siRNA / mRNA / Fusion Protein / Peptide / Oligonucleotide / Other
- therapeutic_area: specific (e.g. "Oncology – NSCLC", "Immunology – Autoimmune")
- stage: Preclinical / Phase 1 / Phase 2 / Phase 3 / Approved / Platform
- total_value_usd: e.g. "$1.2B" or "Not disclosed"
- upfront_usd: e.g. "$100M" or "Not disclosed"
- territory: e.g. "Global ex-China", "US & Europe", "Worldwide"
- highlights: 2-3 sentences — why notable? strategic rationale? standout terms?
- source_url: the article URL
- source_name: FierceBiotech / Endpoints News / BioPharma Dive / Reuters / Bloomberg / STAT News

## EXAMPLE WORKFLOW (follow this pattern exactly):

Step 1: search_web("China biotech licensing deal 2025 fiercebiotech")
Step 2: save_deal({{...}}) for EACH deal mentioned in results
Step 3: search_web("Chinese pharma ADC out-licensing 2024 2025")
Step 4: save_deal({{...}}) for EACH new deal found
... repeat ...
Final step: finish(summary="Found and saved X deals")

Call finish() when you have done at least 5 searches."""

# ── Agent Loop ─────────────────────────────────────────────────────────────────

def trim_messages(messages: list, keep_first: int = 1, keep_last: int = 6) -> list:
    """
    Sliding context window — keeps the conversation short to avoid token buildup.

    How it works:
      - Always keeps the FIRST message (the original user goal)
      - Always keeps the most recent N message-pairs (keep_last)
      - Drops everything in between
      - Special rule: never split a tool_use / tool_result pair mid-trim,
        because that would cause the same 400 error we fixed earlier

    Why this is safe for our agent:
      - The system prompt already contains the full list of saved deals,
        so Claude doesn't need old search results in the message history
      - Each new search is self-contained — old results are just dead weight
    """
    if len(messages) <= keep_first + keep_last:
        return messages

    head = messages[:keep_first]
    tail = messages[-(keep_last):]

    # Make sure tail doesn't start mid tool_use/tool_result pair.
    # A tool_result user message must always follow its tool_use assistant message.
    # If tail[0] is a tool_result, back up one more to include the tool_use.
    if tail and isinstance(tail[0].get("content"), list):
        first_content = tail[0].get("content", [])
        if first_content and isinstance(first_content[0], dict):
            if first_content[0].get("type") == "tool_result":
                # Back up one message to include the matching tool_use
                idx = len(messages) - keep_last
                if idx > keep_first:
                    tail = messages[idx - 1:]

    trimmed = head + tail
    dropped = len(messages) - len(trimmed)
    if dropped > 0:
        print(f"  [Context trim: dropped {dropped} old messages, keeping {len(trimmed)} total]")
    return trimmed


def run_agent(goal: str, search_window: str = ""):
    print(f"\n{'='*60}")
    print(f"  CHINA BIOPHARMA DEAL AGENT  v2")
    print(f"  Existing deals in DB: {len(db['deals'])}")
    print(f"{'='*60}\n")

    messages = [{"role": "user", "content": goal}]
    step     = 0
    done     = False

    while step < MAX_STEPS and not done:
        step += 1
        print(f"[Step {step}] Calling Claude...")

        # Trim context window BEFORE each API call to keep token count low.
        # We keep the original goal + the last 6 messages (3 round-trips).
        # The system prompt holds all saved-deal context so nothing is lost.
        trimmed_messages = trim_messages(messages, keep_first=1, keep_last=6)

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=build_system_prompt(db["deals"], search_window),
                tools=TOOLS,
                messages=trimmed_messages
            )

        except anthropic.RateLimitError as e:
            # 429 — hit the per-minute token limit
            print(f"\n  [Rate limit hit: {e}]")
            print(f"  Waiting 60 seconds before retrying...")
            import time
            time.sleep(62)          # wait just over a minute for the window to reset
            print(f"  Retrying step {step}...")
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=build_system_prompt(db["deals"], search_window),
                    tools=TOOLS,
                    messages=trimmed_messages
                )
            except anthropic.RateLimitError as e2:
                print(f"  [Rate limit hit again after retry — saving what we have and stopping]")
                print(f"  Error: {e2}")
                break

        except anthropic.APIError as e:
            print(f"\n  [API error at step {step}: {e}]")
            print(f"  Saving what we have and stopping.")
            break

        # Always record the full assistant turn in history
        messages.append({"role": "assistant", "content": response.content})

        # Collect every tool_use block regardless of stop_reason
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            print("  [No tool calls — agent done]")
            break

        # Execute all tools and collect results in one batch
        tool_results  = []
        called_finish = False

        for block in tool_use_blocks:
            print(f"  -> {block.name}({str(block.input)[:120]}...)")
            try:
                result = run_tool(block.name, block.input)
            except Exception as e:
                result = f"Tool error: {e}"

            print(f"  <- {str(result)[:160]}")

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     str(result)
            })

            if block.name == "finish":
                called_finish = True

        # Send ALL results back as one user message
        messages.append({"role": "user", "content": tool_results})

        if called_finish:
            done = True

    save_database(db, mark_run=True)   # stamp last_run_date after each agent run
    print(f"\n{'='*60}")
    print(f"  New deals this run:      {len(new_deals_this_run)}")
    print(f"  Total deals in database: {len(db['deals'])}")
    print(f"{'='*60}\n")

# ── Dashboard Generator ────────────────────────────────────────────────────────

def generate_dashboard(db: dict, search_window: str = ""):
    deals        = db["deals"]
    updated      = (db.get("last_updated") or "")[:16].replace("T", " ")
    last_run     = db.get("last_run_date", "Never")
    window_disp  = search_window if search_window else f"Since {last_run}" if last_run != "Never" else "All time"
    total        = len(deals)

    # ── Aggregations ─────────────────────────────────────────────────────────
    types           = {}
    areas           = {}
    chinese_parties = {}
    foreign_parties = {}
    months_raw      = {}

    for d in deals:
        t  = d.get("deal_type", "Other");        types[t]            = types.get(t, 0) + 1
        a  = d.get("therapeutic_area", "Other"); areas[a]            = areas.get(a, 0) + 1
        cp = d.get("chinese_party", "?");        chinese_parties[cp] = chinese_parties.get(cp, 0) + 1
        fp = d.get("foreign_party", "")
        if fp and fp.upper() not in ("N/A", "NA", "NONE", ""):
            foreign_parties[fp] = foreign_parties.get(fp, 0) + 1
        m  = d.get("announcement_month_year", "")
        if m:
            months_raw[m] = months_raw.get(m, 0) + 1

    # Sort months chronologically
    MONTH_ORDER = ["January","February","March","April","May","June",
                   "July","August","September","October","November","December"]
    def month_sort_key(label):
        try:
            parts = label.strip().split()
            if len(parts) == 2:
                mn, yr = parts
                mi = MONTH_ORDER.index(mn) if mn in MONTH_ORDER else 0
                return (int(yr), mi)
        except (ValueError, IndexError):
            pass
        return (9999, 0)  # malformed dates sort to the end

    def fmt_date(label):
        """Convert 'Month YYYY' -> 'YY/MM', e.g. 'March 2024' -> '24/03'."""
        try:
            parts = label.strip().split()
            if len(parts) == 2:
                mn, yr = parts
                mi = MONTH_ORDER.index(mn) + 1 if mn in MONTH_ORDER else 0
                return f"{yr[2:]}/{mi:02d}"
        except (ValueError, IndexError):
            pass
        return label  # return as-is if malformed

    months_sorted = sorted(months_raw.items(), key=lambda x: month_sort_key(x[0]))
    month_labels  = [fmt_date(x[0]) for x in months_sorted]   # YY/MM for chart axis
    month_counts  = [x[1] for x in months_sorted]

    # Broad therapeutic area grouping, capped at 9
    def broad_area(a):
        a_lower = a.lower()
        if "oncology"    in a_lower: return "Oncology"
        if "immunology"  in a_lower: return "Immunology"
        if "cardio"      in a_lower: return "Cardiovascular"
        if "metabolic"   in a_lower or "obesity" in a_lower: return "Metabolic"
        if "respiratory" in a_lower: return "Respiratory"
        if "cns"         in a_lower or "neuro"   in a_lower: return "CNS / Neurology"
        if "rare"        in a_lower: return "Rare Disease"
        return "Other"

    broad_areas = {}
    for a in areas:
        b = broad_area(a)
        broad_areas[b] = broad_areas.get(b, 0) + areas[a]
    broad_areas = dict(sorted(broad_areas.items(), key=lambda x: -x[1])[:9])

    top_area = max(broad_areas,     key=broad_areas.get)     if broad_areas     else "—"
    top_type = max(types,           key=types.get)           if types           else "—"
    top_cp   = max(chinese_parties, key=chinese_parties.get) if chinese_parties else "—"

    top_cn = sorted(chinese_parties.items(), key=lambda x: x[1], reverse=True)[:9]
    top_fp = sorted(foreign_parties.items(),  key=lambda x: x[1], reverse=True)[:9]

    # Set1-inspired palette
    SET1 = ["#E8433A","#4A90D9","#5BBF5A","#9B5FC0",
            "#FF8C1A","#F5D63D","#C47035","#F07DB0","#8A9BB0"]
    def palette(n):
        return json.dumps([SET1[i % len(SET1)] for i in range(n)])

    def badge(t):
        colors = {
            "licensing-out":  SET1[0], "licensing-in":  SET1[1],
            "M&A":            SET1[4], "partnership":   SET1[2],
            "co-development": SET1[3], "acquisition":   SET1[7],
        }
        c = colors.get(t, SET1[8])
        return f'<span class="badge" style="background:{c}28;color:{c};border:1px solid {c}66">{t}</span>'

    def src(name, url):
        icon = {"FierceBiotech":"🔥","Endpoints News":"📍","BioPharma Dive":"🌊",
                "Reuters":"📰","Bloomberg":"📊","STAT News":"⚡"}.get(name,"🔗")
        return f'<a href="{url}" target="_blank" class="src-chip">{icon} {name}</a>'

    def esc_html(v):
        return (str(v) if v else "—").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&#39;")

    # Chart pixel dimensions — fixed, so Chart.js never needs to measure DOM
    CHART_W   = 480   # fits inside a 50% grid card at typical screen width
    BAR_H     = 20    # px per horizontal bar row — small enough to avoid scroll
    MONTH_H   = 18    # px per month bar
    MIN_H     = 120
    MAX_H     = 220   # hard cap — never taller than the scroll container

    area_h    = min(MAX_H, max(MIN_H, len(broad_areas)  * BAR_H  + 30))
    cn_h      = min(MAX_H, max(MIN_H, len(top_cn)       * BAR_H  + 30))
    fp_h      = min(MAX_H, max(MIN_H, len(top_fp)       * BAR_H  + 30))
    month_h   = min(MAX_H, max(MIN_H, len(month_labels) * MONTH_H + 40))

    # Table rows
    rows = "".join(
        f'<tr class="dr" data-type="{esc_html(d.get("deal_type",""))}" '
        f'data-area="{esc_html(d.get("therapeutic_area",""))}" data-idx="{i}" style="cursor:default">'
        f'<td class="dc">{fmt_date(d.get("announcement_month_year","—"))}</td>'
        f'<td><strong>{esc_html(d.get("chinese_party","—"))}</strong></td>'
        f'<td>{esc_html(d.get("foreign_party","—"))}</td>'
        f'<td class="ac">{esc_html(d.get("asset","—"))}</td>'
        f'<td><span class="atag">{esc_html(d.get("therapeutic_area","—"))}</span></td>'
        f'<td>{badge(d.get("deal_type","Other"))}</td>'
        f'<td>{esc_html(d.get("stage","—"))}</td>'
        f'<td><span class="mod-tag">{esc_html(d.get("modality","—"))}</span></td>'
        f'<td class="vc">{esc_html(d.get("total_value_usd","—"))}</td>'
        f'<td>{esc_html(d.get("upfront_usd","—"))}</td>'
        f'<td>{esc_html(d.get("territory","—"))}</td>'
        f'<td>{src(d.get("source_name","Source"), d.get("source_url","#"))}</td>'
        f'<td class="hlc"><span class="hl-text" title="{esc_html(d.get("highlights","—"))}">'
        f'{esc_html(d.get("highlights","—"))}</span>'
        f'<div class="hl-pop">{esc_html(d.get("highlights","—"))}</div></td>'
        f'</tr>'
        for i, d in enumerate(reversed(deals))
    )

    # Chart data as JSON
    broad_labels_js = json.dumps(list(broad_areas.keys()))
    broad_values_js = json.dumps(list(broad_areas.values()))
    cn_labels_js    = json.dumps([x[0] for x in top_cn])
    cn_values_js    = json.dumps([x[1] for x in top_cn])
    fp_labels_js    = json.dumps([x[0] for x in top_fp])
    fp_values_js    = json.dumps([x[1] for x in top_fp])
    month_labels_js = json.dumps(month_labels)
    month_values_js = json.dumps(month_counts)
    set1_js         = json.dumps(SET1)
    area_pal_js     = palette(len(broad_areas))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>China Biopharma Deal Tracker</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{{
  --bg:#0a0c10;--surf:#10141c;--brd:#1d2535;
  --r:{SET1[0]};--b:{SET1[1]};--g:{SET1[2]};--p:{SET1[3]};--o:{SET1[4]};
  --txt:#dde3ee;--mut:#5a6a82;
  --head:'Syne',sans-serif;--mono:'DM Mono',monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:13px;line-height:1.6;min-height:100vh}}

/* Header */
.hdr{{padding:28px 48px 18px;border-bottom:1px solid var(--brd);display:flex;justify-content:space-between;align-items:flex-end;background:linear-gradient(170deg,#0f1520 0%,var(--bg) 100%)}}
.hdr h1{{font-family:var(--head);font-size:1.8rem;font-weight:800;letter-spacing:-.03em;color:var(--txt)}}
.hdr h1 span{{color:var(--r)}}
.hdr p{{color:var(--mut);font-size:12px;margin-top:3px}}
.hdr-meta{{text-align:right;color:var(--mut);font-size:11px;line-height:2.1}}
.hdr-meta strong{{color:var(--txt)}}
.hdr-meta code{{background:#ffffff0f;padding:2px 7px;border-radius:3px;font-size:11px;color:var(--g)}}

/* KPIs */
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--brd);border-bottom:1px solid var(--brd)}}
.kpi{{background:var(--surf);padding:16px 24px;position:relative;overflow:hidden}}
.kpi::after{{content:'';position:absolute;bottom:0;left:0;right:0;height:2px}}
.kpi:nth-child(1)::after{{background:var(--r)}}
.kpi:nth-child(2)::after{{background:var(--b)}}
.kpi:nth-child(3)::after{{background:var(--g)}}
.kpi:nth-child(4)::after{{background:var(--o)}}
.kpi-val{{font-family:var(--head);font-size:2rem;font-weight:800;letter-spacing:-.04em;line-height:1}}
.kpi:nth-child(1) .kpi-val{{color:var(--r)}}
.kpi:nth-child(2) .kpi-val{{color:var(--b)}}
.kpi:nth-child(3) .kpi-val{{color:var(--g)}}
.kpi:nth-child(4) .kpi-val{{color:var(--o)}}
.kpi-lbl{{color:var(--mut);font-size:10px;margin-top:5px;letter-spacing:.08em;text-transform:uppercase}}
.kpi-sub{{color:var(--txt);font-size:12px;margin-top:3px;opacity:.7}}

/* Charts — 2x2 grid, each card scrolls internally */
.charts-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--brd);border-bottom:1px solid var(--brd)}}
.chart-card{{background:var(--surf);padding:18px 22px}}
.chart-card h3{{font-family:var(--head);font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.pill{{background:#ffffff10;border-radius:20px;padding:1px 8px;font-size:10px;color:var(--txt)}}
/* KEY FIX: chart wrapper scrolls; canvas has explicit px size set by JS */
.chart-wrap{{overflow:hidden;height:220px}}
.chart-wrap canvas{{display:block}}

/* Filters */
.filters{{padding:10px 48px;display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--brd);background:var(--surf);flex-wrap:wrap}}
.filters label{{color:var(--mut);font-size:10px;letter-spacing:.07em;text-transform:uppercase}}
.filters select,.filters input{{background:var(--bg);border:1px solid var(--brd);color:var(--txt);padding:5px 10px;font-family:var(--mono);font-size:12px;border-radius:4px;outline:none}}
.filters select:focus,.filters input:focus{{border-color:var(--b)}}
.filters input{{min-width:200px}}
#rc{{color:var(--mut);font-size:11px;margin-left:auto}}

/* Table */
.tw{{overflow-x:scroll;padding-bottom:58px;-webkit-overflow-scrolling:touch}}
table{{width:max-content;min-width:100%;border-collapse:collapse;table-layout:fixed}}
thead tr{{background:var(--surf);border-bottom:2px solid var(--brd)}}
th{{padding:5px 8px;text-align:left;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);font-weight:500;white-space:nowrap;cursor:pointer;user-select:none}}
th:hover{{color:var(--txt)}}
tbody tr{{border-bottom:1px solid var(--brd);transition:background .1s}}
tbody tr:hover{{background:#ffffff08}}
tbody tr.hidden{{display:none}}
td{{padding:5px 8px;vertical-align:middle;overflow:hidden}}
.dc{{color:var(--b);white-space:nowrap;font-size:12px}}
.ac{{color:var(--g);font-weight:500;white-space:normal;word-break:break-word}}
.vc{{color:var(--o);font-weight:500;white-space:normal;word-break:break-word}}
.badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;white-space:nowrap;font-weight:500}}
.atag{{display:inline-block;background:#ffffff08;border:1px solid var(--brd);padding:2px 6px;border-radius:4px;font-size:11px;white-space:normal;word-break:break-word;line-height:1.4}}
.mod-tag{{display:inline-block;background:#9B5FC020;border:1px solid #9B5FC066;color:#c49fe0;padding:2px 7px;border-radius:4px;font-size:11px;white-space:normal;word-break:break-word;line-height:1.4}}
.src-chip{{display:inline-flex;align-items:center;gap:4px;background:#ffffff06;border:1px solid var(--brd);padding:3px 9px;border-radius:20px;font-size:11px;text-decoration:none;color:var(--txt);white-space:nowrap;transition:border-color .12s,color .12s}}
.src-chip:hover{{border-color:var(--r);color:var(--r)}}

/* Highlights cell — 2-line clamp + hover popover */
.hlc{{position:relative;max-width:480px;min-width:340px}}
.hl-text{{
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;
  overflow:hidden;color:#8a9bb8;font-size:11px;line-height:1.5;
  cursor:default;
}}
.hl-pop{{
  display:none;
  position:absolute;
  bottom:calc(100% + 6px);
  left:0;
  width:420px;
  background:#1a2030;
  border:1px solid var(--brd);
  border-radius:6px;
  padding:12px 14px;
  font-size:12px;
  line-height:1.7;
  color:#c8d4e8;
  z-index:200;
  box-shadow:0 8px 32px #00000088;
  pointer-events:none;
  white-space:normal;
  word-break:break-word;
}}
/* flip popover to below if row is near top */
.hlc:hover .hl-pop{{display:block}}
tr:nth-child(-n+3) .hl-pop{{bottom:auto;top:calc(100% + 6px)}}

/* Footer */
.foot{{position:fixed;bottom:0;left:0;right:0;background:var(--surf);border-top:1px solid var(--brd);padding:7px 48px;display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--mut);z-index:10}}
.dot{{width:6px;height:6px;border-radius:50%;background:var(--g);display:inline-block;margin-right:6px;animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.25}}}}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div>
    <h1>China <span>Biopharma</span> Deal Tracker</h1>
    <p>Automated intelligence on life science transactions involving Chinese companies</p>
  </div>
  <div class="hdr-meta">
    <div>Last updated &nbsp;<strong>{updated}</strong></div>
    <div>Last agent run &nbsp;<strong>{last_run}</strong></div>
    <div>Search window &nbsp;<strong>{window_disp}</strong></div>
    <div>Refresh &nbsp;<code>python biopharma_agent.py</code></div>
  </div>
</div>

<!-- KPIs -->
<div class="kpis">
  <div class="kpi"><div class="kpi-val">{total}</div><div class="kpi-lbl">Total Deals</div><div class="kpi-sub">in database</div></div>
  <div class="kpi"><div class="kpi-val" style="font-size:1.1rem">{top_area}</div><div class="kpi-lbl">Top Therapeutic Area</div><div class="kpi-sub">{broad_areas.get(top_area,0)} deals</div></div>
  <div class="kpi"><div class="kpi-val" style="font-size:1.1rem">{top_type}</div><div class="kpi-lbl">Most Common Deal Type</div><div class="kpi-sub">{types.get(top_type,0)} deals</div></div>
  <div class="kpi"><div class="kpi-val" style="font-size:.9rem">{top_cp}</div><div class="kpi-lbl">Most Active Chinese Party</div><div class="kpi-sub">{chinese_parties.get(top_cp,0)} deals</div></div>
</div>

<!-- Charts 2x2 -->
<div class="charts-grid">
  <div class="chart-card">
    <h3>Deals by Therapeutic Area <span class="pill">{len(broad_areas)} categories</span></h3>
    <div class="chart-wrap" id="wrap-aC">
      <canvas id="aC"></canvas>
    </div>
  </div>
  <div class="chart-card">
    <h3>Deals by Month <span class="pill">{len(month_labels)} months</span></h3>
    <div class="chart-wrap" id="wrap-mC">
      <canvas id="mC"></canvas>
    </div>
  </div>
  <div class="chart-card">
    <h3>Top Chinese Parties <span class="pill">top {len(top_cn)}</span></h3>
    <div class="chart-wrap" id="wrap-cnC">
      <canvas id="cnC"></canvas>
    </div>
  </div>
  <div class="chart-card">
    <h3>Top Foreign Parties <span class="pill">top {len(top_fp)}</span></h3>
    <div class="chart-wrap" id="wrap-fpC">
      <canvas id="fpC"></canvas>
    </div>
  </div>
</div>

<!-- Filters -->
<div class="filters">
  <label>Filter</label>
  <select id="tf" onchange="filt()">
    <option value="">All Types</option>
    {''.join(f'<option value="{t}">{t}</option>' for t in sorted(types))}
  </select>
  <select id="af" onchange="filt()">
    <option value="">All Areas</option>
    {''.join(f'<option value="{a}">{a}</option>' for a in sorted(areas))}
  </select>
  <input id="sf" type="text" placeholder="Search company, asset, highlights..." oninput="filt()">
  <span id="rc">{total} deals</span>
</div>

<!-- Table -->
<div style="display:flex;justify-content:flex-end;padding:6px 14px 2px;background:var(--bg)">
  <button onclick="exportCSV()" style="background:none;border:1px solid var(--brd);color:var(--mut);font-family:var(--mono);font-size:11px;padding:4px 12px;border-radius:4px;cursor:pointer" onmouseover="this.style.color='var(--txt)';this.style.borderColor='var(--txt)'" onmouseout="this.style.color='var(--mut)';this.style.borderColor='var(--brd)'">&#8595; Export CSV</button>
</div>
<div class="tw">
  <table>
    <colgroup>
      <col style="width:110px">
      <col style="width:155px">
      <col style="width:145px">
      <col style="width:215px">
      <col style="width:155px">
      <col style="width:120px">
      <col style="width:115px">
      <col style="width:130px">
      <col style="width:90px">
      <col style="width:80px">
      <col style="width:125px">
      <col style="width:135px">
      <col style="width:480px">
    </colgroup>
    <thead><tr>
      <th onclick="srt(0)">Date ↕</th>
      <th onclick="srt(1)">Chinese Party ↕</th>
      <th onclick="srt(2)">Foreign Party ↕</th>
      <th onclick="srt(3)">Asset ↕</th>
      <th onclick="srt(4)">Therapeutic Area ↕</th>
      <th onclick="srt(5)">Deal Type ↕</th>
      <th onclick="srt(6)">Stage ↕</th>
      <th onclick="srt(7)">Modality ↕</th>
      <th onclick="srt(8)">Total Value ↕</th>
      <th onclick="srt(9)">Upfront ↕</th>
      <th onclick="srt(10)">Territory ↕</th>
      <th>Source</th>
      <th onclick="srt(12)">Highlights (hover to expand)</th>
    </tr></thead>
    <tbody id="dt">{rows}</tbody>
  </table>
</div>

<div class="foot">
  <div><span class="dot"></span>Hover highlights cell to read full text</div>
  <div>China Biopharma Deal Tracker &bull; Powered by Claude</div>
  <div>{total} deals tracked</div>
</div>

<script>
const S1  = {set1_js};
const gc  = '#1d2535', tc = '#5a6a82';

// ── KEY FIX: set canvas px size explicitly before creating chart ──────────────
// Chart.js responsive mode measures the DOM parent, which breaks inside
// overflow:auto containers. Instead we disable responsive mode and set
// width/height directly on the canvas element, then let the wrapper scroll.
function makeCanvas(id, w, h) {{
  const c = document.getElementById(id);
  c.width  = w;
  c.height = h;
  c.style.width  = w + 'px';
  c.style.height = h + 'px';
  return c;
}}

function pal(n) {{
  return Array.from({{length:n}}, (_,i) => S1[i % S1.length]);
}}

const CHART_W = document.querySelector('.chart-wrap').parentElement.clientWidth - 44 || 460;

// Therapeutic Area — horizontal bar
new Chart(makeCanvas('aC', CHART_W, {area_h}), {{
  type: 'bar',
  data: {{
    labels: {broad_labels_js},
    datasets: [{{ data: {broad_values_js},
      backgroundColor: pal({len(broad_areas)}).map(c=>c+'99'),
      borderColor: pal({len(broad_areas)}),
      borderWidth:1, borderRadius:3, borderSkipped:false }}]
  }},
  options: {{
    responsive: false, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: {{ display:false }} }},
    scales: {{
      x: {{ ticks:{{color:tc,font:{{size:10}},stepSize:1}}, grid:{{color:gc}} }},
      y: {{ ticks:{{color:'#dde3ee',font:{{size:11}}}}, grid:{{color:'transparent'}} }}
    }}
  }}
}});

// Deals by Month — vertical bar
new Chart(makeCanvas('mC', CHART_W, {month_h}), {{
  type: 'bar',
  data: {{
    labels: {month_labels_js},
    datasets: [{{ data: {month_values_js},
      backgroundColor: S1[1]+'99', borderColor: S1[1],
      borderWidth:1, borderRadius:3, borderSkipped:false }}]
  }},
  options: {{
    responsive: false, maintainAspectRatio: false,
    plugins: {{ legend: {{ display:false }} }},
    scales: {{
      x: {{ ticks:{{color:tc,font:{{size:10}},maxRotation:45,minRotation:30}}, grid:{{color:gc}} }},
      y: {{ ticks:{{color:tc,font:{{size:10}},stepSize:1}}, grid:{{color:gc}} }}
    }}
  }}
}});

// Top Chinese Parties — horizontal bar
new Chart(makeCanvas('cnC', CHART_W, {cn_h}), {{
  type: 'bar',
  data: {{
    labels: {cn_labels_js},
    datasets: [{{ data: {cn_values_js},
      backgroundColor: S1[0]+'88', borderColor: S1[0],
      borderWidth:1, borderRadius:3, borderSkipped:false }}]
  }},
  options: {{
    responsive: false, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: {{ display:false }} }},
    scales: {{
      x: {{ ticks:{{color:tc,font:{{size:10}},stepSize:1}}, grid:{{color:gc}} }},
      y: {{ ticks:{{color:'#dde3ee',font:{{size:11}}}}, grid:{{color:'transparent'}} }}
    }}
  }}
}});

// Top Foreign Parties — horizontal bar
new Chart(makeCanvas('fpC', CHART_W, {fp_h}), {{
  type: 'bar',
  data: {{
    labels: {fp_labels_js},
    datasets: [{{ data: {fp_values_js},
      backgroundColor: S1[1]+'88', borderColor: S1[1],
      borderWidth:1, borderRadius:3, borderSkipped:false }}]
  }},
  options: {{
    responsive: false, maintainAspectRatio: false,
    indexAxis: 'y',
    plugins: {{ legend: {{ display:false }} }},
    scales: {{
      x: {{ ticks:{{color:tc,font:{{size:10}},stepSize:1}}, grid:{{color:gc}} }},
      y: {{ ticks:{{color:'#dde3ee',font:{{size:11}}}}, grid:{{color:'transparent'}} }}
    }}
  }}
}});

// ── Filter ────────────────────────────────────────────────────────────────────
function filt() {{
  const tv = document.getElementById('tf').value.toLowerCase();
  const av = document.getElementById('af').value.toLowerCase();
  const sv = document.getElementById('sf').value.toLowerCase();
  let n = 0;
  document.querySelectorAll('#dt .dr').forEach(r => {{
    const ok = (!tv || r.dataset.type.toLowerCase() === tv)
            && (!av || r.dataset.area.toLowerCase() === av)
            && (!sv || r.textContent.toLowerCase().includes(sv));
    r.classList.toggle('hidden', !ok);
    if (ok) n++;
  }});
  document.getElementById('rc').textContent = n + ' deals';
}}

// ── Sort ──────────────────────────────────────────────────────────────────────
let sd = {{}};
function srt(c) {{
  const tb   = document.getElementById('dt');
  const rows = [...tb.querySelectorAll('.dr')];
  sd[c] = !sd[c];
  rows.sort((a,b) => {{
    const A = a.cells[c]?.textContent.trim() || '';
    const B = b.cells[c]?.textContent.trim() || '';
    return sd[c] ? A.localeCompare(B) : B.localeCompare(A);
  }});
  rows.forEach(r => tb.appendChild(r));
}}

// ── CSV Export ────────────────────────────────────────────────────────────────
const DEALS_DATA = {json.dumps([{
    "date":      d.get("announcement_month_year",""),
    "cp":        d.get("chinese_party",""),
    "fp":        d.get("foreign_party",""),
    "asset":     d.get("asset",""),
    "area":      d.get("therapeutic_area",""),
    "type":      d.get("deal_type",""),
    "stage":     d.get("stage",""),
    "modality":  d.get("modality",""),
    "value":     d.get("total_value_usd",""),
    "upfront":   d.get("upfront_usd",""),
    "territory": d.get("territory",""),
    "highlights":d.get("highlights",""),
    "src_name":  d.get("source_name",""),
    "src_url":   d.get("source_url",""),
} for d in reversed(deals)])};

function exportCSV() {{
  const cols = ['Date','Chinese Party','Foreign Party','Asset','Therapeutic Area',
                'Deal Type','Stage','Modality','Total Value','Upfront','Territory','Highlights','Source','Source URL'];
  const esc  = v => '"' + String(v||'').replace(/"/g,'""') + '"';
  const rows = DEALS_DATA.map(d => [
    d.date,d.cp,d.fp,d.asset,d.area,d.type,d.stage,d.modality||'',
    d.value,d.upfront,d.territory,d.highlights,d.src_name,d.src_url
  ].map(esc).join(','));
  const csv  = [cols.map(esc).join(','),...rows].join('\\n');
  const blob = new Blob([csv],{{type:'text/csv'}});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'china_biopharma_deals.csv';
  a.click();
}}
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard written -> {DASHBOARD_PATH.resolve()}")

# ── CSV Export ────────────────────────────────────────────────────────────────

def generate_csv(db: dict):
    """Write all deals to a CSV file with every column including highlights."""
    import csv
    deals = db["deals"]
    cols  = [
        "announcement_month_year", "deal_type", "chinese_party", "foreign_party",
        "asset", "therapeutic_area", "stage", "total_value_usd", "upfront_usd",
        "territory", "modality", "highlights", "source_name", "source_url"
    ]
    headers = [
        "Date", "Deal Type", "Chinese Party", "Foreign Party",
        "Asset", "Therapeutic Area", "Stage", "Total Value (USD)", "Upfront (USD)",
        "Territory", "Modality", "Highlights", "Source", "Source URL"
    ]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for d in deals:
            writer.writerow([d.get(c, "") for c in cols])
    print(f"CSV written     -> {CSV_PATH.resolve()}")


# ── Entry Point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="China Biopharma Deal Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python biopharma_agent.py              # auto window: search from last run date
  python biopharma_agent.py -r 2025      # search only 2025 deals
  python biopharma_agent.py -r 2024 2025 # search 2024 and 2025 deals
  python biopharma_agent.py -n           # skip search, regenerate dashboard only
        """
    )
    parser.add_argument(
        "-n", "--no-query",
        action="store_true",
        help="Skip the agent search — only regenerate dashboard.html from existing database"
    )
    parser.add_argument(
        "-r", "--range",
        nargs="+",
        metavar="YEAR",
        help="Override search window with explicit year(s), e.g. -r 2025 or -r 2024 2025"
    )
    args = parser.parse_args()

    # ── Determine search window ───────────────────────────────────────────────
    db = load_database()
    last_run = db.get("last_run_date")   # e.g. "2025-07-15" or None

    if args.range:
        # User explicitly specified years, e.g. -r 2024 2025
        years = sorted(set(args.range))
        if len(years) == 1:
            search_window = f"Only find deals announced in {years[0]}."
            window_label  = years[0]
        else:
            year_list = ", ".join(years[:-1]) + f" and {years[-1]}"
            search_window = f"Only find deals announced in {year_list}."
            window_label  = " & ".join(years)
        print(f"\n  [-r] Manual search window: {year_list if len(years) > 1 else years[0]}")

    elif last_run:
        # Auto: search from the day after last run
        from datetime import date, timedelta
        last_dt   = datetime.strptime(last_run, "%Y-%m-%d").date()
        since     = (last_dt + timedelta(days=1)).strftime("%B %-d, %Y")
        since_yr  = (last_dt + timedelta(days=1)).strftime("%Y")
        search_window = (
            f"Only find deals announced on or after {since} "
            f"(your database was last updated on {last_run}). "
            f"Do NOT waste searches on deals from before {since}."
        )
        window_label = f"Since {last_run}"
        print(f"\n  [Auto window] Searching for deals since last run: {last_run}")

    else:
        # First run ever — no restriction
        search_window = "This is the first run. Search broadly across 2024 and 2025."
        window_label  = "All time (first run)"
        print("\n  [First run] No prior run date — searching all of 2024–2025")

    RESEARCH_GOAL = (
        "Search for NEW biopharma deals involving Chinese biotech or pharma companies. "
        "Focus on licensing, partnerships, M&A, co-development. "
        "Prioritize FierceBiotech, Endpoints News, BioPharma Dive. "
        "Run at least 5 different searches with varied queries."
    )

    if args.no_query:
        print("\n  [-n] Skipping search — regenerating dashboard from existing database...")
        search_window = db.get("last_search_window", search_window)
    else:
        # Persist the current search window so -n can re-display it
        db["last_search_window"] = window_label
        save_database(db)
        try:
            run_agent(RESEARCH_GOAL, search_window)
        except KeyboardInterrupt:
            print("\n  [Interrupted by user]")
        except Exception as e:
            print(f"\n  [Unexpected error: {e}]")
            print("  Saving dashboard with whatever was collected...")

    # Always regenerate dashboard
    db = load_database()
    generate_dashboard(db, window_label if not args.no_query else db.get("last_search_window", "—"))
    generate_csv(db)
    print(f"\n  Open dashboard : {DASHBOARD_PATH.resolve()}")
    print(f"  Raw data       : {DB_PATH.resolve()}")
    print(f"  Deals in DB    : {len(db['deals'])}\n")
