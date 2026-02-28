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
import re
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
MAX_STEPS = 60   # ~15 searches × (1 search + 3 saves avg) = 60 steps for a full run

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

# Chinese-language biopharma news sources
CHINESE_SOURCES = [
    "healthnews.cn",          # 健康报 — official pharma/health news
    "phirda.com",             # 医药魔方 — deal tracking, very comprehensive
    "pharmacodia.com",        # 药渡 — drug pipeline & deal database
    "cn-healthcare.com",      # 健康界
    "drugscitech.com",        # 药学进展
    "menet.com.cn",           # 医药经济报
    "bioon.com",              # 生物谷 — biopharma news
    "biocentury.com.cn",      # BioWorld China
    "vbdata.cn",              # 动脉网 — Chinese biotech deals
    "synbiobeta.com",
    "szse.cn",                # Shenzhen Stock Exchange disclosures
    "sse.com.cn",             # Shanghai Stock Exchange disclosures
]

# ── Canonical field enums — single source of truth ────────────────────────────
# These are referenced by: TOOLS schema, prompt field guides, and runtime
# normalizers. Edit here only — never hardcode values elsewhere.

DEAL_TYPE_ENUM = [
    "licensing-out", "licensing-in", "option-to-license",
    "newco-spinout", "platform-deal", "co-development",
    "partnership", "M&A", "acquisition",
]

MODALITY_ENUM = [
    "Small Molecule", "Monoclonal Antibody", "Bispecific Antibody", "ADC",
    "Cell Therapy", "Gene Therapy", "siRNA", "mRNA",
    "Fusion Protein", "Peptide", "Oligonucleotide", "Other",
]

STAGE_ENUM = [
    "Preclinical", "Phase 1", "Phase 2", "Phase 3", "Approved", "Platform",
]

TA_ENUM = [
    "Oncology – Solid Tumors", "Oncology – NSCLC", "Oncology – Breast Cancer",
    "Oncology – Gastrointestinal Cancer", "Oncology – Lymphoma / Leukemia",
    "Oncology – Ovarian Cancer", "Oncology – Neuroendocrine Tumors",
    "Oncology – Multiple Indications",
    "Immunology – Atopic Dermatitis", "Immunology – Inflammatory Bowel Disease",
    "Immunology – Lupus / Nephrology", "Immunology – Asthma / Allergic Disorders",
    "Immunology – Psoriasis / Inflammatory", "Immunology – Multiple Indications",
    "Metabolic – Obesity", "Metabolic – Diabetes",
    "Metabolic – Cardiometabolic", "Metabolic – MASH / Liver",
    "Cardiovascular – Dyslipidemia", "Cardiovascular – Cardiometabolic",
    "Nephrology – IgA Nephropathy", "Nephrology – Other",
    "Respiratory – Asthma", "Respiratory – Other",
    "Women's Health", "RNA Therapeutics – Platform",
    "Multiple Indications", "Not Disclosed",
]

TERRITORY_ENUM = [
    "Global", "Global ex-China", "Global ex-Greater China",
    "Greater China", "China Mainland", "US & Europe", "Europe",
    "Asia ex-China", "Latin America", "Multiple Regions", "Not Disclosed",
]

CHINESE_HQ_ENUM = ["Yes", "No", "Unknown"]

def _pipe(enum: list) -> str:
    """Format an enum list as a pipe-separated string for prompt field guides."""
    return " | ".join(enum)

def _pipe_wrap(enum: list, width: int = 100) -> str:
    """Pipe-separated string, wrapped at `width` chars, indented for readability."""
    lines, cur = [], ""
    for v in enum:
        add = ("" if not cur else " | ") + v
        if cur and len(cur) + len(add) > width:
            lines.append(cur)
            cur = v
        else:
            cur += add
    if cur:
        lines.append(cur)
    return "\n    ".join(lines)

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

def normalize_name(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())

# Maps any company name variant → canonical key used only for dedup grouping
_COMPANY_ALIASES = {
    # ── Chinese party aliases (derived from DB + Nature CSV audit) ─────────────
    # Hengrui
    'hengruipharma': 'hengrui', 'jiangsuhengruipharmaceuticals': 'hengrui',
    'jiangsuhengruipharma': 'hengrui', 'hengruimedicine': 'hengrui',
    # CSPC
    'cspc': 'cspc', 'cspcpharmaceuticalgroup': 'cspc', 'cspcpharmaceuticalgr': 'cspc',
    # Fosun / YaoPharma
    'fosunpharma': 'fosun', 'fosunpharmasubsidiary': 'fosun',
    'fosunpharmasinfinitypharmaceuticals': 'fosun', 'fosunsinfinitypharmaceuticals': 'fosun',
    'fosunpharmayaopharmasubsidiary': 'fosun', 'fosunpharmasinfinitypharm': 'fosun',
    'yaopharmafosunpharmasubsidiary': 'fosun', 'yaopharmafosunpharmaceuticalsubsidiary': 'fosun',
    # Hansoh
    'hansohpharma': 'hansoh', 'hansohpharmaceuticals': 'hansoh',
    # Duality Biologics
    'dualitybiologics': 'duality', 'dualitybiodualitybiologics': 'duality',
    'dualitybioduobaobiotechnology': 'duality',
    # LaNova
    'lanova': 'lanova', 'lanovamedicines': 'lanova',
    # Akeso
    'akeso': 'akeso', 'akesopharma': 'akeso',
    # Argo
    'argobiopharma': 'argo', 'argobiopharmaceutical': 'argo',
    'argobiopharmaceuticalchinabased': 'argo', 'argobiotherapeutics': 'argo',
    'argopharmaceutical': 'argo', 'argobiopharmaceuticalchinafo': 'argo',
    # Kelun
    'kelunbiotech': 'kelun', 'kelunbiopharma': 'kelun',
    # Simcere
    'simcerepharmaceutical': 'simcere', 'simcerezaiming': 'simcere',
    # Earendil / Helixon
    'earendil': 'earendil', 'earendillabshelixontherapeutics': 'earendil',
    'earendillabsaffiliateofhelixontherapeuticschina': 'earendil',
    'helixontherapeuticsviaearendillabs': 'earendil',
    # Jiangsu Chia Tai Feng Hai
    'jiangsuchiataifenghaipharmaceutical': 'chiatai',
    'jiangsuchiataifenghaipharmaceuticalsinobiopharmaceuticalsubsidiary': 'chiatai',
    'jiangsuchiataifengha': 'chiatai',
    # Sino Biopharmaceutical
    'sinobiopharm': 'sinobiopharm', 'sinobiopharmaceutical': 'sinobiopharm',
    # SystImmune
    'systimmunebiokinpharmaceuticalsubsidiary': 'systimmune',
    # ImmuneOnco
    'immuneonco': 'immuneoncobiopharma', 'immuneoncobiopharma': 'immuneoncobiopharma',
    # Suzhou Ribo
    'suzhouribolifescience': 'suzhouribolife', 'ribolifescienceviaribocurepharmaceuticals': 'suzhouribolife',
    # Curon
    'curonbiopharma': 'curon', 'curonpharmaceutical': 'curon',
    # Innovent
    'innoventbiologics': 'innovent',
    # ── Western party aliases ──────────────────────────────────────────────────
    'merck': 'merck', 'merckco': 'merck', 'mercksharp': 'merck', 'msd': 'merck',
    'astrazeneca': 'astrazeneca', 'az': 'astrazeneca',
    'gsk': 'gsk', 'glaxosmithkline': 'gsk',
    'bristolmyerssquibb': 'bms', 'bms': 'bms',
    'roche': 'roche', 'genentech': 'roche',
    'elililly': 'lilly', 'lilly': 'lilly',
    'johnsonjohnson': 'jnj', 'janssen': 'jnj',
    'gileadsciences': 'gilead', 'gilead': 'gilead',
    'pfizer': 'pfizer', 'novartis': 'novartis', 'sanofi': 'sanofi',
    'takeda': 'takeda', 'abbvie': 'abbvie',
}
def canon_company(s: str) -> str:
    """Canonical company key for dedup — collapses name variants."""
    n = normalize_name(s)
    return _COMPANY_ALIASES.get(n, n)

