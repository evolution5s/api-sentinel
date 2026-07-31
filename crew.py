"""
OpenClaw – Autonomous Operator for API Sentinel
================================================
Architektur:
  CEO (OpenClaw)  -> strategische Entscheidungen, Go/No-Go, Priorisierung
  Watcher         -> holt Exchange-Doku, erkennt Diffs (autonom)
  Classifier      -> strukturiert Diffs als bot-lesbares JSON + Severity (autonom)
  Growth          -> Content/Outreach-Entwuerfe (Freigabe noetig)
  Guardian        -> prueft alles, was Geld/Recht/Oeffentlichkeit beruehrt

Alles was Geld kostet, rechtlich bindet oder oeffentlich publiziert wird,
landet in APPROVAL_QUEUE statt ausgefuehrt zu werden.
"""

import os
import json
import hashlib
import difflib
from datetime import datetime, timezone
from pathlib import Path

import requests
from crewai import Agent, Crew, Process, Task, LLM
from crewai.tools import tool

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_KEY:
    raise SystemExit("[FATAL] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# WICHTIG: Railway-Container haben ein fluechtiges Dateisystem.
# Ohne gemountetes Volume geht der Snapshot-Stand bei jedem Deploy verloren
# und der Watcher meldet beim naechsten Lauf alles als "neu".
# In Railway: Service -> Settings -> Volumes -> Mount Path /data
STATE_DIR = Path(os.getenv("STATE_DIR", "/data"))
SNAPSHOT_DIR = STATE_DIR / "snapshots"
REPORT_DIR = STATE_DIR / "reports"
APPROVAL_QUEUE = STATE_DIR / "approval_queue.jsonl"

for d in (SNAPSHOT_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Ueberwachte Quellen. Changelog-Seiten sind deutlich signalstaerker
# als komplette Doku-Startseiten -> weniger False Positives.
WATCH_TARGETS = {
    "binance": "https://developers.binance.com/docs/binance-spot-api-docs/CHANGELOG",
    "bybit": "https://bybit-exchange.github.io/docs/changelog/v5",
    "kraken": "https://docs.kraken.com/api/docs/change-log",
}

LANDING_PAGE = "https://evolution5s.github.io/api-sentinel/"

claude_llm = LLM(model="anthropic/claude-sonnet-5", api_key=ANTHROPIC_KEY)


# ---------------------------------------------------------------------------
# Tools – hierdurch kann der Agent tatsaechlich handeln statt nur zu reden
# ---------------------------------------------------------------------------

@tool("HTTP Status Check")
def http_status(url: str) -> str:
    """Prueft eine URL per HTTP GET und liefert Statuscode, Laufzeit und Kern-Header."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "APISentinel/1.0"})
        return json.dumps({
            "url": url,
            "status_code": r.status_code,
            "elapsed_ms": int(r.elapsed.total_seconds() * 1000),
            "server": r.headers.get("server"),
            "content_type": r.headers.get("content-type"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return json.dumps({"url": url, "error": str(e), "status_code": None})


@tool("Fetch And Diff Exchange Docs")
def fetch_and_diff(exchange: str, url: str) -> str:
    """Laedt eine Exchange-Doku-Seite, vergleicht sie mit dem letzten Snapshot
    und gibt die geaenderten Zeilen zurueck. Speichert den neuen Snapshot."""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "APISentinel/1.0"})
        r.raise_for_status()
        new_text = r.text
    except Exception as e:
        return json.dumps({"exchange": exchange, "error": str(e)})

    snap_file = SNAPSHOT_DIR / f"{exchange}.txt"
    new_hash = hashlib.sha256(new_text.encode()).hexdigest()

    if not snap_file.exists():
        snap_file.write_text(new_text, encoding="utf-8")
        return json.dumps({
            "exchange": exchange,
            "status": "baseline_created",
            "hash": new_hash,
            "note": "Erster Lauf – kein Vergleich moeglich, Baseline gespeichert.",
        })

    old_text = snap_file.read_text(encoding="utf-8")
    if hashlib.sha256(old_text.encode()).hexdigest() == new_hash:
        return json.dumps({"exchange": exchange, "status": "unchanged", "hash": new_hash})

    diff = list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        lineterm="", n=2,
    ))
    snap_file.write_text(new_text, encoding="utf-8")

    # Diff kappen, damit das Kontextfenster nicht explodiert
    changed = [l for l in diff if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    return json.dumps({
        "exchange": exchange,
        "status": "changed",
        "hash": new_hash,
        "changed_line_count": len(changed),
        "diff_excerpt": changed[:120],
    })


@tool("Save Structured Alert")
def save_alert(alert_json: str) -> str:
    """Speichert einen fertig klassifizierten Alert als JSON-Datei.
    Erwartet einen JSON-String mit den Feldern:
    exchange, severity (breaking|behavioral|additive|cosmetic),
    affected_endpoints (list), summary, action_required, effective_date."""
    try:
        data = json.loads(alert_json)
    except json.JSONDecodeError as e:
        return f"FEHLER: kein gueltiges JSON ({e}). Bitte erneut senden."

    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = REPORT_DIR / f"alert_{data.get('exchange', 'unknown')}_{stamp}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Alert gespeichert: {path}"


@tool("Request Human Approval")
def request_approval(category: str, proposal: str, rationale: str) -> str:
    """Legt eine Entscheidung dem Aufsichtsrat vor, statt sie auszufuehren.
    category: spend | legal | publish | deploy | pricing
    Nutze dies IMMER bei Geldausgaben, Vertraegen, oeffentlichen Posts
    oder Produktiv-Deployments."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "proposal": proposal,
        "rationale": rationale,
        "status": "PENDING",
    }
    with APPROVAL_QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return f"Zur Freigabe eingereicht ({category}). Nicht ausgefuehrt – wartet auf Aufsichtsrat."


@tool("Read Business State")
def read_state() -> str:
    """Liest den aktuellen Geschaeftsstand: erzeugte Alerts, offene Freigaben,
    Anzahl ueberwachter Exchanges. Basis fuer jede CEO-Entscheidung."""
    alerts = sorted(REPORT_DIR.glob("alert_*.json"))
    pending = []
    if APPROVAL_QUEUE.exists():
        for line in APPROVAL_QUEUE.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if e.get("status") == "PENDING":
                    pending.append(e)
            except json.JSONDecodeError:
                continue

    conversions = os.getenv("SIGNUP_COUNT")  # aus echter Datenquelle setzen
    return json.dumps({
        "watched_exchanges": list(WATCH_TARGETS),
        "alerts_generated_total": len(alerts),
        "latest_alerts": [p.name for p in alerts[-5:]],
        "pending_approvals": len(pending),
        "pending_items": pending[:10],
        "signup_count": int(conversions) if conversions and conversions.isdigit() else None,
        "signup_source": "ENV:SIGNUP_COUNT" if conversions else "NICHT ANGEBUNDEN",
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

ceo = Agent(
    role="OpenClaw – Autonomous CEO",
    goal=(
        "Baue API Sentinel zu einem profitablen Bootstrap-Geschaeft aus. Der Wedge ist NICHT "
        "'wir erkennen Aenderungen' (das kann Visualping fuer 10 EUR/Monat), sondern "
        "'strukturierte, exchange-spezifische, bot-lesbare Klassifikation mit nachweisbar "
        "niedriger False-Positive-Rate'. Priorisiere ruecksichtslos nach Unit Economics."
    ),
    backstory=(
        "Datengetriebener SaaS-CEO. Zielmarkt sind realistisch 500-1500 Quant-Trader weltweit, "
        "die mit echtem Kapital gegen mehrere Exchanges laufen. Zielpreis 25-60 EUR/Monat. "
        "Du erfindest niemals Zahlen. Fehlen Daten, benennst du praezise, welche Datenquelle "
        "angebunden werden muss. Alles was Geld kostet oder rechtlich bindet, legst du dem "
        "Aufsichtsrat per Request Human Approval vor."
    ),
    llm=claude_llm,
    tools=[read_state, request_approval],
    verbose=True,
)

watcher = Agent(
    role="Watcher – Change Detection Engine",
    goal="Erkenne zuverlaessig jede Aenderung an den ueberwachten Exchange-API-Dokumentationen.",
    backstory=(
        "Site Reliability Engineer. Du behauptest nie einen Status, den du nicht per Tool "
        "gemessen hast. Ein unbestaetigtes 'HTTP 200' ist fuer dich ein Fehler, kein Ergebnis."
    ),
    llm=claude_llm,
    tools=[http_status, fetch_and_diff],
    verbose=True,
)

classifier = Agent(
    role="Classifier – API Change Analyst",
    goal=(
        "Wandle rohe Diffs in praezise, bot-lesbare JSON-Alerts um. Severity-Skala: "
        "breaking (Bot bricht), behavioral (Verhalten aendert sich still), "
        "additive (neue Funktion, abwaertskompatibel), cosmetic (Doku/Layout)."
    ),
    backstory=(
        "Ehemaliger Exchange-Integrationsentwickler. Du kennst den groessten Killer dieses "
        "Produkts: Alert Fatigue durch False Positives. Im Zweifel stufst du eine Aenderung "
        "lieber als 'cosmetic' ein, als einen Fehlalarm als 'breaking' zu verschicken. "
        "Copyright-Jahre, Typos und Layout sind IMMER cosmetic und werden nicht alarmiert."
    ),
    llm=claude_llm,
    tools=[save_alert],
    verbose=True,
)

growth = Agent(
    role="Growth Engine",
    goal="Erreiche die Freqtrade-/CCXT-Community mit ehrlicher, technischer Kommunikation.",
    backstory=(
        "Technischer Marketer fuer Entwicklerprodukte. Deine Zielgruppe erkennt Marketing-Sprech "
        "sofort und bestraft ihn. Du schreibst nuechtern und belegbar. Du postest nie selbst – "
        "du legst Entwuerfe zur Freigabe vor."
    ),
    llm=claude_llm,
    tools=[request_approval],
    verbose=True,
)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

task_watch = Task(
    description=(
        "1) Pruefe die Landing Page {landing} per HTTP Status Check.\n"
        "2) Fuehre fuer JEDE dieser Quellen 'Fetch And Diff Exchange Docs' aus: {targets}\n"
        "Berichte ausschliesslich gemessene Ergebnisse. Bei 'baseline_created' sage klar, "
        "dass erst der naechste Lauf Diffs liefern kann."
    ),
    agent=watcher,
    expected_output="Faktischer Messbericht: Statuscodes und pro Exchange changed/unchanged/baseline inkl. Diff-Auszug.",
)

task_classify = Task(
    description=(
        "Nimm die Diffs des Watchers. Fuer JEDE Aenderung mit Status 'changed': erzeuge einen "
        "Alert und speichere ihn per 'Save Structured Alert' mit exakt diesen Feldern:\n"
        '{{"exchange": str, "severity": "breaking|behavioral|additive|cosmetic", '
        '"affected_endpoints": [str], "summary": str, "action_required": str, '
        '"effective_date": str|null}}\n'
        "Gab es keine Aenderungen, erzeuge KEINEN Alert und sage das klar. "
        "Erfinde niemals Endpunkte, die nicht im Diff stehen."
    ),
    agent=classifier,
    expected_output="Liste der gespeicherten Alerts mit Severity, oder explizite Meldung 'keine Aenderungen'.",
)

task_ceo = Task(
    description=(
        "Lies per 'Read Business State' den Ist-Zustand. Erstelle dann einen Lagebericht:\n"
        "1) Funktioniert die Erkennungs-Pipeline technisch? (Beleg aus den Vortasks)\n"
        "2) Signup-Zahlen: falls 'NICHT ANGEBUNDEN', nenne genau eine konkrete Datenquelle, "
        "die der Aufsichtsrat anbinden soll – keine geschaetzten Zahlen.\n"
        "3) Der EINE naechste Engpass, der das Geschaeft blockiert.\n"
        "4) Maximal 3 konkrete Handlungsempfehlungen. Alles was Geld kostet, rechtlich bindet "
        "oder oeffentlich publiziert wird: per 'Request Human Approval' einreichen."
    ),
    agent=ceo,
    expected_output="Lagebericht mit Engpass-Diagnose, max. 3 Empfehlungen und eingereichten Freigaben.",
)

crew = Crew(
    agents=[watcher, classifier, ceo, growth],
    tasks=[task_watch, task_classify, task_ceo],
    process=Process.sequential,
    verbose=True,
)


if __name__ == "__main__":
    print(f"[OpenClaw] Start {datetime.now(timezone.utc).isoformat()}")
    print(f"[OpenClaw] State-Verzeichnis: {STATE_DIR} (existiert: {STATE_DIR.exists()})")

    result = crew.kickoff(inputs={
        "landing": LANDING_PAGE,
        "targets": json.dumps(WATCH_TARGETS),
    })

    print("\n" + "=" * 70)
    print(result)
    print("=" * 70)

    if APPROVAL_QUEUE.exists():
        pend = [l for l in APPROVAL_QUEUE.read_text(encoding="utf-8").splitlines() if '"PENDING"' in l]
        print(f"\n[AUFSICHTSRAT] {len(pend)} Entscheidung(en) warten auf dich.")
    print("[OpenClaw] Lauf beendet.")import os
from crewai import Agent, Crew, Process, Task, LLM

# Anthropic API Key aus den Umgebungsvariablen prüfen
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("[Error] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# Anthropic Claude 5 Sonnet als dediziertes LLM definieren
claude_llm = LLM(
    model="anthropic/claude-5-sonnet",
    api_key=ANTHROPIC_KEY
)

# Agents mit dem Claude-LLM konfigurieren
devops_agent = Agent(
    role='Lead DevOps Engine',
    goal='Maintain the Exchange API Sentinel infrastructure and verify deployment status',
    backstory='Senior Site Reliability Engineer specializing in zero-touch cloud operations.',
    llm=claude_llm,
    verbose=True
)

marketing_agent = Agent(
    role='Growth Engine',
    goal='Monitor and execute automated dispatches to drive traffic to the landing page',
    backstory='Technical marketer focused on quant trader acquisition.',
    llm=claude_llm,
    verbose=True
)

ceo_agent = Agent(
    role='Autonomous CEO',
    goal='Evaluate validation threshold (5 conversions in 96h) and trigger backend deployment if passed',
    backstory='Data-driven SaaS CEO focused strictly on unit economics and zero-human operations.',
    llm=claude_llm,
    verbose=True
)

# Tasks definieren
task_check_status = Task(
    description='Verify that https://evolution5s.github.io/api-sentinel/ is live and responding with HTTP 200.',
    agent=devops_agent,
    expected_output='Status report of the live landing page.'
)

task_eval_conversions = Task(
    description='Check conversion metrics. If >= 5 signups detected within 96 hours, trigger Hetzner deployment sequence.',
    agent=ceo_agent,
    expected_output='Go/No-Go Decision Report'
)

# Crew instanziieren
crew = Crew(
    agents=[devops_agent, marketing_agent, ceo_agent],
    tasks=[task_check_status, task_eval_conversions],
    process=Process.sequential
)

if __name__ == "__main__":
    print("[Railway Worker] OpenCrew Autonomous Loop Started (Anthropic Claude)...")
    crew.kickoff()
    print("[Railway Worker] Execution finished.")