def drug_names_match(a: str, b: str) -> bool:
    """Fuzzy: catches SSGJ-707 vs PD-1/VEGF, LM-299 vs generic description, etc."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb: return True
    if na == nb: return True
    if na.startswith(nb) or nb.startswith(na): return True
    if na in nb or nb in na: return True
    VAGUE = {'oral','preclinical','clinical','experimental','cancer','drug','antibody',
              'oncology','cardiovascular','bispecific','platform','undisclosed','broad',
              'multiple','novel','therapy','treatment','inflammatory','synthetic',
              'selective','hypertrophic','btk','pd1','weight','obesity','alzheimer',
              'aienabled','aidriven'}
    if a.lower().split()[0] in VAGUE or b.lower().split()[0] in VAGUE: return True
    return False

def parse_deal_value(s: str):
    """Parse $B value from '$18.5B', '$645M', '$1.2B+'. Returns float or None."""
    if not s or s.strip() in ("", "Not disclosed", "—", "N/A"): return None
    m = re.search(r'\$?([\.\d]+)\s*([BM])', s, re.IGNORECASE)
    if not m: return None
    v = float(m.group(1))
    return v if m.group(2).upper() == 'B' else v / 1000

def values_match(a: str, b: str) -> bool:
    """True if both unknown, one unknown, or both known and within 20%."""
    va, vb = parse_deal_value(a), parse_deal_value(b)
    if va is None or vb is None: return True   # unknown = can't rule out match
    if va == 0 or vb == 0: return va == vb
    return max(va, vb) / min(va, vb) <= 1.20

def both_known_match(a: str, b: str) -> bool:
    """True only when BOTH values are known and within 20% — strong signal."""
    va, vb = parse_deal_value(a), parse_deal_value(b)
    if va is None or vb is None: return False
    if va == 0 or vb == 0: return va == vb
    return max(va, vb) / min(va, vb) <= 1.20

def is_duplicate(deal: dict, existing_deals: list) -> bool:
    new_id  = make_deal_id(deal)
    new_cp  = canon_company(deal.get("chinese_party", ""))
    new_fp  = canon_company(deal.get("foreign_party", ""))
    new_dn  = deal.get("drug_name", "") or deal.get("asset", "")
    new_mo  = deal.get("announcement_month_year", "")
    new_tot = deal.get("total_value_usd", "")
    new_up  = deal.get("upfront_usd", "")
    for d in existing_deals:
        if d.get("_id") == new_id:
            return True
        if canon_company(d.get("chinese_party","")) != new_cp: continue
        if canon_company(d.get("foreign_party","")) != new_fp: continue
        if d.get("announcement_month_year","") != new_mo: continue
        # Same cp+fp+month — now check value + drug name
        # Strong signal: both known values match → merge regardless of drug name variance
        if both_known_match(new_tot, d.get("total_value_usd","")): return True
        if both_known_match(new_up,  d.get("upfront_usd","")): return True
        # Weak signal: values unknown → require drug name match too
        if (values_match(new_tot, d.get("total_value_usd",""))
                or values_match(new_up, d.get("upfront_usd",""))):
            if drug_names_match(new_dn, d.get("drug_name","") or d.get("asset","")):
                return True
    # Fallback: exact asset + Chinese party + same month (legacy safety net)
    for d in existing_deals:
        if (d.get("chinese_party","").lower() == deal.get("chinese_party","").lower()
                and d.get("asset","").lower() == deal.get("asset","").lower()
                and d.get("deal_type","").lower() == deal.get("deal_type","").lower()
                and d.get("announcement_month_year","") == deal.get("announcement_month_year","")):
            return True
    return False

# ── Tool Definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_web",
        "description": "Search English-language web scoped to PRIORITY SOURCES only (FierceBiotech, Endpoints News, BioPharma Dive, Reuters, Bloomberg, STAT News). Use ONLY in ROUND 8 — after search_web_wide has completed all discovery rounds.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "search_web_wide",
        "description": "Open web search with NO domain restrictions — entire web including press releases, IR pages, biotech blogs, regional news, wire services. PRIMARY tool for ALL discovery rounds (1–7).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "search_web_cn",
        "description": "Search Chinese-language sources (医药魔方, 药渡, 生物谷, stock exchange filings). Use general Mandarin pattern queries — no specific company or partner names. Use ONLY in ROUND 6.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "General Mandarin pattern, e.g. '中国生物技术 对外授权 2024'"}},
            "required": ["query"]
        }
    },
    {
        "name": "save_deal",
        "description": "Save one deal. Call once per deal found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "announcement_month_year": {"type": "string"},
                "deal_type": {
                    "type": "string",
                    "enum": DEAL_TYPE_ENUM
                },
                "chinese_party":    {"type": "string"},
                "foreign_party":    {"type": "string"},
                "asset":            {"type": "string"},
                "drug_name":        {"type": "string"},
                "modality": {
                    "type": "string",
                    "enum": MODALITY_ENUM
                },
                "therapeutic_area": {
                    "type": "string",
                    "enum": TA_ENUM
                },
                "stage": {
                    "type": "string",
                    "enum": STAGE_ENUM
                },
                "total_value_usd":  {"type": "string"},
                "upfront_usd":      {"type": "string"},
                "territory": {
                    "type": "string",
                    "enum": TERRITORY_ENUM
                },
                "equity_component": {"type": "string"},
                "chinese_hq": {
                    "type": "string",
                    "enum": CHINESE_HQ_ENUM,
                    "description": "Is the chinese_party headquartered in mainland China/HK/Macau? Set 'Yes' even if the company has an English-sounding name (e.g. ProFoundBio, Regor, AnHearts, Eccogene, I-Mab, Biotheus, Triastek)."
                },
                "highlights":       {"type": "string"},
                "source_url":       {"type": "string"},
                "source_name":      {"type": "string"}
            },
            "required": [
                "announcement_month_year", "deal_type", "chinese_party",
                "asset", "therapeutic_area", "source_url", "source_name"
            ]
        }
    },
    {
        "name": "finish",
        "description": "Call when all searches are done.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
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

def search_web_cn(query: str) -> str:
    """Search Chinese-language sources. Query should be in Mandarin for best results."""
    if TAVILY_API_KEY:
        try:
            # First try with Chinese source preference
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": 8,
                    "include_domains": CHINESE_SOURCES
                },
                timeout=15
            )
            results = resp.json().get("results", [])
            # Fall back to open search (no domain filter) if Chinese sources return nothing
            if not results:
                resp2 = requests.post(
                    "https://api.tavily.com/search",
                    json={"api_key": TAVILY_API_KEY, "query": query,
                          "search_depth": "advanced", "max_results": 8},
                    timeout=15
                )
                results = resp2.json().get("results", [])
            if not results:
                return "No results found."
            out = []
            for r in results:
                domain   = r.get("url","").split("/")[2] if "/" in r.get("url","") else ""
                priority = "★ CN SOURCE" if any(p in domain for p in CHINESE_SOURCES) else ""
                body     = r.get("content","") or r.get("raw_content","") or ""
                out.append(
                    f"{priority}\nTITLE: {r.get('title','')}\n"
                    f"URL: {r.get('url','')}\n"
                    f"CONTENT: {body[:1500]}\n"
                )
            return "\n---\n".join(out)
        except Exception as e:
            return f"Search error: {e}"
    else:
        return "No Tavily API key — Chinese search unavailable."


def search_web_wide(query: str) -> str:
    """Open web search — no domain restrictions. Primary discovery tool for Rounds 1–7."""
    if TAVILY_API_KEY:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query,
                      "search_depth": "advanced", "max_results": 8},
                timeout=15
            )
            results = resp.json().get("results", [])
            if not results:
                return "No results found."
            out = []
            for r in results:
                domain   = r.get("url", "").split("/")[2] if "/" in r.get("url", "") else ""
                priority = "★ PRIORITY SOURCE" if any(p in domain for p in PRIORITY_SOURCES) else ""
                body     = r.get("content", "") or r.get("raw_content", "") or ""
                out.append(f"{priority}\nTITLE: {r.get('title','')}\nURL: {r.get('url','')}\nCONTENT: {body[:2000]}\n")
            return "\n---\n".join(out)
        except Exception as e:
            return f"Search error: {e}"
    else:
        return "No Tavily API key — wide search unavailable."
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

# URL slugs that indicate a non-deal article — reject these at save time
NON_DEAL_SLUG_PATTERNS = [
    "coalition", "policy", "fda-approves", "fda-approval", "trial-results",
    "clinical-trial", "phase-results", "earnings", "conference", "layoffs",
    "lawsuit", "patent",
    "mfn", "most-favored", "drug-price", "pricing", "reimbursement",
    "worried", "fight-back", "pushback", "congress", "senator", "legislation",
    "regulation", "guidance", "comment", "opinion", "editorial",
    # NOTE: 'fundrais', 'raises-', 'series-', 'ipo' intentionally removed —
    # investor-backed spinouts (e.g. Hercules/Hengrui) and licensing deals often
    # appear in articles with these URL slugs. The validate_deal field checks
    # (chinese_party + asset present) are sufficient guards.
]

def validate_deal(deal_data: dict) -> tuple[bool, str]:
    """
    Returns (is_valid, rejection_reason).
    Catches hallucinated saves of non-deal articles before they hit the DB.
    """
    url = deal_data.get("source_url", "").lower()
    cp  = deal_data.get("chinese_party", "").strip()
    asset = deal_data.get("asset", "").strip()

    # Must have a Chinese party and an asset
    if not cp or cp in ("—", "N/A", "Unknown", ""):
        return False, f"Rejected: no chinese_party field (url: {url})"
    if not asset or asset in ("—", "N/A", "Unknown", ""):
        return False, f"Rejected: no asset field (url: {url})"

    # Reject if URL slug contains non-deal keywords
    for pattern in NON_DEAL_SLUG_PATTERNS:
        if pattern in url:
            return False, f"Rejected: URL looks like a non-deal article (matched '{pattern}'): {url}"

    # chinese_party should not read like a headline or sentence
    if len(cp.split()) > 8:
        return False, f"Rejected: chinese_party looks like a headline, not a company name: '{cp}'"

    return True, ""


# ── Harmonization ─────────────────────────────────────────────────────────────
# TA_ENUM and TERRITORY_ENUM are defined at module top as canonical sources.
# Only the fast-lookup sets are derived here.

TA_ENUM_SET        = set(TA_ENUM)
TERRITORY_ENUM_SET = set(TERRITORY_ENUM)

TA_EXACT = {
    "Autoimmune": "Immunology – Multiple Indications",
    "Autoimmune / Immunology": "Immunology – Multiple Indications",
    "Autoimmune / Oncology – B-NHL": "Oncology – Lymphoma / Leukemia",
    "Autoimmune / Oncology – Multiple Myeloma": "Oncology – Lymphoma / Leukemia",
    "Autoimmune Diseases": "Immunology – Multiple Indications",
    "Cardiovascular – Cardiometabolic diseases": "Cardiovascular – Cardiometabolic",
    "Immunology – Allergic Disorders (food allergy, asthma, CSU)": "Immunology – Asthma / Allergic Disorders",
    "Immunology – Atopic Dermatitis, Asthma": "Immunology – Atopic Dermatitis",
    "Immunology – Atopic Dermatitis, Asthma, Chronic Rhinosinusitis with Nasal Polyps": "Immunology – Atopic Dermatitis",
    "Immunology – Autoimmune diseases (psoriasis, Crohn's disease, ulcerative colitis)": "Immunology – Psoriasis / Inflammatory",
    "Immunology – Inflammatory Bowel Disease (IBD)": "Immunology – Inflammatory Bowel Disease",
    "Immunology – Systemic Lupus Erythematosus (SLE) / Lupus Nephritis": "Immunology – Lupus / Nephrology",
    "Metabolic – Obesity / Cardiometabolic": "Metabolic – Obesity",
    "Metabolic – Obesity/Type 2 Diabetes": "Metabolic – Obesity",
    "Metabolic – Type 2 Diabetes / MASH": "Metabolic – Diabetes",
    "Multiple – oral RNA therapeutics": "RNA Therapeutics – Platform",
    "Not disclosed (likely Oncology based on BioNTech pipeline focus)": "Not Disclosed",
    "Oncology": "Oncology – Multiple Indications",
    "Oncology – Advanced Solid Tumors": "Oncology – Solid Tumors",
    "Oncology – Breast Cancer (HR+/HER2-)": "Oncology – Breast Cancer",
    "Oncology – Chronic Myeloid Leukemia (CML)": "Oncology – Lymphoma / Leukemia",
    "Oncology – HR+/HER2- Breast Cancer & Advanced Solid Tumors": "Oncology – Breast Cancer",
    "Oncology – NSCLC, SCLC, TNBC": "Oncology – NSCLC",
    "Oncology – Non-Hodgkin Lymphoma / ALL; Autoimmune Diseases": "Oncology – Lymphoma / Leukemia",
    "Oncology – Ovarian Cancer / Solid Tumors": "Oncology – Ovarian Cancer",
    "Oncology – RAS-mutant solid tumors (PDAC, CRC, NSCLC)": "Oncology – Solid Tumors",
    "Oncology – ROS1-positive NSCLC": "Oncology – NSCLC",
    "Oncology – SCLC / Neuroendocrine Tumors": "Oncology – Neuroendocrine Tumors",
    "Oncology – Solid Tumors (MTAP-deleted; glioblastoma, pancreatic cancer, NSCLC)": "Oncology – Solid Tumors",
    "Oncology – Solid Tumors (Urothelial Cancer, TNBC)": "Oncology – Solid Tumors",
    "Oncology – Solid Tumors (adult and pediatric)": "Oncology – Solid Tumors",
    "Oncology – Solid Tumors (lung cancer, gastrointestinal tumors)": "Oncology – Solid Tumors",
    "Oncology – Solid Tumors/NSCLC": "Oncology – NSCLC",
    "Oncology – lung cancer, gastrointestinal cancer, ovarian cancer": "Oncology – Solid Tumors",
    "Oncology; Immunology – multiple indications": "Oncology – Multiple Indications",
    "Respiratory/Immunology – Asthma, Atopic Dermatitis": "Respiratory – Asthma",
    "Women's Health – Fertility / Assisted Reproductive Technology": "Women's Health",
}

TERRITORY_EXACT = {
    "Worldwide": "Global",
    "Global (Hansoh retains option to co-promote or solely commercialize in China)": "Global",
    "Global (exclusive rights to BioNTech)": "Global",
    "Worldwide (Phase 1 asset) + ex-Greater China (Phase 1/2a asset)": "Global ex-Greater China",
    "Worldwide (taletrectinib previously out-licensed in China to Innovent, Japan to Nippon Kayaku, Korea)": "Global",
    "Global ex-China (all territories outside mainland China, Hong Kong, Macau, Taiwan, and Russia)": "Global ex-China",
    "Global ex-China (excluding Mainland China, Hong Kong, Macau and Taiwan)": "Global ex-China",
    "Global ex-China (excluding mainland China, Hong Kong, Macau and Taiwan)": "Global ex-China",
    "Global ex-China (excluding mainland China, Hong Kong, Macau, Taiwan)": "Global ex-China",
    "Global ex-China (excluding mainland China, Hong Kong, and Macau)": "Global ex-China",
    "Greater China and Singapore": "Greater China",
    "EU, UK, Switzerland and selected other countries": "Europe",
    "US, Canada, Europe, Japan (expanded in June 2024 to include Latin America, Middle East, Africa)": "Multiple Regions",
    "Brazil and LATAM": "Latin America",
    "Not disclosed": "Not Disclosed",
}

_TA_FUZZY_RULES = [
    (["nsclc", "non-small cell lung"],                        "Oncology – NSCLC"),
    (["lung cancer"],                                         "Oncology – NSCLC"),
    (["sclc", "small cell lung"],                             "Oncology – Neuroendocrine Tumors"),
    (["neuroendocrine", "carcinoid"],                         "Oncology – Neuroendocrine Tumors"),
    (["breast cancer", "her2", "hr+", "tnbc"],                "Oncology – Breast Cancer"),
    (["ovarian", "fallopian", "peritoneal"],                  "Oncology – Ovarian Cancer"),
    (["gastric", "colorectal", "crc", "pdac", "pancreatic",
      "biliary", "cholangiocarcinoma", "hepatocellular",
      "gastrointestinal", "gi cancer"],                       "Oncology – Gastrointestinal Cancer"),
    (["lymphoma", "leukemia", "leukaemia", "myeloma",
      "cll", "mcl", "aml", "cml", "all", "nhl", "dlbcl"],    "Oncology – Lymphoma / Leukemia"),
    (["glioblastoma", "glioma", "gbm", "brain tumor",
      "brain tumour"],                                        "Oncology – Solid Tumors"),
    (["urothelial", "bladder cancer", "prostate", "renal cell",
      "cervical", "endometrial", "head and neck", "sarcoma",
      "solid tumor", "solid tumour", "advanced tumor"],       "Oncology – Solid Tumors"),
    (["oncology", "cancer", "tumor", "tumour",
      "carcinoma", "malignancy"],                             "Oncology – Multiple Indications"),
    (["atopic dermatitis", "eczema"],                         "Immunology – Atopic Dermatitis"),
    (["ibd", "inflammatory bowel", "crohn",
      "ulcerative colitis"],                                  "Immunology – Inflammatory Bowel Disease"),
    (["lupus", "sle", "systemic lupus"],                      "Immunology – Lupus / Nephrology"),
    (["asthma", "allergic", "food allergy", "csu",
      "chronic urticaria", "rhinosinusitis"],                 "Immunology – Asthma / Allergic Disorders"),
    (["psoriasis", "psoriatic", "ankylosing",
      "rheumatoid arthritis"],                                "Immunology – Psoriasis / Inflammatory"),
    (["autoimmune", "immunology", "inflammatory"],            "Immunology – Multiple Indications"),
    (["obesity", "weight loss", "glp-1", "glp1"],             "Metabolic – Obesity"),
    (["mash", "nash", "steatohepatitis", "fatty liver",
      "masld"],                                               "Metabolic – MASH / Liver"),
    (["type 2 diabetes", "t2d", "hba1c", "insulin"],          "Metabolic – Diabetes"),
    (["cardiometabolic", "metabolic syndrome"],               "Metabolic – Cardiometabolic"),
    (["dyslipidemia", "cholesterol", "ldl", "lp(a)",
      "triglyceride", "hyperlipidemia"],                      "Cardiovascular – Dyslipidemia"),
    (["cardiovascular", "cardiac", "heart failure",
      "hypertension", "hcm", "atrial fibrillation"],          "Cardiovascular – Cardiometabolic"),
    (["iga nephropathy", "igan"],                             "Nephrology – IgA Nephropathy"),
    (["nephrology", "kidney", "renal", "glomerular",
      "ckd", "fsgs"],                                         "Nephrology – Other"),
    (["respiratory", "copd", "pulmonary fibrosis",
      "ipf", "pulmonary hypertension"],                       "Respiratory – Other"),
    (["women", "fertility", "reproductive",
      "endometriosis", "assisted reproductive"],              "Women's Health"),
    (["rna", "sirna", "mrna", "oligonucleotide",
      "antisense"],                                           "RNA Therapeutics – Platform"),
]

def _ta_fuzzy(s: str) -> str | None:
    sl = s.lower()
    for keywords, canonical in _TA_FUZZY_RULES:
        if any(kw in sl for kw in keywords):
            return canonical
    return None

def normalize_therapeutic_area(s: str) -> str:
    if not s: return "Not Disclosed"
    if s in TA_ENUM_SET: return s
    if s in TA_EXACT: return TA_EXACT[s]
    fuzzy = _ta_fuzzy(s)
    if fuzzy:
        print(f"  [TA fuzzy] {repr(s)} → {repr(fuzzy)}")
        return fuzzy
    print(f"  [TA UNMATCHED] {repr(s)} — stored as-is; add to TA_EXACT to silence")
    return s

def normalize_territory(s: str) -> str:
    if not s: return "Not Disclosed"
    if s in TERRITORY_ENUM_SET: return s
    if s in TERRITORY_EXACT: return TERRITORY_EXACT[s]
    sl = s.lower()
    if "ex-greater china" in sl or "ex greater china" in sl or (
            "excluding" in sl and "greater china" in sl):
        return "Global ex-Greater China"
    if "ex-china" in sl or "ex china" in sl or (
            "excluding" in sl and "china" in sl):
        return "Global ex-China"
    if "greater china" in sl: return "Greater China"
    if "worldwide" in sl: return "Global"
    if "mainland china" in sl or "china mainland" in sl: return "China Mainland"
    if "us" in sl and "europe" in sl: return "US & Europe"
    if "latam" in sl or "latin america" in sl: return "Latin America"
    if "eu" in sl or "europe" in sl or "uk" in sl: return "Europe"
    if "not disclosed" in sl or "undisclosed" in sl: return "Not Disclosed"
    print(f"  [TERRITORY UNMATCHED] {repr(s)} — stored as-is; add to TERRITORY_EXACT to silence")
    return s

def normalize_usd(s: str) -> str:
    if not s: return "Not Disclosed"
    s = s.strip()
    if re.match(r"(not disclosed|all-stock|double.digit million|~\$250M.*all-stock)", s, re.I):
        return "Not Disclosed"
    s = re.sub(r"^~", "", s)
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*B", s, re.I)
    if m:
        n = round(float(m.group(1).replace(",", "")) * 1000, 1)
        return f"${n / 1000:.3g}B"
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*M", s, re.I)
    if m:
        n = round(float(m.group(1).replace(",", "")), 1)
        return f"${n:.3g}M"
    return "Not Disclosed"


def save_deal(deal_data: dict) -> str:
    global db, new_deals_this_run

    # Validate before touching anything
    valid, reason = validate_deal(deal_data)
    if not valid:
        print(f"  [REJECTED] {reason}")
        return reason

    deal_data["announcement_month_year"] = sanitize_date(
        deal_data.get("announcement_month_year", ""))
    deal_data["therapeutic_area"] = normalize_therapeutic_area(
        deal_data.get("therapeutic_area", ""))
    deal_data["territory"]        = normalize_territory(
        deal_data.get("territory", ""))
    deal_data["upfront_usd"]      = normalize_usd(
        deal_data.get("upfront_usd", ""))
    deal_data["total_value_usd"]  = normalize_usd(
        deal_data.get("total_value_usd", ""))
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
    if name == "search_web":       return search_web(**inp)
    if name == "search_web_wide":  return search_web_wide(**inp)
    if name == "search_web_cn":    return search_web_cn(**inp)
    if name == "save_deal":        return save_deal(inp)
    if name == "finish":           return finish(**inp)
    return f"Unknown tool: {name}"

def build_company_list(existing_deals: list, search_window: str = "") -> str:
    """
    Builds the Round 2 company list dynamically from the live database.
    Companies are ranked by deal count so the agent searches the most productive
    ones first. New companies discovered in previous runs are automatically included.
    """
    from collections import Counter

    # Extract canonical company names and count deals per company
    counts = Counter()
    for d in existing_deals:
        cp = d.get('chinese_party', '').strip()
        if cp and cp != 'Unknown Chinese firm':
            counts[canon_company(cp)] += 1

    # Resolve canon key back to the best display name in the DB
    # (pick the most frequent actual name string for each canon key)
    canon_to_display = {}
    name_freq = Counter()
    for d in existing_deals:
        cp = d.get('chinese_party', '').strip()
        if cp:
            key = canon_company(cp)
            name_freq[(key, cp)] += 1
    for (key, name), freq in name_freq.items():
        if key not in canon_to_display or freq > name_freq[(key, canon_to_display[key])]:
            canon_to_display[key] = name

    # Extract year hint from search_window for query suffix
    import re
    yr_match = re.search(r'\b(20\d{2})\b', search_window)
    yr = yr_match.group(1) if yr_match else "2024 2025"

    # Bucket into tiers
    tier1 = [(canon_to_display.get(k, k), n) for k, n in counts.most_common() if n >= 5]
    tier2 = [(canon_to_display.get(k, k), n) for k, n in counts.most_common() if 2 <= n < 5]
    tier3 = [(canon_to_display.get(k, k), n) for k, n in counts.most_common() if n == 1]

    def fmt_queries(companies, per_line=3):
        queries = [f'"{name} deal {yr}"' for name, _ in companies]
        lines = []
        for i in range(0, len(queries), per_line):
            lines.append("    " + ", ".join(queries[i:i+per_line]))
        return "\n".join(lines)

    lines = [
        "ROUND 2 — COMPANY-SPECIFIC SEARCHES",
        f"  This list is auto-generated from the live database ({len(counts)} companies, ranked by deal count).",
        f"  New companies found in previous runs are automatically included here.",
        "",
    ]
    if tier1:
        lines.append(f"  Tier 1 – Most active ({len(tier1)} companies, 5+ deals each):")
        lines.append(fmt_queries(tier1))
    if tier2:
        lines.append(f"  Tier 2 – Active ({len(tier2)} companies, 2-4 deals each):")
        lines.append(fmt_queries(tier2))
    if tier3:
        lines.append(f"  Tier 3 – Single deal on record ({len(tier3)} companies — search these too):")
        lines.append(fmt_queries(tier3, per_line=4))

    return "\n".join(lines)


def build_modality_round(existing_deals: list, search_window: str = "") -> str:
    """
    Builds Round 3 modality queries dynamically from the live DB.
    Ranks modalities by deal count so the highest-yield drug types are searched first.
    On first run (empty DB) falls back to broad structural terms — no hardcoded names.
    """
    from collections import Counter
    yr_match = re.search(r'\b(20\d{2})\b', search_window)
    yr = yr_match.group(1) if yr_match else ""
    yr_suffix = f" {yr}" if yr else ""

    counts = Counter()
    for d in existing_deals:
        m = (d.get("modality") or "").strip()
        if m and m.lower() not in ("", "other", "not disclosed"):
            counts[m] += 1

    lines = ["ROUND 3 — MODALITY / THERAPY SWEEPS via search_web_wide"]
    if counts:
        lines += [
            f"  Auto-generated from DB ({len(counts)} modalities seen, ranked by frequency).",
            f"  New modalities found in future runs are automatically included.",
            "",
        ]
        for modality, n in counts.most_common():
            lines.append(f'  Search: "China {modality} licensing deal{yr_suffix}"   ({n} deals in DB)')
    else:
        lines += [
            "  DB is empty — using broad structural modality terms (not hardcoded to specific drugs).",
            "",
            f'  "China ADC antibody drug conjugate licensing{yr_suffix}"',
            f'  "China bispecific antibody deal{yr_suffix}"',
            f'  "China small molecule licensing{yr_suffix}"',
            f'  "China monoclonal antibody licensing deal{yr_suffix}"',
            f'  "China cell therapy CAR-T licensing{yr_suffix}"',
            f'  "China GLP-1 metabolic disease deal{yr_suffix}"',
            f'  "China siRNA mRNA oligonucleotide licensing{yr_suffix}"',
            f'  "China gene therapy licensing deal{yr_suffix}"',
        ]
    return "\n".join(lines)


def build_partner_round(existing_deals: list, search_window: str = "") -> str:
    """
    Builds Round 4 partner queries dynamically from the live DB.
    Ranks foreign parties by deal count — most active buyers searched first.
    On first run (empty DB) falls back to structural 'global pharma China' sweeps
    with no hardcoded company names, so any buyer (Takeda, Roche, GSK, etc.) surfaces.
    """
    from collections import Counter
    yr_match = re.search(r'\b(20\d{2})\b', search_window)
    yr = yr_match.group(1) if yr_match else ""
    yr_suffix = f" {yr}" if yr else ""

    counts = Counter()
    for d in existing_deals:
        fp = (d.get("foreign_party") or "").strip()
        if fp and fp.lower() not in ("", "not disclosed", "multiple", "various"):
            counts[fp] += 1

    lines = ["ROUND 4 — PARTNER-FOCUSED via search_web_wide"]
    if counts:
        lines += [
            f"  Auto-generated from DB ({len(counts)} foreign partners seen, ranked by deal count).",
            f"  New partners found in future runs are automatically included.",
            "",
        ]
        for partner, n in counts.most_common(15):
            lines.append(f'  Search: "{partner} China licensing{yr_suffix}"   ({n} deals in DB)')
        lines += [
            "",
            "  Also run broad sweeps to surface buyers NOT yet in DB:",
            f'  "global pharma China biotech deal{yr_suffix}"',
            f'  "Japan pharma China licensing{yr_suffix}"',
        ]
    else:
        lines += [
            "  DB is empty — using structural sweeps only (no hardcoded company names).",
            "  These will surface ANY Western buyer — Takeda, Roche, GSK, Sanofi, AbbVie, etc.",
            "",
            f'  "US pharma China biotech licensing deal{yr_suffix}"',
            f'  "European pharma China licensing{yr_suffix}"',
            f'  "Japan pharma China biotech deal{yr_suffix}"',
            f'  "multinational pharma China licensing{yr_suffix}"',
            f'  "global pharma China biotech deal{yr_suffix}"',
            f'  "big pharma China licensing option{yr_suffix}"',
        ]
    return "\n".join(lines)


# ── System Prompt ──────────────────────────────────────────────────────────────

def build_system_prompt(existing_deals: list, search_window: str = "",
                        completed_queries: list = None) -> str:
    # Compact already-saved list — just cp/drug/month, capped at 20 entries
    saved_summary = "\n".join(
        f"{d.get('chinese_party','?')}/{d.get('drug_name') or d.get('asset','?')[:25]}/{d.get('announcement_month_year','?')}"
        for d in existing_deals[-20:]
    ) or "None yet."

    window_str = search_window if search_window else "Search all time."

    # Build dynamic company list from live DB
    company_round  = build_company_list(existing_deals, search_window)
    modality_round = build_modality_round(existing_deals, search_window)
    partner_round  = build_partner_round(existing_deals, search_window)

    # Compact query log — tells Claude exactly what it has already searched
    if completed_queries:
        query_log = (
            f"SEARCHES ALREADY COMPLETED THIS SESSION ({len(completed_queries)} total — do NOT repeat these):\n" +
            "\n".join(f"  ✓ {q}" for q in completed_queries)
        )
    else:
        query_log = "SEARCHES ALREADY COMPLETED THIS SESSION: none yet — this is the first search."

    return f"""You are a senior biopharma deal analyst specializing in China life science transactions.

DATABASE: {len(existing_deals)} deals already saved. Do NOT re-save anything already here:
{saved_summary}

YOUR MISSION: Find NEW deals not listed above and save them using save_deal().
SEARCH WINDOW: {window_str}

{query_log}

═══════════════════════════════════════════════════
 CRITICAL RULES
═══════════════════════════════════════════════════

**SAVE AGGRESSIVELY.** After EVERY search, immediately call save_deal() for EACH deal found.
Do NOT wait. Do NOT skip a deal because some fields are uncertain.
Use "Not disclosed" for any financial fields you cannot find.

**USE ALL YOUR STEPS.** Do NOT call finish() until you have done AT LEAST 15 searches.
You have a generous step budget — use every single step productively.
Calling finish() early wastes the budget and leaves deals unfound.

**SAVE AFTER EVERY SEARCH.** The only valid pattern is:
  search → save ALL deals found → search → save ALL deals found → ... (repeat 15+ times) → finish()

═══════════════════════════════════════════════════
 DEAL TYPES — save ALL of these structures
═══════════════════════════════════════════════════

| Structure | deal_type | How to recognise it |
|-----------|-----------|---------------------|
| Chinese company grants rights to foreign pharma | licensing-out | "X licensed Y to Z for $..." |
| Foreign pharma pays option fee for right to later license | option-to-license | "option", "right to license", "option agreement" |
| Investors create NewCo to hold Chinese assets | newco-spinout | "NewCo", "backed by", "spinout", "new company formed" |
| Access to a technology platform (not a specific drug) | platform-deal | "AI platform", "ADC platform", "linker tech", "discovery collab" |
| Chinese company acquires rights from foreign party | licensing-in | Chinese company is the buyer of foreign IP |
| Joint R&D sharing costs/rights equally | co-development | "co-develop", "joint development" |
| Full company or asset acquisition | acquisition | "acquires", "buys", "merger" |

═══════════════════════════════════════════════════
 FIELD GUIDE
═══════════════════════════════════════════════════

- announcement_month_year: "Month YYYY" e.g. "March 2024"
- deal_type: {_pipe(DEAL_TYPE_ENUM)}
- chinese_party: Chinese company name
- foreign_party: non-Chinese partner (or NewCo name + backers)
- asset: drug code + target + indication e.g. "HRS-5346 (oral Lp(a) inhibitor, cardiovascular)"
- drug_name: code only — "HRS-5346" or "ivonescimab". No descriptions.
- modality: {_pipe(MODALITY_ENUM)}
- therapeutic_area: pick closest from — {_pipe_wrap(TA_ENUM)}
- stage: {_pipe(STAGE_ENUM)}
- total_value_usd / upfront_usd: single clean figure e.g. "$1.2B", "$300M", "Not Disclosed" — strip all qualifiers and parenthetical notes.
- territory: {_pipe_wrap(TERRITORY_ENUM)}
  "Worldwide" → Global. "Global ex-China (excluding ...)" → Global ex-China.
- equity_component: e.g. "~20% stake in NewCo" or "None"
- highlights: ONE sentence max — most notable fact (value, strategic angle, or record term)
- source_url / source_name: article URL and publication name

Do NOT save: policy/pricing articles, pure fundraising rounds, clinical results without a deal,
opinion pieces, or any item missing a named Chinese company + named drug/asset.

═══════════════════════════════════════════════════
 SEARCH STRATEGY — MANDATORY SEQUENCE
═══════════════════════════════════════════════════

TOOL USAGE:
  search_web_wide → PRIMARY tool for ALL discovery rounds (1–7).
                    No domain filter — finds deals on press wires, IR pages,
                    regional sites, and anywhere priority outlets don't cover.
  search_web      → ONLY Round 8 (priority-source cleanup).
  search_web_cn   → ONLY Round 6 (Mandarin sweeps).

Do NOT hardcode specific company names, partner names, or drug names into queries
in Rounds 3–7. These rounds use structural and ecosystem searches that work for
any year. Specific known companies are already covered by the auto-generated
Round 2 list. Specific named deals belong in --csv mode.

Work through ALL rounds in order. Keep a mental checklist.

ROUND 1 — MONTHLY SWEEPS via search_web_wide (12 searches)
  "China biopharma licensing deal January {window_str}"
  "China biopharma licensing deal February {window_str}"
  ... (March through December — one search per month)
Save ALL deals found before moving to the next month.

{company_round}

{modality_round}

{partner_round}

ROUND 5 — DEAL STRUCTURE SEARCHES via search_web_wide
  "China biotech NewCo spinout deal {window_str}"
  "China biopharma option agreement licensing {window_str}"
  "China biotech small molecule licensing {window_str}"
  "China biopharma preclinical deal {window_str}"

ROUND 6 — CHINESE-LANGUAGE SWEEPS via search_web_cn
General Mandarin pattern searches only — no specific company or partner names.
  "中国生物技术 对外授权 {window_str}"            (China biotech outbound licensing)
  "医药 license-out 授权交易 {window_str}"        (pharma license-out deals)
  "中国创新药 BD交易 {window_str}"                (China innovative drug BD deals)
  "生物科技公司 跨境授权 {window_str}"             (biotech cross-border licensing)
  "新药授权 里程碑付款 {window_str}"               (new drug licensing milestone payments)
  "肿瘤 抗体 授权合作 {window_str}"               (oncology antibody licensing)
  "ADC 授权 {window_str}"                        (ADC licensing)
  "代谢病 授权 {window_str}"                     (metabolic disease licensing)

ROUND 7 — GEOGRAPHY & ECOSYSTEM via search_web_wide
Do NOT name specific companies. Search by location and investor network to surface
companies whose English names give no hint of their China origin.
  City hubs:
    "Shanghai biotech licensing deal {window_str}"
    "Suzhou biotech licensing {window_str}"
    "Beijing biopharmaceutical deal {window_str}"
    "Hangzhou biotech licensing {window_str}"
    "Guangzhou Shenzhen biotech deal {window_str}"
  Investor ecosystems:
    "OrbiMed China portfolio licensing {window_str}"
    "6 Dimensions Capital biotech deal {window_str}"
    "Lilly Asia Ventures portfolio licensing {window_str}"
    "Hillhouse biotech licensing deal {window_str}"
  Set chinese_hq = "Yes" for all companies found in this round.

ROUND 8 — PRIORITY SOURCE CLEANUP via search_web
  "China biopharma licensing {window_str} site:fiercebiotech.com"
  "China licensing {window_str} site:endpointsnews.com"
  "China biotech deal {window_str} site:biopharmadive.com"
  "China licensing {window_str} site:reuters.com"
  "China biotech {window_str} site:statnews.com"

═══════════════════════════════════════════════════
 SEARCH BUDGET REMINDER
═══════════════════════════════════════════════════

Budget = any search tool call. Save calls are free.

- Searches 1-12:   Monthly sweeps — search_web_wide (ROUND 1)
- Searches 13-32:  Company-specific — search_web_wide (ROUND 2, auto-generated)
- Searches 33+:    Modality — search_web_wide (ROUND 3, auto-generated)
- Searches cont.:  Partner — search_web_wide (ROUND 4, auto-generated)
- Searches cont.:  Structure — search_web_wide (ROUND 5)
- Searches cont.:  Chinese-language — search_web_cn (ROUND 6)
- Searches cont.:  Geography/ecosystem — search_web_wide (ROUND 7)
- Searches cont.:  Priority cleanup — search_web (ROUND 8)
- Final:           finish() — only after ALL rounds complete

After EACH search call, immediately save_deal() for EVERY deal found.

DO NOT call finish() before completing at least ROUND 1, ROUND 2, and ROUND 7."""

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


def run_agent(goal: str, search_window: str = "", max_steps: int = MAX_STEPS):
    print(f"\n{'='*60}")
    print(f"  CHINA BIOPHARMA DEAL AGENT  v2")
    print(f"  Existing deals in DB: {len(db['deals'])}")
    print(f"  Search budget: {max_steps} searches")
    print(f"{'='*60}\n")

    messages          = [{"role": "user", "content": goal}]
    searches          = 0
    api_calls         = 0
    done              = False
    completed_queries = []   # every search query that has been run this session

    while searches < max_steps and not done:
        api_calls += 1
        print(f"[API call {api_calls} | Searches used: {searches}/{max_steps}] Calling Claude...")

        trimmed_messages = trim_messages(messages, keep_first=1, keep_last=8)

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=build_system_prompt(db["deals"], search_window, completed_queries),
                tools=TOOLS,
                messages=trimmed_messages
            )

        except anthropic.RateLimitError as e:
            print(f"\n  [Rate limit hit: {e}]")
            print(f"  Waiting 60 seconds before retrying...")
            import time
            time.sleep(62)
            print(f"  Retrying...")
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=build_system_prompt(db["deals"], search_window, completed_queries),
                    tools=TOOLS,
                    messages=trimmed_messages
                )
            except anthropic.RateLimitError as e2:
                print(f"  [Rate limit hit again — saving what we have and stopping]")
                break

        except anthropic.APIError as e:
            print(f"\n  [API error: {e}]")
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            print("  [No tool calls — agent done]")
            break

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

            if block.name in ("search_web", "search_web_cn", "search_web_wide"):
                searches += 1
                completed_queries.append(block.input.get("query", ""))
            if block.name == "finish":
                called_finish = True

        messages.append({"role": "user", "content": tool_results})

        if called_finish:
            done = True

    save_database(db, mark_run=True)
    print(f"\n{'='*60}")
    print(f"  New deals this run:      {len(new_deals_this_run)}")
    print(f"  Total deals in database: {len(db['deals'])}")
    print(f"  Searches performed:      {searches}")
    print(f"  Total API calls:         {api_calls}")
    print(f"{'='*60}\n")

# ── Dashboard Generator ────────────────────────────────────────────────────────

def generate_dashboard(db: dict, search_window: str = ""):
    deals        = db["deals"]
    updated      = (db.get("last_updated") or "")[:16].replace("T", " ")
    last_run     = db.get("last_run_date", "Never")
    window_disp  = search_window if search_window else f"Since {last_run}" if last_run != "Never" else "All time"
    total        = len(deals)

    # ── Aggregations ─────────────────────────────────────────────────────────

    def parse_usd_millions(s: str) -> float:
        """Parse '$1.2B' → 1200.0, '$300M' → 300.0, anything else → 0."""
        if not s:
            return 0.0
        m = re.search(r'\$([\d,]+(?:\.\d+)?)\s*B', s, re.I)
        if m:
            return float(m.group(1).replace(',', '')) * 1000
        m = re.search(r'\$([\d,]+(?:\.\d+)?)\s*M', s, re.I)
        if m:
            return float(m.group(1).replace(',', ''))
        return 0.0

    types           = {}
    areas           = {}
    chinese_parties = {}
    foreign_parties = {}
    months_raw      = {}

    # Parallel value-sum dicts (total_value_usd in $M)
    types_val   = {}
    areas_val   = {}
    cp_val      = {}
    fp_val      = {}
    months_val  = {}

    for d in deals:
        v = parse_usd_millions(d.get("total_value_usd", ""))

        t  = d.get("deal_type", "Other")
        types[t]     = types.get(t, 0) + 1
        types_val[t] = types_val.get(t, 0.0) + v

        a  = d.get("therapeutic_area", "Other")
        areas[a]     = areas.get(a, 0) + 1
        areas_val[a] = areas_val.get(a, 0.0) + v

        cp = d.get("chinese_party", "?")
        chinese_parties[cp] = chinese_parties.get(cp, 0) + 1
        cp_val[cp]          = cp_val.get(cp, 0.0) + v

        fp = d.get("foreign_party", "")
        if fp and fp.upper() not in ("N/A", "NA", "NONE", ""):
            foreign_parties[fp] = foreign_parties.get(fp, 0) + 1
            fp_val[fp]          = fp_val.get(fp, 0.0) + v

        m  = d.get("announcement_month_year", "")
        if m:
            months_raw[m] = months_raw.get(m, 0) + 1
            months_val[m] = months_val.get(m, 0.0) + v

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
        return (9999, 0)

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
        return label

    months_sorted = sorted(months_raw.items(), key=lambda x: month_sort_key(x[0]))
    month_labels  = [fmt_date(x[0]) for x in months_sorted]
    month_counts  = [x[1] for x in months_sorted]
    month_values  = [round(months_val.get(x[0], 0.0), 1) for x in months_sorted]

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

    broad_areas     = {}
    broad_areas_val = {}
    for a in areas:
        b = broad_area(a)
        broad_areas[b]     = broad_areas.get(b, 0) + areas[a]
        broad_areas_val[b] = broad_areas_val.get(b, 0.0) + areas_val.get(a, 0.0)
    broad_areas     = dict(sorted(broad_areas.items(), key=lambda x: -x[1])[:9])
    broad_areas_val = {k: round(broad_areas_val.get(k, 0.0), 1) for k in broad_areas}

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
            "licensing-out":    SET1[0], "licensing-in":    SET1[1],
            "M&A":              SET1[4], "partnership":     SET1[2],
            "co-development":   SET1[3], "acquisition":     SET1[7],
            "option-to-license": "#9B59B6", "newco-spinout": "#E67E22",
            "platform-deal":    "#16A085",
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
        f'<td class="dn">{esc_html(d.get("drug_name","—"))}</td>'
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

    # Chart data as JSON — counts AND values for each dimension
    broad_labels_js  = json.dumps(list(broad_areas.keys()))
    broad_counts_js  = json.dumps(list(broad_areas.values()))
    broad_values_js  = json.dumps(list(broad_areas_val.values()))

    cn_labels_js     = json.dumps([x[0] for x in top_cn])
    cn_counts_js     = json.dumps([x[1] for x in top_cn])
    cn_values_js     = json.dumps([round(cp_val.get(x[0], 0.0), 1) for x in top_cn])

    fp_labels_js     = json.dumps([x[0] for x in top_fp])
    fp_counts_js     = json.dumps([x[1] for x in top_fp])
    fp_values_js     = json.dumps([round(fp_val.get(x[0], 0.0), 1) for x in top_fp])

    month_labels_js  = json.dumps(month_labels)
    month_counts_js  = json.dumps(month_counts)
    month_values_js  = json.dumps(month_values)

    set1_js          = json.dumps(SET1)
    area_pal_js      = palette(len(broad_areas))

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

/* Chart mode toggle */
.chart-toggle{{display:flex;gap:0;border:1px solid var(--brd);border-radius:5px;overflow:hidden;margin-left:auto}}
.chart-toggle button{{background:none;border:none;color:var(--mut);font-family:var(--mono);font-size:10px;padding:3px 10px;cursor:pointer;transition:background .15s,color .15s;white-space:nowrap}}
.chart-toggle button:hover{{background:#ffffff0a;color:var(--txt)}}
.chart-toggle button.active{{background:var(--b);color:#fff}}

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

<!-- Chart mode toggle -->
<div style="display:flex;align-items:center;padding:10px 22px 0;background:var(--surf);border-top:1px solid var(--brd);gap:12px">
  <span style="font-size:10px;color:var(--mut);letter-spacing:.07em;text-transform:uppercase">Charts show</span>
  <div class="chart-toggle">
    <button id="btn-count" class="active" onclick="setMode('count')"># Deals</button>
    <button id="btn-value" onclick="setMode('value')">Total Value ($M)</button>
  </div>
  <span id="val-note" style="font-size:10px;color:var(--mut);display:none">⚠ Deals with undisclosed values counted as $0</span>
</div>

<!-- Charts 2x2 -->
<div class="charts-grid">
  <div class="chart-card">
    <h3>by Therapeutic Area <span class="pill">{len(broad_areas)} categories</span></h3>
    <div class="chart-wrap" id="wrap-aC">
      <canvas id="aC"></canvas>
    </div>
  </div>
  <div class="chart-card">
    <h3>by Month <span class="pill">{len(month_labels)} months</span></h3>
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
      <col style="width:215px">  <!-- asset -->
      <col style="width:110px">  <!-- drug name -->
      <col style="width:155px">  <!-- therapeutic area -->
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
      <th onclick="srt(4)">Drug Name ↕</th>
      <th onclick="srt(5)">Therapeutic Area ↕</th>
      <th onclick="srt(6)">Deal Type ↕</th>
      <th onclick="srt(7)">Stage ↕</th>
      <th onclick="srt(8)">Modality ↕</th>
      <th onclick="srt(9)">Total Value ↕</th>
      <th onclick="srt(10)">Upfront ↕</th>
      <th onclick="srt(11)">Territory ↕</th>
      <th>Source</th>
      <th onclick="srt(13)">Highlights (hover to expand)</th>
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

// ── Chart data — both modes ───────────────────────────────────────────────────
const DATA = {{
  area: {{
    labels:  {broad_labels_js},
    counts:  {broad_counts_js},
    values:  {broad_values_js},
  }},
  month: {{
    labels:  {month_labels_js},
    counts:  {month_counts_js},
    values:  {month_values_js},
  }},
  cn: {{
    labels:  {cn_labels_js},
    counts:  {cn_counts_js},
    values:  {cn_values_js},
  }},
  fp: {{
    labels:  {fp_labels_js},
    counts:  {fp_counts_js},
    values:  {fp_values_js},
  }},
}};

// ── Chart instances ───────────────────────────────────────────────────────────
const CHARTS = {{}};

function makeBarChart(id, w, h, labels, data, colors, indexAxis='y', xStepSize=null) {{
  const opts = {{
    responsive: false, maintainAspectRatio: false,
    indexAxis,
    plugins: {{ legend: {{ display:false }},
      tooltip: {{
        callbacks: {{
          label: ctx => {{
            const v = ctx.parsed[indexAxis === 'y' ? 'x' : 'y'];
            return currentMode === 'value'
              ? ` ${{v >= 1000 ? (v/1000).toFixed(1)+'B' : v+'M'}}`
              : ` ${{v}} deal${{v===1?'':'s'}}`;
          }}
        }}
      }}
    }},
    scales: {{
      x: {{
        ticks: {{ color:tc, font:{{size:10}},
          ...(indexAxis==='x' ? {{maxRotation:45,minRotation:30}} : {{stepSize: xStepSize||1}}),
          callback: v => currentMode==='value'
            ? (v>=1000 ? '$'+(v/1000).toFixed(0)+'B' : '$'+v+'M')
            : v
        }},
        grid: {{color:gc}}
      }},
      y: {{
        ticks: {{ color: indexAxis==='y' ? '#dde3ee' : tc, font:{{size: indexAxis==='y' ? 11 : 10}},
          ...(indexAxis==='x' ? {{stepSize: xStepSize||1,
            callback: v => currentMode==='value'
              ? (v>=1000 ? '$'+(v/1000).toFixed(0)+'B' : '$'+v+'M')
              : v
          }} : {{}})
        }},
        grid: {{color: indexAxis==='y' ? 'transparent' : gc}}
      }}
    }}
  }};
  const chart = new Chart(makeCanvas(id, w, h), {{
    type: 'bar',
    data: {{
      labels,
      datasets: [{{
        data,
        backgroundColor: (Array.isArray(colors) ? colors : Array(labels.length).fill(colors))
          .map(c => c+'99'),
        borderColor: (Array.isArray(colors) ? colors : Array(labels.length).fill(colors)),
        borderWidth:1, borderRadius:3, borderSkipped:false
      }}]
    }},
    options: opts
  }});
  return chart;
}}

let currentMode = 'count';

function buildCharts(mode) {{
  const d = (key) => mode === 'count' ? DATA[key].counts : DATA[key].values;

  // destroy existing
  Object.values(CHARTS).forEach(c => c.destroy());

  CHARTS.area = makeBarChart('aC',  CHART_W, {area_h},  DATA.area.labels,  d('area'),  pal({len(broad_areas)}));
  CHARTS.month= makeBarChart('mC',  CHART_W, {month_h}, DATA.month.labels, d('month'), S1[1], 'x');
  CHARTS.cn   = makeBarChart('cnC', CHART_W, {cn_h},    DATA.cn.labels,    d('cn'),    S1[0]);
  CHARTS.fp   = makeBarChart('fpC', CHART_W, {fp_h},    DATA.fp.labels,    d('fp'),    S1[1]);
}}

function setMode(mode) {{
  currentMode = mode;
  document.getElementById('btn-count').classList.toggle('active', mode==='count');
  document.getElementById('btn-value').classList.toggle('active', mode==='value');
  document.getElementById('val-note').style.display = mode==='value' ? 'inline' : 'none';
  buildCharts(mode);
}}

// Initial render
buildCharts('count');

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
    "drug_name": d.get("drug_name",""),
    "area":      d.get("therapeutic_area",""),
    "type":      d.get("deal_type",""),
    "equity":    d.get("equity_component",""),
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
  const cols = ['Date','Chinese Party','Foreign Party','Asset','Drug Name','Therapeutic Area','Equity Component',
                'Deal Type','Stage','Modality','Total Value','Upfront','Territory','Highlights','Source','Source URL'];
  const esc  = v => '"' + String(v||'').replace(/"/g,'""') + '"';
  const rows = DEALS_DATA.map(d => [
    d.date,d.cp,d.fp,d.asset,d.drug_name||'',d.area,d.equity||'',d.type,d.stage,d.modality||'',
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
        "asset", "drug_name", "therapeutic_area", "stage", "equity_component", "total_value_usd", "upfront_usd",
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


# ── CSV Import Mode ────────────────────────────────────────────────────────────

# Flexible column aliases — maps whatever the user put in their CSV header
# to our internal field names.  Add more synonyms here as needed.
_CSV_COL_ALIASES = {
    # chinese_party
    "chinese party":    "chinese_party",
    "chinese_party":    "chinese_party",
    "chinese company":  "chinese_party",
    "china company":    "chinese_party",
    "licensor":         "chinese_party",
    "seller":           "chinese_party",
    # foreign_party
    "foreign party":    "foreign_party",
    "foreign_party":    "foreign_party",
    "counterparty":     "foreign_party",
    "other party":      "foreign_party",
    "partner":          "foreign_party",
    "licensee":         "foreign_party",
    "buyer":            "foreign_party",
    "western party":    "foreign_party",
    # asset / drug
    "lead asset":       "asset",
    "asset":            "asset",
    "drug":             "asset",
    "drug name":        "drug_name",
    "drug_name":        "drug_name",
    "compound":         "drug_name",
    "molecule":         "drug_name",
    # date / year
    "ann. date":        "date_hint",
    "announcement date":"date_hint",
    "date":             "date_hint",
    "year":             "date_hint",
    "year-month":       "date_hint",
    # financials
    "upfront (usd)":    "upfront_usd",
    "upfront":          "upfront_usd",
    "upfront usd":      "upfront_usd",
    "milestones / total":"total_value_usd",
    "total":            "total_value_usd",
    "total value":      "total_value_usd",
    "deal value":       "total_value_usd",
    # stage
    "stage":            "stage",
    "clinical stage":   "stage",
    # modality
    "modality":         "modality",
    "drug type":        "modality",
    "molecule type":    "modality",
    # therapeutic area
    "therapeutic area": "therapeutic_area",
    "indication":       "therapeutic_area",
    "disease area":     "therapeutic_area",
    "ta":               "therapeutic_area",
    # deal type
    "deal type":        "deal_type",
    "deal_type":        "deal_type",
    "structure":        "deal_type",
    "transaction type": "deal_type",
    # territory
    "territory":        "territory",
    "geography":        "territory",
    "rights":           "territory",
    # extra freeform hints passed through as-is
    "notes":            "notes",
    "description":      "notes",
    "comments":         "notes",
}

def parse_csv_file(path: str) -> list[dict]:
    """
    Parse a user-supplied CSV into a list of row dicts with normalised field names.
    Returns one dict per data row; skips blank rows.
    """
    import csv as _csv
    # Column names that are just row-number counters — skip entirely
    _SKIP_COLS = {"#", "no", "no.", "row", "row#", "id", "index", "num", "number", "s/n", "sn"}
    _EMPTY_VALS = {"—", "nan", "NaN", "N/A", "n/a", "none", "None", "-", ""}
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = _csv.DictReader(fh)
        for raw_row in reader:
            row: dict[str, str] = {}
            for col, val in raw_row.items():
                col_clean = col.strip()
                if col_clean.lower() in _SKIP_COLS:
                    continue
                key = _CSV_COL_ALIASES.get(col_clean.lower(), col_clean.lower())
                val = val.strip() if val else ""
                if val and val not in _EMPTY_VALS:
                    if key not in row or not row[key]:
                        row[key] = val
            if any(row.values()):
                rows.append(row)
    return rows


def _fmt_row_for_prompt(row: dict) -> str:
    """Render one CSV row as a compact key:value block for the system prompt."""
    lines = []
    label_map = {
        "chinese_party":    "Chinese party",
        "foreign_party":    "Foreign party",
        "asset":            "Asset/drug",
        "drug_name":        "Drug code",
        "date_hint":        "Date hint",
        "upfront_usd":      "Upfront USD",
        "total_value_usd":  "Total value",
        "stage":            "Stage",
        "modality":         "Modality",
        "therapeutic_area": "Therapeutic area",
        "deal_type":        "Deal type (hint)",
        "territory":        "Territory (hint)",
        "notes":            "Notes/extra",
    }
    for key, label in label_map.items():
        v = row.get(key, "")
        if v:
            lines.append(f"  {label}: {v}")
    # Any leftover columns not in label_map — pass them through as extra hints
    known_keys = set(label_map.keys())
    for k, v in row.items():
        if k not in known_keys and v:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def build_csv_row_prompt(row: dict, row_num: int, total_rows: int,
                         existing_deals: list) -> str:
    """
    System prompt for a single CSV-row lookup.
    Much tighter than the full discovery prompt — one target deal per call.
    """
    row_block = _fmt_row_for_prompt(row)

    # Full dedup context using canonical names so Claude understands what's already saved.
    # Show cp / drug / month for every deal — compact but complete.
    all_saved = "\n".join(
        f"  {canon_company(d.get('chinese_party','?'))} / "
        f"{(d.get('drug_name') or d.get('asset','?'))[:30]} / "
        f"{d.get('announcement_month_year','?')}"
        for d in existing_deals
    ) or "  (none yet)"

    # Pre-compute canonical key for the CSV row to warn Claude if it looks like a dup
    cp_canon   = canon_company(row.get("chinese_party", ""))
    fp_canon   = canon_company(row.get("foreign_party", ""))
    asset_hint = row.get("asset") or row.get("drug_name", "")

    # Fields Claude should carry directly if found in CSV (don't search for these)
    carry_hints = []
    for field in ("modality", "therapeutic_area", "deal_type", "territory", "stage"):
        v = row.get(field, "")
        if v:
            carry_hints.append(f"  • {field}: use \"{v}\" directly (from CSV) unless search contradicts it")
    carry_block = "\n".join(carry_hints) if carry_hints else "  (none — infer all from search)"

    return f"""You are a senior biopharma deal analyst. Your task is to look up ONE specific deal
and save it using save_deal().

═══════════════════════════════════════════════════
 ROW {row_num} of {total_rows} — TARGET DEAL
═══════════════════════════════════════════════════
{row_block}

═══════════════════════════════════════════════════
 FIELDS TO CARRY DIRECTLY FROM CSV (no search needed)
═══════════════════════════════════════════════════
{carry_block}

═══════════════════════════════════════════════════
 YOUR TASK
═══════════════════════════════════════════════════

1. Build a tight search query combining:
     [chinese_party] + [foreign_party] + [asset/drug name] + [year from date_hint if present]
   Example: "Hengrui Ideaya SHR-4849 licensing deal 2024"

   If the first English search finds nothing useful, try:
     a) A broader query (just company names + year)
     b) search_web_cn with Mandarin: "[Chinese company] [drug] 授权 [year]"
   You have up to {3} searches max. Stop after that regardless.

2. Save exactly ONE deal using save_deal() — the deal described in the row above.
   - If you find the deal: fill in ALL fields as completely as possible from the article.
   - Prefer the article's data over the CSV where they differ (articles have fuller context).
   - But use the CSV values as ground truth for: chinese_party, foreign_party, asset/drug_name,
     upfront_usd (if numeric), and any carry-through fields listed above.
   - If you cannot confirm after {3} searches: still call save_deal() using whatever
     the CSV provides. Mark unconfirmed fields "Not disclosed". Do NOT invent facts.

3. After saving (or after {3} failed searches), call finish().

═══════════════════════════════════════════════════
 FIELD GUIDE (same schema as always)
═══════════════════════════════════════════════════

- announcement_month_year: "Month YYYY" e.g. "March 2024". If only year known, use "January YYYY".
- deal_type: {_pipe(DEAL_TYPE_ENUM)}
- chinese_party:    use EXACTLY the name from the CSV row (only fix spelling/punctuation)
- foreign_party:    use EXACTLY the name from the CSV row (only fix spelling/punctuation)
- asset:            drug code + target + indication e.g. "SHR-1819 (TSLP mAb, asthma)"
- drug_name:        code only — "SHR-1819". No descriptions.
- modality:         {_pipe(MODALITY_ENUM)}
- therapeutic_area: pick closest from — {_pipe_wrap(TA_ENUM)}
- stage:            {_pipe(STAGE_ENUM)}
- total_value_usd / upfront_usd: single clean figure e.g. "$1.2B", "$300M", "Not Disclosed" — strip all qualifiers and parenthetical notes.
- territory:        {_pipe_wrap(TERRITORY_ENUM)}
  "Worldwide" → Global. "Global ex-China (excluding ...)" → Global ex-China.
- chinese_hq:       {_pipe(CHINESE_HQ_ENUM)} — set "Yes" even for English-sounding
                    China-founded companies (ProFoundBio, Regor, AnHearts, LaNova, etc.)
- highlights:       ONE sentence — most notable fact about this deal
- source_url / source_name: article URL and publication name

═══════════════════════════════════════════════════
 DEALS ALREADY IN DATABASE — DO NOT RE-SAVE THESE
═══════════════════════════════════════════════════
Format: canonical_chinese_party / drug_or_asset / month_year
{all_saved}

NOTE: The deduplication system uses canonical company names, so "Hengrui Pharma" and
"Jiangsu Hengrui Pharmaceuticals" count as the same company. If the deal you're looking
up is clearly already present above (same companies + same asset + same timeframe), call
finish() without saving — do NOT save a duplicate.

═══════════════════════════════════════════════════
 CRITICAL
═══════════════════════════════════════════════════

- You MUST call save_deal() exactly once (or confirm duplicate and skip), then finish().
- Do NOT save unrelated deals found while searching — only the one described above.
- The deduplication system will catch accidental duplicates, so when in doubt, save."""


def run_csv_import(csv_path: str, max_searches_per_row: int = 3):
    """
    CSV import mode: reads every row from csv_path and runs a focused
    mini-agent loop for each one (up to max_searches_per_row searches per row).
    Uses the same save_deal() / is_duplicate() / canon_company() pipeline
    as the normal discovery mode — guarantees consistency.
    """
    import csv as _csv

    rows = parse_csv_file(csv_path)
    if not rows:
        print(f"  [CSV import] No data rows found in {csv_path}")
        return

    print(f"\n{'='*60}")
    print(f"  CSV IMPORT MODE")
    print(f"  File:            {csv_path}")
    print(f"  Rows to process: {len(rows)}")
    print(f"  Max searches/row: {max_searches_per_row}")
    print(f"  Existing deals:  {len(db['deals'])}")
    print(f"{'='*60}\n")

    saved_count    = 0
    skipped_count  = 0
    api_calls      = 0

    for row_idx, row in enumerate(rows, start=1):
        cp_hint    = row.get("chinese_party", "?")
        fp_hint    = row.get("foreign_party", "?")
        asset_hint = row.get("asset") or row.get("drug_name", "?")
        print(f"\n--- Row {row_idx}/{len(rows)}: {cp_hint} → {fp_hint} | {asset_hint} ---")

        # Build the initial user message as a specific search instruction
        # so Claude knows exactly what it's looking for from the very first message.
        user_msg = (
            f"Look up and save this specific deal:\n"
            f"{_fmt_row_for_prompt(row)}\n\n"
            f"Search for it, fill in all fields you can find, then call save_deal() "
            f"followed by finish()."
        )

        messages   = [{"role": "user", "content": user_msg}]
        searches   = 0
        row_done   = False
        row_saved  = False

        while searches < max_searches_per_row and not row_done:
            api_calls += 1
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=build_csv_row_prompt(row, row_idx, len(rows), db["deals"]),
                    tools=TOOLS,
                    messages=messages
                )
            except anthropic.RateLimitError:
                import time
                print("  [Rate limit — waiting 60s]")
                time.sleep(62)
                try:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=4096,
                        system=build_csv_row_prompt(row, row_idx, len(rows), db["deals"]),
                        tools=TOOLS,
                        messages=messages
                    )
                except anthropic.RateLimitError:
                    print("  [Rate limit again — skipping row]")
                    skipped_count += 1
                    row_done = True
                    continue
            except anthropic.APIError as e:
                print(f"  [API error: {e}] — skipping row")
                skipped_count += 1
                row_done = True
                continue

            messages.append({"role": "assistant", "content": response.content})

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_blocks:
                print("  [No tool calls — row done]")
                row_done = True
                continue

            tool_results = []
            for block in tool_blocks:
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

                if block.name in ("search_web", "search_web_cn", "search_web_wide"):
                    searches += 1
                if block.name == "save_deal":
                    row_saved = True
                    saved_count += 1
                if block.name == "finish":
                    row_done = True

            messages.append({"role": "user", "content": tool_results})

        # If the agent never saved despite exhausting its search budget,
        # build a fallback deal directly from the CSV row so no row is silently lost.
        if not row_saved:
            print(f"  [No save after {searches} searches — writing CSV-sourced fallback]")
            fallback = _build_fallback_deal(row)
            result = save_deal(fallback)
            print(f"  [Fallback] {result}")
            if "Saved" in result:
                saved_count += 1
            else:
                skipped_count += 1

        # Brief progress report after each row
        print(f"  [Row {row_idx} done | searches: {searches} | "
              f"DB total: {len(db['deals'])} | new this import: {len(new_deals_this_run)}]")

    save_database(db, mark_run=True)
    print(f"\n{'='*60}")
    print(f"  CSV import complete")
    print(f"  Rows processed:          {len(rows)}")
    print(f"  Deals saved (new):       {saved_count}")
    print(f"  Duplicates/skipped:      {skipped_count}")
    print(f"  Total API calls:         {api_calls}")
    print(f"  Total deals in database: {len(db['deals'])}")
    print(f"{'='*60}\n")


def _build_fallback_deal(row: dict) -> dict:
    """
    Build a minimal but valid deal dict directly from a CSV row,
    used when the agent couldn't confirm the deal via search.
    All financially uncertain fields are marked "Not disclosed".
    """
    date_raw = row.get("date_hint", "")
    # Try to parse "2024-11" → "November 2024" or "2024-11-13" → "November 2024"
    month_map = {
        "01":"January","02":"February","03":"March","04":"April",
        "05":"May","06":"June","07":"July","08":"August",
        "09":"September","10":"October","11":"November","12":"December"
    }
    mo_yr = ""
    date_match = re.match(r"(\d{4})-(\d{2})(?:-\d{2})?", str(date_raw))
    if date_match:
        yr, mo = date_match.group(1), date_match.group(2)
        mo_yr = f"{month_map.get(mo, 'January')} {yr}"
    elif re.match(r"20\d{2}$", str(date_raw)):
        mo_yr = f"January {date_raw}"  # year-only → assume January as placeholder

    def fmt_usd(v: str) -> str:
        """Turn a raw number like '1800000000' into '$1.8B'."""
        if not v or v in ("—", "nan", ""):
            return "Not disclosed"
        try:
            n = float(str(v).replace(",", "").replace("$", ""))
            if n >= 1e9:  return f"${n/1e9:.2g}B"
            if n >= 1e6:  return f"${n/1e6:.0f}M"
            return f"${n:,.0f}"
        except ValueError:
            return v if v else "Not disclosed"

    upfront   = fmt_usd(row.get("upfront_usd", ""))
    total_val = fmt_usd(row.get("total_value_usd", ""))

    asset = row.get("asset") or row.get("drug_name") or "Not disclosed"
    drug  = row.get("drug_name") or (asset.split()[0] if asset != "Not disclosed" else "Not disclosed")

    return {
        "announcement_month_year": mo_yr or "Not disclosed",
        "deal_type":        "licensing-out",   # safest default for China outbound
        "chinese_party":    row.get("chinese_party", "Not disclosed"),
        "foreign_party":    row.get("foreign_party", "Not disclosed"),
        "asset":            asset,
        "drug_name":        drug,
        "modality":         row.get("modality", "Not disclosed"),
        "therapeutic_area": row.get("notes") or "Not disclosed",
        "stage":            row.get("stage", "Not disclosed"),
        "total_value_usd":  total_val,
        "upfront_usd":      upfront,
        "territory":        "Not disclosed",
        "highlights":       f"Deal between {row.get('chinese_party','?')} and {row.get('foreign_party','?')} — details from CSV import, unconfirmed by web search.",
        "source_url":       "https://www.nature.com/articles/d41573-025-00022-4",
        "source_name":      "Nature (CSV import)",
        "chinese_hq":       "Yes",
        "_csv_fallback":    True,
    }


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
  python biopharma_agent.py --csv deals.csv          # import from CSV (3 searches/row)
  python biopharma_agent.py --csv deals.csv --csv-searches 5  # more searches per row
        """
    )
    parser.add_argument(
        "-n", "--no-query",
        action="store_true",
        help="Skip the agent search — only regenerate dashboard.html from existing database"
    )
    parser.add_argument(
        "-s", "--steps",
        type=int,
        default=None,
        metavar="N",
        help="Number of searches to perform (default: 60). Use -s 20 for a quick run, -s 46 for full coverage."
    )
    parser.add_argument(
        "-r", "--range",
        nargs="+",
        metavar="YEAR",
        help="Override search window with explicit year(s), e.g. -r 2025 or -r 2024 2025"
    )
    parser.add_argument(
        "--csv",
        metavar="FILE",
        help=(
            "CSV import mode: look up each row as a specific deal and save to database. "
            "CSV must have a header row. Recognised columns (case-insensitive): "
            "'Chinese Party', 'Counterparty'/'Foreign Party', 'Lead Asset'/'Drug Name', "
            "'Stage', 'Ann. Date'/'Date', 'Upfront (USD)', 'Milestones / Total'. "
            "Any extra columns are passed as search hints. "
            "Example: python biopharma_agent.py --csv nature_deals.csv"
        )
    )
    parser.add_argument(
        "--csv-searches",
        type=int,
        default=3,
        metavar="N",
        help="Max web searches per CSV row (default: 3). Increase for harder-to-find deals."
    )
    args = parser.parse_args()

    max_steps = args.steps if args.steps is not None else MAX_STEPS
    if args.steps is not None:
        print(f"\n  [-s] Step budget overridden: {max_steps} steps")

    db = load_database()

    # ── CSV import mode ───────────────────────────────────────────────────────
    if args.csv:
        csv_file = args.csv
        if not Path(csv_file).exists():
            print(f"\n  [ERROR] CSV file not found: {csv_file}")
            import sys; sys.exit(1)
        run_csv_import(csv_file, max_searches_per_row=args.csv_searches)
        # Regenerate outputs after import
        db = load_database()
        generate_dashboard(db, f"CSV import: {Path(csv_file).name}")
        generate_csv(db)
        print(f"\n  Open dashboard : {DASHBOARD_PATH.resolve()}")
        print(f"  Raw data       : {DB_PATH.resolve()}")
        print(f"  Deals in DB    : {len(db['deals'])}\n")
        import sys; sys.exit(0)

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

    # Build a year-aware goal that names the target years explicitly
    if args.range:
        year_mention = " and ".join(sorted(set(args.range)))
        goal_window  = f"Focus specifically on {year_mention}."
    else:
        goal_window = "Cover 2024 and 2025 comprehensively."

    RESEARCH_GOAL = (
        f"Comprehensively research ALL biopharma deals involving Chinese biotech or pharma companies. "
        f"{goal_window} "
        f"You MUST run at least 15 varied searches before finishing — "
        f"monthly sweeps first, then company-specific, then modality, then partner-focused. "
        f"There are 90-100 China deals per year — do not stop until you have exhausted the search strategy. "
        f"Prioritize: FierceBiotech, Endpoints News, BioPharma Dive, Reuters, STAT News."
    )

    if args.no_query:
        print("\n  [-n] Skipping search — regenerating dashboard from existing database...")
        search_window = db.get("last_search_window", search_window)
    else:
        # Persist the current search window so -n can re-display it
        db["last_search_window"] = window_label
        save_database(db)
        try:
            run_agent(RESEARCH_GOAL, search_window, max_steps=max_steps)
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
