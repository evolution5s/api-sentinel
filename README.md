# API Sentinel

> **Hinweis zur Pflege:** Dieses Dokument beschreibt den Code-Stand in `crew.py`,
> `tools.py`, `holding.py`, `scoring.py`, `crewai_patches.py` und
> `agent_profile.json`. Insbesondere Kapitel 3 (Agenten) und Kapitel 9
> (Modelle/Token/Limits) müssen aktualisiert werden, sobald sich Rollen,
> Tools, Prompts oder Limits ändern - sie sind eine Momentaufnahme, keine
> automatisch generierte Doku.

## Inhaltsverzeichnis

1. [Überblick](#1-überblick)
2. [Ablauf eines Zyklus](#2-ablauf-eines-zyklus)
3. [Die Agenten im Detail](#3-die-agenten-im-detail)
4. [Systemvorgaben und Leitplanken](#4-systemvorgaben-und-leitplanken)
5. [Der Build-Measure-Learn-Loop (Hypothesis Engine)](#5-der-build-measure-learn-loop-hypothesis-engine)
6. [Channel-Auswahl und Content-Erstellung](#6-channel-auswahl-und-content-erstellung)
7. [Governance-Ebene: Main-CEO / Holding](#7-governance-ebene-main-ceo--holding)
8. [Mensch im Loop: Freigabe-Queue und Telegram-Fernsteuerung](#8-mensch-im-loop-freigabe-queue-und-telegram-fernsteuerung)
9. [Modelle, Tokens und Limits](#9-modelle-tokens-und-limits)
10. [CrewAI: Funktion und Implementierung](#10-crewai-funktion-und-implementierung)
11. [Persistenz und Datenmodell](#11-persistenz-und-datenmodell)
12. [Deployment und Kommunikation mit Railway](#12-deployment-und-kommunikation-mit-railway)
13. [Tests](#13-tests)
14. [Umgebungsvariablen](#14-umgebungsvariablen)
15. [Bekannte Grenzen und offene Punkte](#15-bekannte-grenzen-und-offene-punkte)

---

## 1. Überblick

API Sentinel ist eine autonome, mehrstufige Simulation eines kleinen
Software-Unternehmens (Holding mit einer Subsidiary), die alle 2 Stunden als
Cron-Job auf Railway läuft und dabei einen strikten
**Build-Measure-Learn-Loop** (Lean Startup) auf Basis von vier
[CrewAI](https://github.com/crewAIInc/crewAI)-Agenten durchläuft, die auf
Anthropic Claude aufsetzen.

Die eigentliche Geschäftsidee der ersten Subsidiary (`api-sentinel`) ist ein
Exchange-API-Change-Monitoring-Dienst für die Freqtrade/CCXT-Quant-Bot-
Community - aber das System befindet sich bewusst noch in der
**Hypothesis-Testing-Phase**: Es baut noch kein Produkt, sondern testet
systematisch Annahmen über Zielgruppe, Kanal, Preis und Value Proposition,
bevor überhaupt Entwicklungsaufwand investiert wird.

Zentrale Design-Prinzipien, die sich durch den gesamten Code ziehen:

- **Nichts wird erfunden.** Reichweiten-, Konversions- und Umsatzzahlen
  kommen ausschließlich aus echten Datenquellen (GitHub Issues, öffentliche
  Reddit/Discord/Telegram-APIs, echte Signups) oder werden explizit als
  Schätzung mit Quelle gekennzeichnet. Kein Tool "rät" einen Wert.
- **Kein autonomes Geld ausgeben, keine rechtliche Verpflichtung, keine
  Veröffentlichung ohne Menschen.** Jede Aktion mit einer dieser
  Eigenschaften geht durch eine Freigabe-Queue, die ein Mensch (das
  "Aufsichtsrat"/Board) über ein CLI-Tool oder per Telegram-Antwort
  entscheidet.
- **Deterministische Entscheidungen statt Modell-Bauchgefühl** überall dort,
  wo es um Geld oder Fortbestand einer Hypothese geht: Score-Formel,
  Break-even-Berechnung und das Vier-Wege-Outcome (`build`/`test_further`/
  `pivot`/`bury`) sind reine Python-Funktionen in `scoring.py`, nicht etwas,
  das ein Agent sich aussuchen kann.
- **Strukturierte Übergaben statt Freitext.** Sub-CEO → Growth/Dev
  (`task_orders.jsonl`), Sub-CEO → Main-CEO (`status_reports.jsonl`),
  Main-CEO → Sub-CEO (`strategic_directions.jsonl`) sind alles feste
  JSONL-Records mit Pflichtfeldern, nicht Prosa, die die jeweils andere
  Seite interpretieren müsste.
- **Alles ist Klartext-JSONL unter einem `STATE_DIR`.** Kein Datenbank-
  Server, keine Bindung an einen bestimmten Hosting-Anbieter für den
  Zustand - nur Dateien, die sich mit jedem Texteditor lesen lassen (siehe
  Kapitel 11).

---

## 2. Ablauf eines Zyklus

Ein Zyklus ist ein einzelner Aufruf von `python crew.py`, den Railway per
Cron alle 2 Stunden auslöst (`railway.json`, `cronSchedule: "0 */2 * * *"`).
Ablauf im Detail (siehe `crew.py`, Block `if __name__ == "__main__":`):

1. **Persistenz-Check** (`check_state_persistence()`) - läuft als
   allererstes, noch vor allem anderen, damit eine Warnung auch dann noch
   nach Telegram gelangt, wenn der Rest des Zyklus danach fehlschlägt.
   Siehe Kapitel 9.7.
2. **Telegram-Kommandos verarbeiten** (`process_telegram_commands()`) - läuft
   als nächstes, auch wenn das System pausiert ist, damit ein "start"
   überhaupt gesehen werden kann. Siehe Kapitel 8.
3. **Pause-Check** (`is_system_paused()`) - ist das System per Telegram
   `stop` pausiert, wird der komplette restliche Zyklus übersprungen und
   eine entsprechende Telegram-Nachricht verschickt. Kein `crew.kickoff()`,
   keine Kosten.
4. **`crew.kickoff()`** - führt die fünf Tasks sequenziell aus (siehe
   Kapitel 10, Prozess-Modell):
   1. `task_channel_strategy` (Sub-CEO) - immer, nicht überspringbar.
   2. `task_growth` (Growth) - übersprungen, falls das Zyklus-Token-Budget
      bereits ausgeschöpft ist (`ConditionalTask`, siehe Kapitel 9).
   3. `task_ceo` (Sub-CEO, Build-Measure-Learn) - ebenso conditional.
   4. `task_main_ceo_review` (Main-CEO) - ebenso conditional.
   5. `task_dev` (Dev) - ebenso conditional.
5. **`send_cycle_summary()`** - läuft in jedem Fall, auch wenn
   `crew.kickoff()` eine Exception geworfen hat (dann wird der Fehler explizit
   in der Telegram-Nachricht gemeldet statt verschluckt). Postet eine
   Zusammenfassung nach Telegram (Token-/Kosten-Kopfzeile, Task-Outputs,
   Sicherheitslimits, Persistenz-/Malformed-Tool-Call-Hinweise, offene
   Freigaben) plus eine separate formatierte Token-/Kosten-Tabelle (Kapitel
   9.6), und speichert eine gekürzte Version als Kontinuitätsnotiz für den
   nächsten Zyklus (`last_cycle_note.txt`).

Wichtig: `crew.kickoff()` selbst wird in `checkup.py` **nie** aufgerufen -
der Testsuite würde damit echte Anthropic-API-Kosten verursachen. Tests
rufen jedes Tool einzeln über sein `.run(...)`-Interface auf.

---

## 3. Die Agenten im Detail

> Alle vier Agenten werden in `crew.py` mit `Agent(...)` instanziiert und
> laufen sequenziell (`Process.sequential`) in genau der Reihenfolge, in der
> ihre Tasks unten aufgeführt sind. Jeder Agent bekommt sein LLM, seine
> Limits (`max_iter`, `max_execution_time`) aus `agent_profile.json` (siehe
> Kapitel 9) sowie fest codiert `max_rpm=20` und `max_retry_limit=1`. Alle
> vier tragen außerdem denselben Effizienz-Grundsatz wörtlich in ihrer
> Backstory: Tokens/Iterationen sind ein echter, gemessener Kostenfaktor,
> kein freies Gut - korrekt in so wenigen Tool-Aufrufen wie tatsächlich
> nötig fertig zu werden ist das eigentliche Ziel, die Caps (`max_iter`
> etc.) sind eine harte Obergrenze gegen außer Kontrolle geratene Kosten,
> kein Zielwert, den es auszuschöpfen gilt.

### 3.1 Growth Engine / Dev Relations (`growth_agent`)

**Rolle im Loop:** Führt `task_growth` aus - misst reale Reichweite für
aktive Hypothesen und erstellt organischen Community-Content.

**Goal:** Genuine, organische Community-Inhalte passend zur aktuell aktiven
Hypothese entwerfen, echte Reichweite nach Freigabe und Veröffentlichung
messen, und dabei die Posting-Historie jedes Accounts innerhalb der Regeln
seiner Community sowie der 90/10-Genuine-zu-Werbe-Quote halten.

**Backstory (Kernpunkte):** Technischer Marketer für die Freqtrade/CCXT- und
Quant-Bot-Communities. Entwirft echte Posts (`draft_content`) in eigenen,
einfachen Worten - veröffentlicht sie aber nie selbst; das bestätigt immer
ein Mensch per Telegram-`posted:`-Antwort. Jede Reichweitenzahl kommt aus
`read_channel_metrics`, nie eine Schätzung aus der Luft. Ein Account pro
Produkt pro Plattform: vor jedem Post werden die aktuellen Regeln der
jeweiligen Community geprüft (`rules_checked`/`rules_notes` bei *jedem*
einzelnen Entwurf) sowie `check_community_risk` für die Removal-Historie
dieser Community. `get_account_stats` hält die 90/10-Quote pro Plattform im
Blick. Ein Produktlink erscheint nie, bevor `landing_page_live=true` für die
jeweilige Hypothese bestätigt ist, und bevorzugt dann einen Profil-/
Signatur-Link statt eines Inline-Links. Konkrete Arbeit kommt vom Sub-CEO als
Task-Order (`read_task_orders(to_role='growth', ...)`) - das ist die
maßgebliche Anweisung, keine Paraphrase davon.

**Tools:**
| Tool | Zweck |
|---|---|
| `request_approval` | Freigabe für Content/Spend beantragen |
| `read_channel_metrics` | Reale Reichweite pro Kanal messen (Reddit/Discord/Telegram-Auto-Fetch, X/Landing-Page manuell) |
| `read_channels` | Aktuellen Kanal-Roster lesen |
| `read_state` | Pipeline-Zustand (Signups, offene Freigaben, Hypothesenzahl) |
| `read_hypotheses` | Aktive Hypothesen lesen |
| `read_task_orders` | Offene Aufträge vom Sub-CEO abrufen |
| `complete_task_order` | Auftrag mit echtem Ergebnis abschließen |
| `draft_content` | Organischen Community-Post entwerfen (siehe Kapitel 6) |
| `read_content_drafts` | Eigene Entwürfe/Status (drafted/posted/removed) lesen |
| `check_community_risk` | Removal-Historie einer Community prüfen |
| `get_account_stats` | Genuine/Werbe-Verhältnis pro Plattform prüfen |
| `log_research_finding` / `read_research_findings` | Research-Evidence-Tier (siehe Kapitel 5) |
| `read_subsidiary_policies` | Prüft, ob z.B. bezahlte Kanäle für diese Subsidiary erlaubt sind |
| `read_knowledge_base` | Distillierte Erkenntnisse zu Themen/Kanälen/Taktiken lesen (siehe Kapitel 5.6) |
| `propose_idea` | Marktlücke außerhalb der eigenen Hypothese an den Main-CEO melden (Kapitel 7.2) |

**Was der Agent explizit NICHT tut:** selbst posten (immer Mensch-bestätigt),
bezahlte Werbung selbst schalten oder vorschlagen (geht über
`task_channel_strategy`/den Sub-CEO und eine `spend`-Freigabe).

### 3.2 Landing Page & Backend Developer (`dev_agent`)

**Rolle im Loop:** Führt `task_dev` aus - letzter Task im Zyklus, setzt
konkrete Bauaufträge des Sub-CEO als Pull Requests um.

**Goal:** Neue Landing-Page-Varianten als Pull Requests umsetzen, wann immer
eine Hypothese das erfordert.

**Backstory (Kernpunkte):** Liefert Varianten ausschließlich als PRs - merged
oder macht nie selbst etwas live; das bleibt immer ein separater,
menschlich freigegebener Schritt. Konkrete Arbeit kommt als Task-Order vom
Sub-CEO (`read_task_orders(to_role='dev', ...)`). Für jeden Auftrag, der an
ein `build`-Outcome hängt, wird über `check_approval_status` selbst
verifiziert, dass die zugehörige Freigabe tatsächlich `approved` ist, bevor
ein PR geöffnet wird - nimmt nie ungeprüft die Behauptung eines anderen
Agenten. Ruft `complete_task_order` mit dem echten Ergebnis (z.B. der
PR-URL) auf, sobald fertig.

**Tools:**
| Tool | Zweck |
|---|---|
| `open_pull_request` | Neue Datei auf neuem Branch gegen `main` als PR öffnen (nie Merge, nie direkte `index.html`-Änderung) |
| `read_task_orders` | Offene Dev-Aufträge lesen |
| `complete_task_order` | Auftrag mit PR-URL (oder Begründung, warum nichts geöffnet wurde) abschließen |
| `check_approval_status` | Freigabestatus einer Genehmigung selbst verifizieren |

`open_pull_request` benötigt `GITHUB_TOKEN` (repo-scoped) in der Umgebung;
ohne Token liefert es einen klaren Fehler statt so zu tun, als hätte es
funktioniert.

### 3.3 Sub-CEO von API Sentinel (`ceo_agent`)

**Rolle im Loop:** Läuft **zweimal** pro Zyklus - einmal als
`task_channel_strategy` (ganz am Anfang, immer, nicht überspringbar) und
einmal als `task_ceo` (Build-Measure-Learn, nach Growth).

**Goal:** Fällige Hypothesen bewerten, Folge-Hypothesen formulieren und API
Sentinel wachsen lassen, indem echte, lösenswerte Probleme für die
Zielgruppe validiert werden - Monetarisierung (Break-even-Ökonomie,
Defensibility, Pricing) ist ein verpflichtender Filter, den jede Hypothese
erfüllen muss, nicht das, worauf hin optimiert wird - ohne jemals eine Zahl
zu erfinden, die menschliche Freigabe-Queue zu umgehen oder allein eine
fundamentale Strategieänderung zu entscheiden.

**Backstory (Kernpunkte):** Datengetriebener SaaS-Sub-CEO mit striktem
Build-Measure-Learn-Loop für eine Subsidiary der Holding. Hat keinen Zugriff
auf Zahlungsmittel; jede Aktion mit Kosten, rechtlicher Verpflichtung oder
Öffentlichkeitswirkung muss zuerst durch `request_approval`. Berechnet
Scores nur über `evaluate_hypothesis` und Break-even-Nutzerzahlen nur über
`compute_break_even` - nie im Kopf. Jede Hypothese bekommt eine eigene,
zu ihr passende ökonomische Messlatte, kein fester globaler Zielwert.
Dieses Geschäft wird von KI-Agenten gebaut und betrieben - Kostenschätzungen
müssen das widerspiegeln (Dev-Agent-Token-Spend + minimale laufende Infra),
nie klassische Agentur-/Freelancer-/Angestellten-Kostenannahmen; ein Build,
der ein menschliches Team Tausende kosten würde, kostet dieses System
typischerweise ein paar Dollar in Tokens (Kapitel 5.1, Kapitel 15). Liest
die aktuelle strategische Ausrichtung des Main-CEO
(`read_strategic_direction`) zu Zyklusbeginn und bezieht sie als Rahmen ein
- nicht als Befehl, der taktische Kanal-/Größen-Entscheidungen übersteuert.
Gibt konkrete Arbeit an Growth/Dev als Task-Order weiter
(`file_task_order`), statt sie aus einem Report ableiten zu lassen, und
berichtet genauso strukturiert zurück an den Main-CEO
(`file_status_report`) - immer bei einem `build`-Outcome, sonst wann immer
etwas wirklich eine Entscheidung von oben braucht. Operiert strikt innerhalb
des aktuellen Geschäftsmodells von API Sentinel - meldet bei einem
fundamentalen Strategieproblem (`check_escalation`) einen strukturierten
Pivot-Vorschlag beim Main-CEO (`file_pivot_proposal`), statt allein zu
pivotieren oder direkt ans Board zu eskalieren. Kann historische Daten
anderer Subsidiaries über `search_research_archive` ziehen, kontaktiert aber
nie deren Sub-CEO direkt - das läuft immer über den Main-CEO
(`file_cross_subsidiary_request`). Liest die eigenen Policies der Subsidiary
(`read_subsidiary_policies`) als harte Nebenbedingung, nicht als etwas, um
das man argumentativ herumkommt. Nutzt `read_due_hypotheses` statt selbst
verstrichene Zeit zu berechnen, damit keine aktive Hypothese unbemerkt über
ihre eigene Zeitbox (`duration_days`, oder einen frühen
`sample_size_trigger`) hinaus läuft, ohne durch die Vier-Wege-Bewertung zu
gehen. Prüft `read_knowledge_base`, bevor eine neue Hypothese vorgeschlagen
wird - derselbe günstige Erst-Schritt-Gedanke wie beim externen
Research-Evidence-Tier, nur aus den eigenen vorherigen Hypothesen dieser
Subsidiary distilliert - und schreibt einen neuen Eintrag
(`write_knowledge_entry`), sobald eine Hypothese zu build/pivot/bury
aufgelöst wird, damit auch das "warum es nicht passte" eines Pivots erhalten
bleibt, nicht nur die Erfolge. Rangiert konkurrierende Hypothesen-Ideen nach
`impact_score`/`confidence_score` genauso wie Kanäle rangiert werden, da nur
`MAX_ACTIVE_HYPOTHESES` gleichzeitig laufen können - sich über zu viele
Hypothesen gleichzeitig zu verzetteln gilt als der häufigere Fehler als die
falsche zu wählen. Dieses Ranking steht im Dienst der Validierung eines
echten Problems, nicht der direkten Umsatzjagd: `impact_score` soll
widerspiegeln, wie überzeugend eine Hypothese ein echtes Nutzerproblem
validieren - oder eindeutig widerlegen - würde, nicht wie leicht sie sich
monetarisieren ließe oder welches Experiment die interessantesten Daten
liefern würde. Die ökonomischen Felder (`break_even_users`,
`defensibility_notes`, `pricing_tier_reasoning`) bleiben der verpflichtende
Filter, den jede Hypothese erfüllen muss, egal wie vielversprechend das
Value-Signal aussieht - sie sind das Tor, nicht das Ranking-Kriterium
selbst. Derselbe Maßstab gilt für Kanal-Wahl und Pivot-Richtung, wann immer echtes Ermessen
im Spiel ist.

**Tools:**
| Tool | Zweck |
|---|---|
| `read_state` | Pipeline-Zustand lesen |
| `read_hypotheses` / `write_hypothesis` | Hypothesen lesen/anlegen/aktualisieren |
| `read_due_hypotheses` | Deterministisch prüfen, welche aktiven Hypothesen fällig sind (Zeitbox oder Sample-Trigger erreicht) |
| `evaluate_hypothesis` | Echten Score + Vier-Wege-Outcome berechnen |
| `check_escalation` | Rollierenden Score-Durchschnitt der Hypothesen-Linie prüfen |
| `compare_channel_performance` | Kanäle nach echtem Durchschnitts-Score ranken |
| `request_approval` | Freigabe beantragen |
| `read_channels` / `write_channel` | Kanal-Roster (Bullseye) pflegen |
| `compute_break_even` | Break-even-Nutzerzahl vorab berechnen |
| `file_task_order` / `read_task_orders` | Aufträge an Growth/Dev vergeben/verfolgen |
| `file_status_report` | Strukturiert an Main-CEO berichten |
| `read_strategic_direction` | Aktuelle Main-CEO-Vorgabe lesen |
| `file_pivot_proposal` | Strukturierten Pivot-Vorschlag beim Main-CEO einreichen |
| `file_cross_subsidiary_request` | Anfrage an andere Subsidiary über Main-CEO stellen |
| `search_research_archive` | Historisches Wissen holdingsweit durchsuchen |
| `read_subsidiary_policies` | Eigene Policies (bezahlte Kanäle, Cold Email, ...) lesen |
| `read_content_drafts` | Was Growth entworfen/gepostet hat, einsehen |
| `log_research_finding` / `read_research_findings` | Research-Evidence-Tier |
| `read_knowledge_base` / `write_knowledge_entry` | Distillierte Erkenntnisse lesen/schreiben (siehe Kapitel 5.6) |
| `propose_idea` | Marktlücke außerhalb dieser Subsidiary an den Main-CEO melden (Kapitel 7.2) |

### 3.4 Main-CEO der Open Claw Holding (`main_ceo_agent`)

**Rolle im Loop:** Führt `task_main_ceo_review` aus - Governance-Review nach
Sub-CEO und Growth, vor Dev.

**Goal:** Die Subsidiaries der Holding strategisch **Richtung Validierung
echter, lösenswerter Probleme** für ihre Zielgruppen steuern - nicht
Richtung endloser Experimente, aber auch nicht Richtung Umsatzjagd als
Selbstzweck: Pivot-Vorschläge, Cross-Subsidiary-Anfragen und
Status-Reports der Sub-CEOs prüfen, sicherstellen, dass jeder Subsidiary
mindestens einmal gesagt wurde, dass der Punkt ein wirklich nützliches,
monetarisierbares Produkt ist, im Blick behalten, ob jede Subsidiary
tatsächlich Fortschritt macht - statt sich im Kreis zu drehen -, strategische
Ausrichtung setzen, wo tatsächlich gerechtfertigt, das Subsidiary-Register
(inkl. Dormant-Lifecycle) pflegen, und bei allem mit echter Tragweite das
Aufsichtsrat einbeziehen - nie allein Entscheidungen mit großer Wirkung
treffen.

**Backstory (Kernpunkte):** Leitet die Holding über den einzelnen Sub-CEOs.
Mit heute nur `api-sentinel` registriert haben die meisten Zyklen nichts zu
prüfen - das ist erwartet, kein Fehlerzeichen. Erfindet nie eine
Entscheidung, nur um etwas zu berichten zu haben; "nichts diesen Zyklus" ist
eine vollständige, gültige Antwort. Liest Sub-CEO-Status-Reports
(`read_status_reports`) - besonders solche, die eine Entscheidung
verlangen, z.B. jedes `build`-Outcome landet hier immer, bevor irgendjemand
zu bauen beginnt - und quittiert sie nach Prüfung
(`acknowledge_status_report`), damit sie nicht jeden Zyklus erneut
auftauchen. Jede Subsidiary bekommt mindestens einmal eine strategische
Ausrichtung (`set_strategic_direction`) - geprüft über
`read_strategic_direction`, nicht implizit vorausgesetzt - die klarstellt,
dass der eigentliche Punkt das Lösen eines echten Problems für echte
Nutzer ist, mit Monetarisierung als verpflichtendem, nicht verhandelbarem
Filter, den jede Hypothese erfüllen muss (die bestehende Break-even-/
Defensibility-/Pricing-Ökonomie) - nicht als das, was um seiner selbst
willen verfolgt wird; das ist eine einmalige Baseline pro Subsidiary, keine
taktische Mikrosteuerung. Darüber hinaus setzt es eine NEUE Ausrichtung nur,
wenn es einen echten Grund dafür gibt - ein Marktwandel, ein Muster über
mehrere Berichte hinweg, eine gerade getroffene Entscheidung - das bleibt
die Ausnahme, keine Pflichtübung; es übersteuert nie das eigene taktische
Ermessen des Sub-CEO, es ist der Rahmen, den der Sub-CEO liest und
innerhalb dessen er arbeitet. Prüft außerdem jeden Zyklus
`assess_subsidiary_trajectory` als Gesundheits-Check auf echten Fortschritt
- keinen zweiten Umsatz-Tracker -, unabhängig davon, ob etwas eskaliert
wurde: eine Subsidiary kann unbegrenzt weiterlaufen, ohne dass je eine
formale Eskalation feuert, während sie sich tatsächlich nur im Kreis dreht
(evaluierte Hypothesen häufen sich, ohne je eine Auflösung Richtung
validiertem `build` oder klarem Kill zu erreichen, oder dieselbe Frage wird
über wiederholte ergebnislose Pivots/Test-further-Verlängerungen immer
wieder neu gestellt); sagt das explizit im eigenen Bericht, wenn das Muster
das nahelegt, ohne selbst einen neuen Eskalationsrecord anzulegen und ohne
eine auf dem Papier umsatzpositiv wirkende Hypothese als Beleg für echten
Fortschritt zu werten, wenn das zugrunde liegende Problem nie wirklich
validiert wurde - `check_escalation` (die Rolling-Score-Prüfung des Sub-CEO
pro Hypothesen-Linie) bleibt das Einzige, was tatsächlich einen formalen
Pivot-Vorschlag auslöst. Das Instanziieren einer neuen Subsidiary,
neuer Agenten oder neuer externer Tools läuft immer über
`request_approval` an das Aufsichtsrat, ohne Ausnahme - `register_subsidiary`
selbst erzwingt das, aber die gleiche Disziplin gilt für jede Entscheidung
dieser Rolle. Setzt die generellen Policies einer Subsidiary
(`update_subsidiary_policies`) ebenfalls nur hinter einer bereits
freigegebenen `request_approval` - jede Subsidiary startet konservativ
(alles aus/niedrig) und lockert nur mit echtem, board-freigegebenem Grund.

**Tools:**
| Tool | Zweck |
|---|---|
| `read_subsidiaries` / `register_subsidiary` / `set_subsidiary_status` | Subsidiary-Register pflegen |
| `read_pivot_proposals` / `decide_pivot_proposal` | Pivot-Vorschläge der Sub-CEOs entscheiden |
| `read_cross_subsidiary_requests` / `resolve_cross_subsidiary_request` | Cross-Subsidiary-Anfragen routen/entscheiden |
| `read_status_reports` / `acknowledge_status_report` | Sub-CEO-Berichte prüfen/quittieren |
| `set_strategic_direction` / `read_strategic_direction` | Ausrichtung für einen Sub-CEO setzen/prüfen ob je eine gesetzt wurde |
| `assess_subsidiary_trajectory` | Subsidiary-weite Outcome-Zählung - Gesundheits-Check auf echten Fortschritt, jeden Zyklus (siehe Kapitel 7) |
| `search_research_archive` | Holdingsweites Wissen durchsuchen |
| `request_approval` | Freigabe ans Aufsichtsrat beantragen |
| `read_subsidiary_policies` / `update_subsidiary_policies` | Generelle Vorgaben einer Subsidiary lesen/ändern (approval-gated) |
| `propose_idea` / `read_ideas` / `route_idea` | Idee-Intake lesen und routen (Kapitel 7.2) |

---

## 4. Systemvorgaben und Leitplanken

Es gibt zwei Ebenen von Vorgaben, die klar getrennt sind:

### 4.1 Holdingweite, universelle Ziele (nicht verhandelbar, nicht pro Subsidiary abschaltbar)

Diese sind in den Backstories der Agenten verankert, nicht als abschaltbare
Policy modelliert: kein autonomes Ausgeben von Geld, keine Dateneingriffe
ohne Freigabe, keine Umgehung der menschlichen Freigabe-Queue, keine
fundamentale Strategieänderung ohne Main-CEO/Board, keine neuen externen
Integrationen/Agenten/Tools ohne Freigabe.

### 4.2 Generelle Prerequisites pro Subsidiary (einstellbar, aber approval-gated)

`holding.py::SUBSIDIARY_POLICY_DEFAULTS` - jede Subsidiary hat diese vier
Schalter, alle konservativ vorbelegt:

| Policy | Default | Bedeutung |
|---|---|---|
| `paid_channels_allowed` | `False` | Ein bezahlter Kanal (`is_paid=true`) kann nicht auf `status='testing'` wechseln, solange dies `false` ist - geprüft in `write_channel`, *vor* der bestehenden Spend-Freigabe-Prüfung |
| `cold_email_allowed` | `False` | Aktuell ohnehin irrelevant, da Cold Email nicht in der unterstützten Plattform-Liste (`CONTENT_PLATFORMS`) enthalten ist - rechtliches Risiko (EU ePrivacy/DSGVO, deutsches UWG), kein Stil-Entscheid |
| `data_collection_allowed` | `False` | Reserviert für zukünftige Nutzung |
| `risk_tolerance` | `"low"` | Reserviert für zukünftige Nutzung |

Ändern nur über `update_subsidiary_policies` - erfordert eine bereits
freigegebene `request_approval`, exakt dieselbe Disziplin wie
`register_subsidiary`. Jede Änderung wird mit Zeitstempel, geändertem Feld,
Freigabe-ID und Begründung in `policy_history` auf dem Subsidiary-Record
protokolliert.

**Wichtig:** Die konkrete Kanalliste für API Sentinel (r/algotrading,
r/quantfinance, r/quant, QuantConnect Forum/Discord, Elite Trader,
Trade2Win, quant.stackexchange.com) ist bewusst *nicht* Teil dieser
holdingweiten Policies - sie steht im Task-Text von `task_channel_strategy`
in `crew.py`, weil sie API-Sentinel-spezifisch ist. Jede neue Subsidiary
bekäme ihre eigene, andere Kanalliste.

---

## 5. Der Build-Measure-Learn-Loop (Hypothesis Engine)

Kernstück ist `task_ceo` (Sub-CEO) zusammen mit den reinen, testbaren
Funktionen in `scoring.py`. Ablauf pro Hypothese:

### 5.1 Anlegen einer Hypothese

Jede neue Hypothese (Bootstrap in Schritt 0 oder Pivot-Folgehypothese in
Schritt 5 von `task_ceo`) braucht **vor** dem Start des Tests fixierte
Ökonomie, die danach nie an das Ergebnis angepasst wird:

- `hypothesis_type`: `value` (löst ein echtes Nutzerproblem) oder `growth`
  (hilft bei Distribution/Skalierung von bereits Validiertem).
- `impact_score`, `confidence_score` - eigenes ehrliches Urteil, gleiche
  Form wie bei einem Kanal (Kapitel 6.1) - das Ranking-Signal, mit dem
  konkurrierende Hypothesen-Ideen gegeneinander priorisiert werden, sobald
  mehr Ideen als Kapazität vorhanden sind (Kapitel 5.8).
- `estimated_build_cost`, `price_point_monthly`, `break_even_horizon_months`
  - grob geschätzte Kosten/Preis/Zeitrahmen für das *echte* Produkt/Feature,
    nicht für den Test selbst. `estimated_build_cost` muss auf echten
    KI-Agenten-Ökonomie beruhen - Dev-Agent-LLM-Aufrufe plus etwaige echte
    laufende Infra-Kosten (Hosting, Domain) - **nie** auf einem
    Marktpreis/Agentur-/Freelancer-Satz; dieses System wird von KI-Agenten
    gebaut und betrieben, eine Landing Page/ein Signup-Formular/ein
    kleines Backend-Skript kostet hier realistisch einen niedrigen
    einstelligen Dollarbetrag in Tokens, keine Hunderte oder Tausende.
    `write_hypothesis` verlangt dafür ein Pflichtfeld `build_cost_reasoning`
    (Aufschlüsselung der echten Kostenkomponenten) und lehnt Schätzungen
    über `SIMPLE_BUILD_COST_CEILING` (10,0 USD) ohne substanzielle
    Begründung (mind. `BUILD_COST_JUSTIFICATION_MIN_LENGTH` = 80 Zeichen,
    echter zusätzlicher Token-/Iterationsaufwand, nicht "fühlt sich teurer
    an") ab. `break_even_horizon_months` defaultet konzeptionell auf 1
    Monat - bei so günstigen Builds soll sich eine wirklich validierte Idee
    schnell amortisieren; länger als 1 Monat braucht ebenfalls
    `build_cost_reasoning` (z.B. echte laufende Infra-Kosten), nicht
    Gewohnheit. Siehe Kapitel 15 zur Entstehungsgeschichte dieser Regel
    (eine $15.000-Fehlschätzung mit Agentur-Logik verzerrte
    `break_even_users` auf 52 statt der realistischen niedrigen einstelligen
    Zahl).
- `break_even_users` - **nie** von Hand geschätzt, sondern via
  `compute_break_even()` (`scoring.compute_break_even_users`):
  `ceil(build_cost / (price_point_monthly * horizon_months))`.
- `duration_days` - die verpflichtende Zeitbox (immer erforderlich), plus
  optional `sample_size_trigger` für eine frühere Fälligkeit (Kapitel 5.6).
- Genau eine ungetestete Variable pro Test: bei einem Pivot-Folgetest
  `pivot_variable_changed`; beim ersten Versuch einer Linie (kein
  `prior_hypothesis_id` gesetzt, auch die allererste Hypothese überhaupt)
  `primary_variable_tested` - beide aus derselben Menge (`audience`/
  `price`/`copy`/`channel`/`timing`). Nie mehrere echt ungetestete
  Variablen in einen ersten Test bündeln (z.B. neue Zielgruppe *und* neuer
  Preis gleichzeitig) - sonst lässt sich das Ergebnis keiner der beiden
  Änderungen eindeutig zuordnen. Optional dazu `holding_constant_notes` -
  was bewusst konstant gehalten wird.

Dazu optionale, freitextliche Reasoning-Felder (keine neue Pass/Fail-Hürde,
nur dokumentierte Abwägung): `defensibility_notes` (könnte ein Solo-
Entwickler das an einem Nachmittag mit einem LLM nachbauen?),
`pricing_tier_reasoning` (niedriger Preis braucht großes, scharfes
Painpoint + Volumen; höherer Preis braucht weniger Volumen, aber
langsamere Adoption), `expansion_notes` (Upsell-/B2B-Potenzial, rein
zukunftsgerichtet), `channel_fit_reasoning` (warum genau dieser Kanal zu
genau dieser Zielgruppe passt).

Vor der Formulierung einer neuen Hypothese: `read_knowledge_base(topic=...)`
prüfen (Kapitel 5.7) - vielleicht existiert schon eine distillierte
Erkenntnis zu diesem Thema/Kanal/Taktik aus einem früheren Zyklus. Danach,
vor einem echten Live-Experiment: Research-Evidence-Tier prüfen
(`read_research_findings`/`log_research_finding`) - Wettbewerbsprodukte,
Forendiskussionen, Antworten auf einen echten `own_question_post`. Günstiger
und schneller als ein Live-Test, aber schwächere Evidenz: kann
`test_further`/`pivot`-Reasoning stützen, nie allein zu `build` führen.

### 5.2 Score-Formel (`scoring.compute_score`)

```
rate  = conversions / estimated_reach
score = clamp(round((2*(rate-failure_rate)/(success_rate-failure_rate) - 1) / 0.1) * 0.1, -1, 1)
```

Kontinuierlich zwischen -1 und 1 in 0,1-Schritten. `failure_rate` und
`success_rate` sind die pro Hypothese festgelegten Konversionsraten-Anker
(bei welcher Rate ist es eindeutig gescheitert bzw. eindeutig erfolgreich).

`estimated_reach` kommt aus `measured.reach_estimate`, das Growth über
`read_channel_metrics` real gemessen hat - nie geraten (siehe Kapitel 6).
`conversions` zählt `evaluate_hypothesis` selbst aus `signups.jsonl`,
gematcht über `landing_page_variant_id` und einen `submitted_at`-Zeitstempel
innerhalb von `[created_at, created_at + duration_days]`.

### 5.3 Vier-Wege-Outcome (`scoring.classify_outcome`)

Deterministisch, kein Modell-Ermessen - sonst könnten `test_further` und
`pivot` endlos weiterlaufen:

| Outcome | Bedingung |
|---|---|
| `build` | `score >= 0.7` **UND** `conversions >= break_even_users` dieser Hypothese |
| `test_further` | Noch keine Extension genutzt (`extension_used`) und `score >= -0.3` - läuft **genau einmal** pro Hypothese |
| `bury` | `score < -0.7`, oder Pivot-Budget der Linie ausgeschöpft |
| `pivot` | Alles andere, solange `pivot_attempts_so_far < PIVOT_ATTEMPT_CAP` (= 2) |

Eine starke Rate auf zu wenigen echten Conversions ist `test_further`, nicht
`build` - selbst wenn `break_even_users` klein ist. Umgekehrt ist eine
winzige echte Stichprobe eine völlig legitime `build`-Basis, wenn
`break_even_users` selbst niedrig ist (z.B. 2).

Nach jeder Bewertung, unabhängig vom Outcome, prüft `check_escalation` den
rollierenden Durchschnitt der letzten 3 Scores in der Hypothesen-Linie
(Kette über `prior_hypothesis_id`). `escalate=true` bei ≥3 Scores und
Durchschnitt ≤ -0,5 → strukturierter `file_pivot_proposal` an den Main-CEO
statt eigenständigem Weiterpivotieren.

### 5.4 Was pro Outcome passiert

- **`build`**: `write_hypothesis` persistiert `status='evaluated'`,
  `outcome='build'`. **Immer** zusätzlich `request_approval(category='deploy',
  ...)` - der einzige Outcome, der immer einen menschlichen Blick braucht,
  bevor irgendjemand baut. Danach `file_task_order(to_role='dev', ...)` mit
  der Freigabe-ID im Klartext, damit Dev sie selbst über
  `check_approval_status` verifizieren kann.
- **`test_further`**: Neue `duration_days`, `extension_used=true`, Status
  bleibt `active` - typischerweise mit größerer Stichprobe.
- **`pivot`**: `status='evaluated'`, `outcome='pivot'`. Danach genau eine
  Retest-Hypothese mit `prior_hypothesis_id`/`prior_score` gesetzt und genau
  **einer** geänderten Variablen (`audience`/`price`/`copy`/`channel`/
  `timing`, in `pivot_variable_changed` + `pivot_reasoning` dokumentiert) -
  nie mehrere gleichzeitig, sonst ist das Ergebnis nicht interpretierbar.
- **`bury`**: `status='buried'`, `outcome='bury'`, mit `bury_reasoning`. Nicht
  permanent und keine Löschung - der Record bleibt und kann bei geänderten
  Rahmenbedingungen später wieder aufgegriffen werden.

Bei **build**, **pivot** und **bury** (nicht bei `test_further` - das ist
eine Fortsetzung, noch keine Auflösung) wird zusätzlich ein
`write_knowledge_entry`-Eintrag distilliert (Kapitel 5.7) - ein Pivot lässt
sich genauso lehrreich zusammenfassen ("warum es nicht passte") wie ein
Build ("das hat funktioniert").

### 5.5 Phasentrennung (Validierung vs. Skalierung)

Es gibt keine separate "Phasen"-Maschinerie - die vorhandenen Mechanismen
implementieren das bereits: `test_further`/`pivot` **sind** die
Validierungsphase (Content bleibt organisch, kein Produktlink, bevor
`landing_page_live=true`), `build` löst die genehmigungspflichtige
Skalierungsphase aus (Dev baut erst nach Freigabe).

### 5.6 Fälligkeit und Zeitbox (`read_due_hypotheses`)

Jede Hypothese braucht eine vor dem Start festgelegte Laufzeitgrenze -
ohne sie könnte ein Experiment unbegrenzt weiterlaufen, ohne je durch die
Vier-Wege-Bewertung gezwungen zu werden. `duration_days` ist dafür immer
Pflicht (Kapitel 5.1). Zusätzlich kann optional `sample_size_trigger`
gesetzt werden - ein `measured.reach_estimate`-Wert, bei dessen Erreichen
die Hypothese schon **vor** Ablauf von `duration_days` fällig wird (nützlich
für einen schnellen Kanal, der bereits früh ein echtes Signal liefert und
nicht das volle Fenster abwarten muss).

`read_due_hypotheses()` berechnet deterministisch, welche aktiven
Hypothesen gerade fällig sind (Zeitbox abgelaufen ODER Sample-Trigger
erreicht, je nachdem was zuerst eintritt) - `task_ceo` nutzt dieses Tool
in Schritt 1 statt selbst Datumsarithmetik zu betreiben. Eine Hypothese, bei
der keiner der beiden Mechanismen je greift, taucht dort einfach nie auf -
der Sinn ist, jede Hypothese verlässlich durch ihre eigene Zeitbox zu
zwingen, nicht die Bewertung dem Gefühl zu überlassen, wann sie "fertig
wirkt".

### 5.7 Distillierte Wissensbasis (`knowledge_base.jsonl`)

`hypotheses.jsonl` ist ein Protokoll einzelner Versuche, kein
akkumuliertes Wissen - ohne eine distilliertere Ebene darüber könnte das
System dasselbe später in anderer Verpackung erneut testen, ohne es zu
merken. `knowledge_base.jsonl` hält kurze, konsultierbare Takeaways pro
Thema/Kanal/Taktik:

- `write_knowledge_entry(topic, takeaway, confidence, source_hypothesis_ids,
  channel="", tactic="")` - `confidence` ∈ `{low, moderate, high}`,
  `source_hypothesis_ids` eine nicht-leere Liste der Hypothesen-IDs, aus
  denen der Takeaway distilliert wurde. Wird bei jedem `build`/`pivot`/
  `bury`-Outcome geschrieben (Kapitel 5.4), nicht nur bei Erfolgen.
- `read_knowledge_base(topic="", channel="")` - `topic` matcht als
  case-insensitive Teilstring, `channel` exakt. Der Sub-CEO (und, lesend,
  Growth) konsultiert dies **vor** dem Formulieren einer neuen Hypothese
  (Kapitel 5.1) - derselbe günstige-Erst-Schritt-Gedanke wie beim externen
  Research-Evidence-Tier, nur aus den eigenen vorherigen Hypothesen dieser
  Subsidiary distilliert statt aus externen Quellen.

Beispiel-Eintrag: *"Reddit organic on r/algotrading: tested 4x, weak below
~50 karma accounts, moderate confidence"* - kurz genug, um vor dem
Schreiben der nächsten Hypothese tatsächlich gelesen zu werden, kein
Report.

### 5.8 Bullseye-Ranking für konkurrierende Hypothesen

Dieselbe Brainstorm-Score-Test-Verdoppeln-Logik, die schon für Kanäle gilt
(Kapitel 6.1), gilt eine Ebene höher auch für *was* getestet wird, wenn
mehrere Hypothesen-Ideen gleichzeitig zur Wahl stehen - konkurrierende
Value-Prop-Varianten, Zielgruppen-Segmente, Preishypothesen, nicht nur
Kanäle für eine einzelne Hypothese.

`MAX_ACTIVE_HYPOTHESES = 3` (`write_hypothesis` erzwingt das): eine neue
Hypothese kann nicht `status='active'` werden, solange bereits drei andere
aktiv sind - unabhängig vom bestehenden Parallelitäts-Limit pro
`landing_page_variant_id` (max. 2). Gibt es mehr vielversprechende
Hypothesen-Ideen als freie Kapazität, sollen sie nach `impact_score`/
`confidence_score` gerankt werden - unter Einbeziehung der bereits pro
Hypothese erfassten Break-even-, Defensibility-/Pricing- und
Channel-Fit-Reasoning (Kapitel 5.1), nicht als separater, unverbundener
Scoring-Durchgang. Dieselbe Kapp-Logik wie bei Kanälen: sich über zu viele
Hypothesen gleichzeitig zu verzetteln gilt als der häufigere Fehler als die
falsche zuerst zu wählen.

### 5.9 Evidence-Stage-Leiter (`evidence_stage`)

Optionales Feld auf `write_hypothesis`, geordnet von billigstem/schwächstem
zu teuerstem/stärkstem Signal: `EVIDENCE_STAGES = ["research",
"community_engagement", "landing_page", "build"]`. Nicht auf jeder
Hypothese verpflichtend - die Bootstrap-Hypothese und jeder andere
legitime Direkt-zu-Landing-Page-Test bleiben gültig auch ohne dieses Feld -
aber `file_task_order(to_role="dev", ...)` prüft es, sobald eine
`hypothesis_id` mitgegeben wird:

- Abgelehnt, wenn `evidence_stage` der Hypothese (noch) nicht
  `landing_page`/`build` ist **und** kein `stage_justification` mitgegeben
  wurde.
- Akzeptiert, sobald entweder `evidence_stage` bereits `landing_page`/
  `build` zeigt (der Normalfall: eine Landing Page *ist* dieser Test, also
  setzt der Sub-CEO `evidence_stage='landing_page'` direkt beim
  `write_hypothesis`-Aufruf, der die Hypothese erzeugt), oder ein
  nicht-leeres `stage_justification` erklärt, warum echte Dev-Arbeit jetzt
  trotzdem richtig ist.

Das ist ein auditierbares Gate, kein hartes Verbot - die billigste Version
ist immer nur eine begründete Entscheidung, keine stillschweigende. Die
eigentliche Frage ("was ist der billigste Test, der diese Unsicherheit
tatsächlich auflöst") bleibt Ermessenssache des Sub-CEO (`ceo_agent`s
Backstory, `crew.py`) - das Gate erzwingt nur die Nachvollziehbarkeit, nicht
das Urteil selbst. Die meisten Hypothesen gehen weiterhin legitim direkt zu
`landing_page` - das ist der bewährte Standardpfad dieses Systems, keine
Regression davon.

### 5.10 Zahlungsbereitschafts-Test (Payment-Intent)

Der Landing-Page-Test misst standardmäßig nur Interesse (E-Mail-Signup).
Für eine Hypothese, bei der echte Zahlungsbereitschaft das Signal
wesentlich stärken würde, ist ein Pre-Order-/Deposit-Test verfügbar - eine
Option, kein Standard:

1. Der Sub-CEO ruft `request_approval(category='spend', proposal=...,
   reasoning=...)` auf und beschreibt Preis und gewünschte Art des
   Pre-Orders/Deposits. Das System provisioniert **niemals selbst** einen
   Zahlungsanbieter/-link - das bleibt exakt derselbe menschliche Tier-2-
   Schritt wie DNS/Verträge/neue Logins.
2. Ein Mensch legt den echten Link an und bestätigt per Telegram:
   `payment_link: <appr_id> <url>` (gleiches Muster wie `live:`/`posted:`/
   `removed:`, Kapitel 8.2) - nur wirksam, wenn die Anfrage bereits
   `status='approved'` ist.
3. `check_approval_status(approval_id)` liefert `payment_link_url` (`null`,
   bis ein Mensch ihn tatsächlich hinterlegt hat, selbst wenn `status`
   schon `'approved'` ist) - der Sub-CEO fragt das ab, statt einen Link
   anzunehmen, und trägt den bestätigten Link wörtlich in den
   `file_task_order` an Dev ein, nie eine Paraphrase.

---

## 6. Channel-Auswahl und Content-Erstellung

### 6.1 Bullseye-Framework (Traction, Weinberg & Mares)

`task_channel_strategy` (Sub-CEO, immer erster Task im Zyklus) pflegt den
Kanal-Roster in `channels.jsonl` nach dem Bullseye-Prinzip: viele Kandidaten
brainstormen, bewerten, wenige gleichzeitig testen (`MAX_CHANNELS_TESTING =
3`), auf den Gewinner setzen, nicht funktionierende austauschen statt an
ihnen festzuhalten (`MAX_TOTAL_CHANNELS = 20` insgesamt).

Konkrete Kandidaten für API Sentinel (im Task-Text, nicht abschließend):
r/algotrading, r/quantfinance, r/quant, QuantConnect Community-Forum und
-Discord, Elite Trader, Trade2Win, quant.stackexchange.com (letzteres mit
echtem `view_count`-Feld pro Frage).

Ein bezahlter Kanal (`is_paid=true`) braucht **beides**: die Policy
`paid_channels_allowed=true` (Kapitel 4.2) **und** eine freigegebene
`spend`-Anfrage - `write_channel` erzwingt beide Gates in dieser
Reihenfolge.

**Namens-Duplikat-Schutz:** Ein neuer Kanal, dessen Name (Groß-/
Kleinschreibung und Leerzeichen egal) bereits in der Roster existiert, wird
abgelehnt - auch unter einer anderen id. Reaktion auf einen diagnostizierten
101k-Token-Zyklus, in dem derselbe Kanal mehrfach unter verschiedenen
Bedingungen erneut geschrieben wurde, statt via `read_channels()` zu
erkennen, dass er schon da war (Kapitel 15) - `write_channel` verweist bei
einem Namenstreffer explizit auf die existierende id, statt eine
Beinahe-Dopplung anzulegen.

### 6.2 Reichweitenmessung (`read_channel_metrics`)

Growth misst reale Reichweite, nie geraten:

| Kanal | Auto-Fetch (keylos) | Fallback |
|---|---|---|
| Reddit | Ja - `<post_url>.json` mit deskriptivem User-Agent | `metrics_json` mit `upvotes`/`comments` |
| Discord | Ja - Discord Invite-API (`with_counts=true`) | `metrics_json` mit `members` |
| Telegram | Ja - Scraping der öffentlichen `t.me/s/<channel>`-Vorschauseite | `metrics_json` mit `members` |
| X (Twitter) | Nein - seit 2023 keine kostenlose öffentliche Metrik-API mehr | `metrics_json` (zahlt der Mensch, wäre eine `spend`-Freigabe) |
| `landing_page_direct` | Nein | Echte Analytics (Plausible/GA) über `metrics_json` |

Ein echter, plattformnativer Wert wird immer der Fallback-Schätzformel
(`reach_estimators.json`, kalibrierbar über `update_reach_multiplier`)
vorgezogen. Ohne verwertbare Daten: Fehler statt Schätzung.

### 6.3 Content-Erstellung (`draft_content`, nur Growth)

Content-Erstellung ist **Pflicht**, nicht optional - in der frühen Phase
zählt Lernen über Foren/Communities mehr als eine ausgefeilte Landing Page.
Die gesamte Erstellung läuft über den Agenten, nie manuell außerhalb des
Systems; das Aufsichtsrat gibt frei, gepostet wird von Hand.

Drei Post-Typen:

| `post_type` | Zweck | Längen-Cap | Produktlink |
|---|---|---|---|
| `thread_reply` | Echte Antwort in fremdem Thread | 600 Zeichen | erlaubt, wenn `landing_page_live` |
| `own_question_post` | Echte, neugierige Frage - selbst eine Validierungsmethode (speist Research-Evidence-Tier) | 500 Zeichen | i.d.R. nicht sinnvoll |
| `own_hypothesis_post` | Eigener Thread über das Problem/die Idee | 1500 Zeichen | erlaubt, wenn `landing_page_live` |

Mechanisch erzwungene Prüfungen in `draft_content` (Stil/Qualität darüber
hinaus bleibt Ermessen des Agenten):

- `rules_checked=true` **und** nicht-leeres `rules_notes` bei **jedem
  einzelnen** Entwurf - Community-Regeln ändern sich, ein Check von vor drei
  Zyklen zählt nicht.
- Keine KI-typischen Muster: Markdown-Header, Bullet-Listen, "in
  conclusion"/"in summary"/"as an AI"/"I hope this helps".
- Längen-Cap je nach `post_type` (Tabelle oben).
- `include_product_link=true` erfordert `landing_page_live=true` auf der
  referenzierten Hypothese - sonst Ablehnung.

Plattformen: `reddit`, `discord`, `quant_stackexchange`, `forum_other`.
**Cold Email ist nicht in der Liste** - ausdrücklich aus rechtlichen
Gründen (EU-ePrivacy/DSGVO, deutsches UWG) ausgeschlossen, nicht nur eine
Stilfrage.

Der gesamte Lebenszyklus eines Entwurfs (`content_drafts.jsonl`):

```
drafted --(Mensch postet, Telegram "posted: <id> <url>")--> posted --(optional, Telegram "removed: <id> <grund>")--> removed
```

- `check_community_risk(platform, target_community)`: zählt Removals der
  letzten 30 Tage für diese Community; ≥2 → `risk="high"` (Cooldown-Signal).
- `get_account_stats(platform)`: Verhältnis `is_promotional=true` zu
  gesamt, Zielwert 10 % promotional (90/10-Regel).
- `landing_page_live` wird **nie** vom System selbst gesetzt - nur über
  einen menschlichen Telegram-Befehl `live: <hypothesis_id>`, weil ein
  gemergter PR eine rein menschliche Tatsache ist, die das System sonst
  nicht beobachten kann.

---

## 7. Governance-Ebene: Main-CEO / Holding

`holding.py` ist die Ebene *über* `tools.py`: während `tools.py` der
operative Zustand einer einzelnen Subsidiary ist (Hypothesen, Kanäle,
Freigaben), verwaltet `holding.py` das Subsidiary-Register, Pivot-Reviews,
Cross-Subsidiary-Routing und das holdingweite Research-Archiv - in einem
eigenen Unterordner `STATE_DIR/_holding/`, damit es sich nie mit dem Zustand
einer Subsidiary vermischt.

Mit heute nur einer registrierten Subsidiary (`api-sentinel`, automatisch
beim ersten `read_subsidiaries()`-Aufruf gebootstrapped) ist vieles davon
Scaffolding, das erst mit einer zweiten Subsidiary tragend wird.

| Mechanismus | Dateien | Zweck |
|---|---|---|
| Subsidiary-Register | `subsidiaries.jsonl` | id/name/focus/status (active/dormant)/policies/status_history |
| Pivot-Review | `pivot_proposals.jsonl` | Sub-CEO reicht ein (8 Pflichtfelder: `nature_of_change`, `validating_data`, `evolutionary_or_disruptive`, `existing_business_disposition`, `capability_gap_analysis`, `new_resources_needed`, `risk_assessment`, `synergy_overlap`), Main-CEO entscheidet (`approve_in_place`/`move_to_subsidiary`/`spinoff_required`/`rejected`) |
| Cross-Subsidiary-Requests | `cross_subsidiary_requests.jsonl` | Sub-CEOs kontaktieren sich nie direkt - immer über den Main-CEO |
| Research-Archiv | (liest `hypotheses.jsonl`/`channels.jsonl`/`pivot_proposals.jsonl` aller Subsidiaries) | "Pull principle" - nicht automatisch im Kontext, nur auf Anfrage per `search_research_archive` |
| Status-Reports (Sub-CEO → Main-CEO) | `status_reports.jsonl` | Fester Record statt Freitext-Report; `needs_decision_from_above` markiert, was wirklich Aufmerksamkeit braucht |
| Strategische Ausrichtung (Main-CEO → Sub-CEO) | `strategic_directions.jsonl` | Rahmen, kein Befehl - übersteuert nie taktisches Ermessen des Sub-CEO |
| Ideen-Intake | `ideas.jsonl` | Jeder Agent schlägt vor (`propose_idea`), Main-CEO routet (`route_idea`) - Kapitel 7.2 |
| Subsidiary-Policies | auf `subsidiaries.jsonl`-Record | Siehe Kapitel 4.2 |

Neue Subsidiary registrieren (`register_subsidiary`) erstellt **nur** den
Metadaten-Eintrag - keine echte Infrastruktur (neuer Railway-Service, eigene
Crew/Agenten). Das bleibt separate, menschlich gesteuerte Ingenieursarbeit,
nachdem eine `request_approval` freigegeben wurde. Seit dem Audit-Addendum
(Kapitel 15) steht das jetzt auch **auf dem Record selbst**
(`operative_capability`), nicht nur in der Dokumentation hier: `tools.py`
hat aktuell genau ein `STATE_DIR` (kein `subsidiary_id`-Feld auf
`hypotheses.jsonl`/`channels.jsonl`) und `crew.py` verdrahtet genau eine
`Crew` fest auf `api-sentinel` - eine zweite registrierte Subsidiary hat
also weder einen eigenen Zustand noch eine eigene Crew, bis das jemand
separat baut.

### 7.1 Fortschritt statt endloser Experimente - Umsatz als Filter, nicht als Ziel

Zwei Mechanismen sollen verhindern, dass eine Subsidiary unbegrenzt
Hypothesen testet, ohne dass ihr eigentlicher Zweck (ein echtes Problem für
echte Nutzer lösen) je explizit gemacht oder ihr tatsächlicher Fortschritt
dorthin je unabhängig betrachtet wird - beide laufen in
`task_main_ceo_review`, jeden Zyklus, unabhängig davon, ob der Sub-CEO
etwas eskaliert hat. Wichtig dabei: Umsatz ist bewusst **kein**
Optimierungsziel, sondern ein verpflichtender Filter, den jede Hypothese
ohnehin schon erfüllen muss (Break-even-Ökonomie, Defensibility, Pricing -
mechanisch enforced, siehe Kapitel 5.1) - direkt auf Umsatz zu optimieren
würde Hypothesen belohnen, die sich leicht monetarisieren lassen (aggressive
Preise, aufdringliche Texte), statt solcher, die ein echtes Problem lösen -
genau das Fehlermuster, das das restliche Systemdesign (organisch-only,
menschlich klingender Content, 90/10-Regel, `hypothesis_type=value`)
vermeiden soll:

- **Verpflichtende Erstausrichtung:** Für jede aktive Subsidiary prüft der
  Main-CEO `read_strategic_direction(subsidiary_id=...)`. Kommt
  `direction=null` zurück - diese Subsidiary hatte noch **nie** eine
  strategische Ausrichtung -, setzt der Main-CEO proaktiv eine. Voran steht
  das Lösen eines echten Problems für die Zielgruppe; Monetarisierung wird
  als verpflichtender, nicht verhandelbarer Filter benannt (die bestehende
  Break-even-/Defensibility-/Pricing-Ökonomie), den jede Hypothese erfüllen
  muss - nicht als das eigentliche Ziel. Das ist eine einmalige Baseline
  pro Subsidiary, keine taktische Vorgabe für Kanal-/Hypothesen-
  Entscheidungen - die bleiben beim Sub-CEO. Jede spätere, zusätzliche
  Ausrichtung bleibt die bestehende Ausnahme-Regel (Kapitel 3.4): nur bei
  einem echten Grund, nicht jeden Zyklus.
- **Trajektorie-Check jeden Zyklus (Gesundheits-Check, kein
  Umsatz-Tracker):** `assess_subsidiary_trajectory` (Kapitel 5.8
  ergänzend, aber holdingweit statt pro Hypothesen-Linie) zählt
  deterministisch alle je aufgelösten Outcomes (`build`/`pivot`/`bury`)
  einer Subsidiary. `possible_stall=true`, wenn mindestens
  `STALL_RESOLVED_THRESHOLD` (5) Hypothesen aufgelöst wurden und keine
  davon `build` war - das ist die mechanisch erfassbare Teilmenge; ebenso
  ein Stillstands-Signal, nur nicht mechanisch erfasst, sind wiederholte
  ergebnislose Pivot-/Test-further-Zyklen, die dieselbe Frage immer wieder
  neu stellen, ohne je zu einer echten Auflösung zu kommen - der Main-CEO
  liest dafür selbst die Hypothesen-Historie mit. Das ist bewusst **kein**
  zweiter Eskalationsmechanismus neben `check_escalation` (das bleibt pro
  Hypothesen-Linie das Einzige, was tatsächlich einen formalen
  Pivot-Vorschlag auslöst, von der Sub-CEO-Seite aus) - dieses Tool
  persistiert nichts und feuert nichts selbst aus; es liefert nur die
  Zahlen, und der Main-CEO benennt einen möglichen Stillstand explizit im
  eigenen Zyklus-Report, falls das Muster das nahelegt - ohne eine auf dem
  Papier umsatzpositive Hypothese automatisch als echten Fortschritt zu
  werten, wenn das zugrunde liegende Problem nie wirklich validiert wurde.

### 7.2 Idee-Intake und Subsidiary-Routing (`ideas.jsonl`)

Eine Idee (eine Marktlücke / Value-Creation-Opportunity) kann von überall
im System kommen - Main-CEO, ein Sub-CEO, oder Growth aus echtem
Community-Engagement heraus. Zwei-Schritt-Flow, bewusst nicht
auto-verkettet (dieselbe Trennung wie bei `file_pivot_proposal`/
`decide_pivot_proposal`):

- `propose_idea(summary, source, reasoning)` - jeder Agent kann das
  aufrufen (Sub-CEO/Main-CEO haben es als Tool, Growth ebenfalls für
  Marktlücken außerhalb der eigenen Hypothese). Landet mit `status='pending'`
  in `ideas.jsonl`.
- `read_ideas(status="")` - vom Main-CEO in `task_main_ceo_review` Schritt 0
  jeden Zyklus gelesen (`status="pending"`).
- `route_idea(idea_id, decision, reasoning, target_subsidiary_id="")` -
  `decision` ∈ `existing_subsidiary` (braucht `target_subsidiary_id`; der
  Main-CEO setzt danach selbst `set_strategic_direction` auf der
  Ziel-Subsidiary - `route_idea` selbst tut das nicht), `new_subsidiary`
  (braucht weiterhin eine eigene `request_approval` vor
  `register_subsidiary`, exakt wie jeder andere Spin-off - `route_idea`
  erstellt nichts), oder `rejected`. Nicht erneut routbar, sobald entschieden.

Wichtig, siehe auch die Notiz in Kapitel 7 oben: `decision='new_subsidiary'`
gefolgt von einem tatsächlichen `register_subsidiary` erzeugt bis heute nur
einen Registry-Eintrag mit `operative_capability`-Hinweis, keine lauffähige
zweite Subsidiary. Der Main-CEO-Task-Text sagt das explizit, damit das nie
stillschweigend als "die neue Subsidiary läuft jetzt" missverstanden wird.

---

## 8. Mensch im Loop: Freigabe-Queue und Telegram-Fernsteuerung

### 8.1 Freigabe-Queue (`approval_queue.jsonl`)

Jede Aktion mit Kosten, rechtlicher Verpflichtung oder Öffentlichkeitswirkung
läuft über `request_approval(category, proposal, reasoning)`.
`category` ∈ `{spend, legal, publish, deploy, pricing}`. Der Eintrag landet
mit `status='pending'` in der Queue und wird **nie** vom System selbst
ausgeführt.

Ein Mensch entscheidet über:

```bash
python approve.py                       # offene Anfragen auflisten
python approve.py approve appr_ab12cd34
python approve.py reject appr_ab12cd34 [grund]
```

Andere Tools (`check_approval_status`, `write_channel`, `register_subsidiary`,
`update_subsidiary_policies`, …) prüfen den echten Status selbst nach, statt
der Behauptung eines anderen Agenten zu vertrauen.

### 8.2 Telegram - Benachrichtigung und Fernsteuerung

`send_telegram_message` postet die Zyklus-Zusammenfassung; benötigt
`TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID`, degradiert bei fehlenden/
ungültigen Credentials sauber (Warnung im Log, kein Crash).
`notify_new_pending_approvals` schickt für jede neue offene Freigabe eine
eigene Nachricht (Idempotenz über ein `telegram_notified`-Flag auf dem
Record).

`process_telegram_commands` liest neue Nachrichten seit dem letzten
verarbeiteten Update (`telegram_update_offset.txt`) und wertet sie aus
(`_classify_command`/`_apply_telegram_commands`). Unterstützte Befehle:

| Befehl | Wirkung |
|---|---|
| `stop` / `pause` | System pausieren (`system_paused.json`) - überspringt jeden Zyklus, bis `start` kommt |
| `start` / `resume` / `weiter` | Pause aufheben |
| Antwort `approve`/`ja`/`yes` auf eine Freigabe-Benachrichtigung, oder `<id> approve` getippt | Freigabe genehmigen |
| Antwort `reject`/`nein`/`no`, oder `<id> reject` getippt | Freigabe ablehnen |
| `live: <hypothesis_id>` | `landing_page_live=true` setzen (nur so settbar - ein gemergter PR ist eine rein menschliche Tatsache) |
| `posted: <draft_id> <url>` | Content-Entwurf als tatsächlich gepostet markieren |
| `removed: <draft_id> <grund>` | Geposteten Entwurf als entfernt markieren (speist `check_community_risk`) |
| `payment_link: <appr_id> <url>` | Echten Zahlungslink für eine `status='approved'`-Payment-Intent-Anfrage hinterlegen (Kapitel 5.10) |

Alle anderen Nachrichten werden stillschweigend ignoriert (der Operator darf
einfach chatten, das ist kein Fehler). Ein Telegram-/Netzwerkfehler darf nie
einen Cron-Zyklus blockieren oder crashen - alle Telegram-Funktionen fangen
ihre eigenen Fehler ab.

---

## 9. Modelle, Tokens und Limits

### 9.1 Agent-Profile (`agent_profile.json`)

Modell- und Limit-Konfiguration liegt **nicht** hartcodiert in `crew.py`,
sondern in `agent_profile.json` mit zwei benannten Profilen. Umschalten =
`active_profile` ändern + Redeploy, kein Code-Change:

```json
{
  "active_profile": "testing",
  "profiles": {
    "testing": { "model": "claude-haiku-4-5", "cycle_token_budget": 50000, ... },
    "normal":  { "model": "claude-sonnet-5",  "cycle_token_budget": 1000000, ... }
  }
}
```

| Profil | Modell | Zweck |
|---|---|---|
| `testing` (aktuell aktiv) | `claude-haiku-4-5` | Reiner "läuft der Zyklus End-to-End"-Check, keine Qualitätsprüfung - minimale Kosten während der aktiven Entwicklung |
| `normal` | `claude-sonnet-5` | Produktionseinstellungen für echte Hypothesenqualität |

Pro Agent (`agents.<rolle>` im aktiven Profil):

| Agent (Profil-Key) | `testing`: max_tokens/max_iter/max_execution_time | `normal`: max_tokens/max_iter/max_execution_time |
|---|---|---|
| `growth` | 900 / 9 / 120s | 3000 / 30 / 600s |
| `dev` | 2000 / 6 / 90s | 8000 / 15 / 300s |
| `sub_ceo` (`ceo_agent`) | 800 / 15 / 240s | 8000 / 50 / 900s |
| `main_ceo` | 500 / 6 / 120s | 4000 / 25 / 600s |

`cycle_token_budget`: `testing` = 50.000, `normal` = 1.000.000 Tokens pro
Zyklus insgesamt.

`growth`/`dev` wurden im Audit-Addendum (Kapitel 15) von 500/6 bzw. 500/4
angehoben - in echten Railway-Zyklus-Logs bestätigt: Dev erreichte
`max_iter=4` jeden Zyklus mittendrin in `open_pull_request` ohne
`file_content` (strukturell unmöglich, eine vollständige Landing-Page-HTML
in diesem Budget je fertigzustellen), Growth traf `max_iter=6` in mehreren
Zyklen - ein vollständiger Durchlauf (`read_task_orders`,
`read_hypotheses`, `check_community_risk`, `get_account_stats`,
`draft_content`, `request_approval`, `complete_task_order`) braucht schon
im besten Fall 7 Tool-Aufrufe. Beide Werte sind evidenzbasiert angehoben,
nicht pauschal gelockert - `sub_ceo`/`main_ceo` blieben unverändert, da
für sie kein vergleichbarer Engpass beobachtet wurde.

Aktives Profil und Modell werden beim Start geloggt und in jeder
Telegram-Zusammenfassung mit ausgewiesen.

### 9.2 `thinking` bleibt für alle Agenten in beiden Profilen ungesetzt

Kein `thinking={"type": ...}` irgendwo im Code. Grund: Claude Sonnet 5 läuft
ohne diese Angabe automatisch adaptiv; Claude Haiku 4.5 (kein
Adaptive-Thinking-Support) läuft ohne Angabe ohne Thinking - beide sind also
mit derselben Nicht-Konfiguration korrekt bedient. `thinking={"type":
"disabled"}` wurde bewusst entfernt (siehe Git-Historie): crewai serialisiert
diese Config immer mit `budget_tokens: None` mit, was Anthropic unter
`type="disabled"` als 400-Fehler ablehnt (`crewai_patches.py`-nahes, aber
separates Problem - hier per Konfiguration umgangen, nicht gepatcht).

### 9.3 Sicherheits-Limits pro Agent (verifiziert gegen die installierte crewai-Version)

| Limit | Wirkung |
|---|---|
| `max_iter` | Harte Obergrenze für Tool-Aufrufe/Denkschritte einer einzelnen Task-**Ausführung** |
| `max_execution_time` (Sekunden) | Harte Wall-Clock-Grenze pro Task-Ausführung - fängt wenige, aber sehr teure Schritte ab, die `max_iter` allein nicht erwischen würde |
| `max_rpm=20` | Fest für alle Agenten - zusätzliches Netz gegen einen Agenten, der kurzfristig ungewöhnlich viele Requests abfeuert |
| `max_retry_limit=1` | Fest für alle Agenten (crewai-Default wäre 2 = 3 Versuche, jeder mit vollem `max_iter`/`max_execution_time` neu). Bei einem deterministisch fehlschlagenden Fehler bedeutet das: höchstens das Doppelte statt das Dreifache des Budgets, bevor endgültig aufgegeben wird |

Was diese vier Limits **nicht** abdecken: eine Grenze über den **gesamten**
Zyklus (alle 5 Tasks zusammen) - jedes wirkt nur pro einzelner
Task-Ausführung. `ceo_agent` läuft zweimal pro Zyklus, ohne dass sein Budget
dafür irgendwo automatisch aufaddiert sichtbar wäre.

### 9.4 Zyklus-Budget (`CYCLE_TOKEN_BUDGET`)

Die einzige Grenze, die tatsächlich über den **ganzen** Zyklus wirkt.
Implementiert über crewais `ConditionalTask` (siehe Kapitel 10.3): Jeder
Task außer dem allerersten prüft vor seiner Ausführung
`crew.calculate_usage_metrics().total_tokens` gegen `CYCLE_TOKEN_BUDGET` aus
dem aktiven Profil; ist das Budget erreicht, wird der Task übersprungen statt
ausgeführt, und der Grund landet in `_limit_hits` (erscheint als Warnung in
der Telegram-Zusammenfassung).

### 9.5 `max_iter`-Watchdog

CrewAI wirft beim Erreichen von `max_iter` selbst keine Exception und feuert
auch kein Event - es hängt intern still eine "gib jetzt deine beste finale
Antwort"-Anweisung an und macht weiter. Ohne eigenes Monitoring würde ein
wiederholt an sein Limit stoßender Agent also nie auffallen.
`_make_iteration_watchdog` (Task-`callback`, feuert einmal pro
Task-Abschluss) prüft `agent.agent_executor.iterations >= agent.max_iter`
und trägt einen Treffer in `_limit_hits` ein.

### 9.6 Token-Reporting, Kosten und Formatierung

`_compute_cycle_usage()` liest `crew.usage_metrics` nach `kickoff()` einmal
pro Zyklus, berechnet die Kosten (siehe unten) und hängt beides per
`log_cycle_usage` an `usage_history.jsonl` an, damit Kosten-/Token-Trends
über die Zeit sichtbar werden, nicht nur einmalig gemeldet und vergessen.

**Kostenberechnung (`pricing.py`):** reine, testbare Funktionen im Stil von
`scoring.py`, ohne CrewAI-/STATE_DIR-Abhängigkeit. `get_pricing(model,
as_of)` schlägt die USD-pro-Million-Tokens-Sätze für `claude-haiku-4-5`
bzw. `claude-sonnet-5` nach - datumsbewusst, da Sonnet 5 am 2026-09-01 einen
bestätigten Preissprung hat; `as_of` ist immer das Datum des jeweiligen
Zyklus, nicht das Datum, an dem der Code geschrieben wurde, damit der
Report auf beiden Seiten des Stichtags automatisch korrekt bleibt.
`compute_cycle_cost(...)` multipliziert die vier bereits getrennt
vorliegenden Token-Kategorien (Base-Input, Cache-Write, Cache-Hit/Read,
Completion - laut Anthropics API nie überlappend) mit den passenden Sätzen
und summiert. Der Cache-Write-Tarif ist bewusst der 5-Minuten-Satz, nicht
der 1-Stunden-Satz - verifiziert direkt im installierten crewai-Paket
(`crewai.llms.providers.anthropic.completion._stamp_cache_control_on_
message` stampt `cache_control` ohne explizites `ttl`, was Anthropics
Default von 5 Minuten ist), nicht angenommen.

**Report-Aufbau:** in zwei Telegram-Nachrichten gesplittet.
`_usage_headline()` steht als eigene, unmissverständliche Zeile
(`Gesamt-Tokens diesen Zyklus: X (Y% Zyklus-Budget) - Kosten: $Z`) direkt am
Anfang der ersten (Haupt-)Nachricht, noch vor Warnungen und
Telegram-Kommando-Logs. `_usage_detail_line()` folgt weiter unten mit dem
Rest in Prosa: Agent-Profil/Modell, Prompt-/Completion-Aufteilung,
Cache-Erklärung, `max_tokens` pro Agent. Direkt im Anschluss an die
Hauptnachricht verschickt `_format_usage_table()` eine **zweite, separate**
Nachricht mit denselben Zahlen als fest formatierte Monospace-Tabelle
(Markdown-Codeblock, `parse_mode="Markdown"`) - zwei Tabellen: Token-/
Kosten-Übersicht und `max_tokens` pro Agent. Getrennt von der Hauptnachricht,
damit ein Formatierungsfehler dort nie den eigentlichen Report gefährdet
(`send_telegram_message` faengt das selbst ab und würde bei einem
abgelehnten `parse_mode`-Send automatisch als Klartext erneut versuchen) -
die Zahlen stehen ohnehin schon in Prosa in der Hauptnachricht, die Tabelle
ist rein kosmetisch on top.

Telegram Bot API 10.1 (11.06.2026) hat mit `sendRichMessage`/
`RichBlockTable` echte native Tabellen eingeführt - live gegen Telegrams
eigene Dokumentation bestätigt real (nicht die hier verwendete Methode:
drei separate Abruf-Versuche gegen `core.telegram.org/bots/api` und dessen
Changelog lieferten zwar die Existenz, aber nie die vollständige
Feld-für-Feld-Parametertabelle). Ein JSON-Schema gegen eine echte externe
API zu raten, nur weil die Methode nachweislich existiert, widerspricht der
Verifikationsdisziplin dieses Repos - deshalb der dokumentierte, bewährte
`sendMessage` + `parse_mode="Markdown"`-Codeblock-Weg statt eines
ungetesteten `sendRichMessage`-Aufrufs. Aufzugreifen, sobald das exakte
Schema verifizierbar ist.

**Instrumentierung (ohne Limit-Änderung):** `_task_usage_log` erfasst pro
Task den tatsächlichen Token-Verbrauch (Differenz von
`crew.calculate_usage_metrics().total_tokens` vor/nach der jeweiligen Task,
im selben Callback wie der `max_iter`-Watchdog, siehe 9.5) - ein Ausreisser
in einer einzelnen Task war vorher erst sichtbar, nachdem er bereits das
ganze Zyklus-Budget gesprengt hatte. `_malformed_tool_calls` zählt
`ToolValidateInputErrorEvent`-Vorkommen (crewai-eigenes Event, gefeuert wenn
ein Tool-Aufruf schon an Pydantic-Argumentvalidierung scheitert, bevor die
eigentliche Tool-Funktion je läuft) - ein aus einem diagnostizierten
101k-Token-Zyklus vermuteter, aber unbestätigter Zusammenhang mit dem
strict-tools-Patch (Kapitel 10.6) wird damit zu einer echten Zahl statt
einer Vermutung. Beides landet in `usage_history.jsonl` und, falls
Vorkommen vorliegen, als kurzer Hinweis im Telegram-Report - explizit *ohne*
`cycle_token_budget`/`max_iter`/`max_tokens` selbst anzufassen, bis nach
mehreren Zyklen echte Daten vorliegen (siehe Kapitel 15).

### 9.7 Persistenz-Check (`check_state_persistence`)

Läuft als aller erster Schritt jedes Zyklus (noch vor
`process_telegram_commands`), damit eine Warnung Telegram auch dann noch
erreicht, wenn danach alles andere fehlschlägt. Prüft `RAILWAY_VOLUME_
MOUNT_PATH` - eine echte, von Railway dokumentierte Laufzeit-Env-Var, die
"if any" Volume angehängt ist, gesetzt wird
(https://docs.railway.com/variables/reference) - gegen `STATE_DIR`:

- Außerhalb Railways (kein `RAILWAY_ENVIRONMENT_ID` gesetzt - lokale Läufe,
  Tests): `applicable=False`, keine Warnung - es gibt dort kein
  Volume-Konzept zu prüfen.
- Innerhalb Railways, `RAILWAY_VOLUME_MOUNT_PATH` stimmt mit `STATE_DIR`
  überein: `persistent=True`, keine Warnung.
- Innerhalb Railways, aber kein passender Mount-Pfad: `persistent=False`
  mit konkreter Warnung, gleiche Sichtbarkeitsstufe wie die bestehenden
  Budget-/`max_iter`-Warnungen im Telegram-Report (Kapitel 15 zur
  Entstehungsgeschichte dieses Checks).

---

## 10. CrewAI: Funktion und Implementierung

### 10.1 Warum CrewAI

CrewAI orchestriert mehrere `Agent`-Objekte (Rolle, Goal, Backstory, LLM,
Tool-Liste, Limits) über eine Liste von `Task`-Objekten (Beschreibung,
erwarteter Output, zugewiesener Agent) innerhalb einer `Crew`. Jeder Agent
bekommt seine Tools als `crewai.tools.tool`-dekorierte Python-Funktionen -
das Modell entscheidet selbst, welches Tool wann mit welchen Argumenten
aufgerufen wird (ReAct-artige Tool-Use-Schleife), nicht der Anwendungscode.

### 10.2 Prozessmodell: `Process.sequential`

```python
crew = Crew(
    agents=[growth_agent, ceo_agent, main_ceo_agent, dev_agent],
    tasks=[task_channel_strategy, task_growth, task_ceo, task_main_ceo_review, task_dev],
    process=Process.sequential,
)
```

Die fünf Tasks laufen strikt in Listenreihenfolge, jeweils mit dem Output
des vorherigen Tasks als zusätzlichem Kontext für den nächsten. Es gibt
keine Parallelität und keinen hierarchischen Manager-Agent (`Process.
hierarchical` wird nicht verwendet) - die Reihenfolge selbst kodiert die
fachliche Abhängigkeit (erst Kanalstrategie, dann Reichweitenmessung, dann
Bewertung/neue Hypothesen, dann Governance-Review, zuletzt Umsetzung).

### 10.3 `ConditionalTask` als Budget-Schutz

`crewai.tasks.conditional_task.ConditionalTask` ist ein offizieller,
dokumentierter Mechanismus: eine `ConditionalTask` wird nur ausgeführt, wenn
ihre `condition`-Funktion `True` zurückgibt, sonst übersprungen. Angewendet
auf alle vier Tasks außer `task_channel_strategy` (die darf per
`ConditionalTask`-Constraint nicht selbst conditional sein - braucht aber
ohnehin kein Budget-Gate, da vor ihr im Zyklus noch nichts verbraucht wurde).
`crew.calculate_usage_metrics()` liest live-kumulierte Nutzung direkt von
jedem Agenten-LLM und funktioniert deshalb auch mitten im Lauf zuverlässig
(siehe Kapitel 9.4).

### 10.4 Prompt Caching

CrewAI markiert automatisch Cache-Breakpoints
(`crewai.llms.cache.mark_cache_breakpoint`, unbedingt aufgerufen in
`crew_agent_executor._setup_messages`) für Rolle/Goal/Backstory und
Tool-Definitionen pro Agent, sobald der gemeinsame Präfix (Tools + System)
das modellabhängige Minimum erreicht. Das funktioniert für dieses Setup
bereits ohne jede Code-Änderung - in `_usage_detail_line()`s Report sichtbar
als `cached_prompt_tokens` (günstig, gelesen) vs. `cache_creation_tokens`
(teurer, einmalig pro Cache-Fenster geschrieben).

### 10.5 Model-Routing über die native Anthropic-Anbindung

Alle vier LLM-Instanzen (`growth_llm`, `dev_llm`, `ceo_llm`,
`main_ceo_llm`) werden über `crewai.LLM(model=f"anthropic/{model}", ...)`
gebaut und laufen über crewais **nativen** `AnthropicCompletion`-Provider
(nicht über litellm) - das greift auch für Modelle wie `claude-sonnet-5`,
die (noch) nicht in crewais hartcodierter `ANTHROPIC_MODELS`-Liste stehen,
über den `claude-`-Präfix-Fallback in der Provider-Erkennung.

### 10.6 Patches für bestätigte crewai-Bugs (`crewai_patches.py`)

`apply_patches()` wird in `crew.py` **vor** jeder Agent-/Task-Konstruktion
aufgerufen. Aktuell ein Patch:

**`_patch_max_iterations_final_answer_role`** - wenn ein Agent `max_iter`
erreicht, versucht crewai eine finale Antwort zu erzwingen, indem es die
entsprechende Anweisung als **assistant**-Rolle-Nachricht anhängt und als
letzte Nachricht der Conversation verschickt. Jedes aktuelle Claude-Modell
lehnt das mit einem 400 ab ("This model does not support assistant message
prefill. The conversation must end with a user message."). Das ist keine
Rand-, sondern die Normalsituation, sobald ein `max_iter`-Cap tatsächlich
greift - reproduziert in Produktion. Der Patch ersetzt die betroffene
Funktion (`handle_max_iterations_exceeded`) modulweit in **beiden** Stellen,
die sie über ein eigenes `from ... import` referenzieren
(`crewai.agents.crew_agent_executor` und
`crewai.experimental.agent_executor`) - nur die Ursprungsdefinition in
`crewai.utilities.agent_utils` zu patchen hätte sich **nicht** propagiert.
Verifiziert gegen eine Fake-LLM sowohl vor (Rolle war `"assistant"`) als
auch nach dem Patch (Rolle ist `"user"`). Defensiv implementiert: Ändert
sich crewais interne Struktur, überspringt `apply_patches()` diesen Patch
mit einer Log-Warnung statt beim Import zu crashen.

### 10.7 Versionsverifikation

Jede hier beschriebene Verhaltensweise wurde direkt im installierten Paket
verifiziert - lokal `crewai==1.15.9`, produktiv gepinnt `crewai[anthropic]
==1.15.11` (`requirements.txt`) - nicht aus Dokumentation oder Trainings-
wissen angenommen. `checkup.py` läuft vor jedem Commit gegen beide
Versionen (siehe Kapitel 13).

---

## 11. Persistenz und Datenmodell

Kein Datenbank-Server - alles ist Klartext-JSONL (eine JSON-Zeile pro
Record) unter `STATE_DIR` (`jsonl_store.py`: `read_jsonl`/`write_jsonl`/
`append_jsonl`, geteilt zwischen `tools.py` und `holding.py`). `STATE_DIR`
ist `/data` auf Railway - durabel über Cron-Ticks hinweg nur, wenn dort
tatsächlich ein Railway Volume angehängt ist (aktuell live bestätigt: ein
Volume namens `data`, Status "Ready", gemountet auf `/data` - siehe Kapitel
15 zur Historie, warum das nicht einfach angenommen werden sollte). Lokal/
für Tests ist `STATE_DIR` ein gewöhnliches Verzeichnis ohne Volume-Bezug,
per `STATE_DIR`-Umgebungsvariable überschreibbar.

### 11.1 Subsidiary-Ebene (`tools.py`, direkt unter `STATE_DIR`)

| Datei | Inhalt |
|---|---|
| `hypotheses.jsonl` | Alle Hypothesen (aktiv/evaluiert/begraben) inkl. Ökonomie, Outcome, Pivot-Kette |
| `channels.jsonl` | Kanal-Roster (Bullseye) |
| `signups.jsonl` | Echte Signups, aus GitHub Issues synchronisiert |
| `approval_queue.jsonl` | Menschliche Freigabe-Queue (die *einzige* im ganzen System) |
| `task_orders.jsonl` | Sub-CEO → Growth/Dev Aufträge |
| `content_drafts.jsonl` | Entworfene/geplante/entfernte Community-Posts |
| `research_findings.jsonl` | Research-Evidence-Tier-Einträge |
| `knowledge_base.jsonl` | Distillierte Takeaways pro Thema/Kanal/Taktik (siehe Kapitel 5.7) |
| `usage_history.jsonl` | Token-Nutzung pro Zyklus |
| `last_cycle_note.txt` | Kontinuitätsnotiz für den nächsten Zyklus |
| `telegram_update_offset.txt` | Zuletzt verarbeitetes Telegram-Update |
| `system_paused.json` | Pause-Status (Telegram `stop`/`start`) |

### 11.2 Holding-Ebene (`holding.py`, `STATE_DIR/_holding/`)

| Datei | Inhalt |
|---|---|
| `subsidiaries.jsonl` | Subsidiary-Register inkl. Policies + Policy-Historie |
| `pivot_proposals.jsonl` | Pivot-Vorschläge der Sub-CEOs |
| `cross_subsidiary_requests.jsonl` | Cross-Subsidiary-Anfragen |
| `status_reports.jsonl` | Sub-CEO → Main-CEO Berichte |
| `strategic_directions.jsonl` | Main-CEO → Sub-CEO Ausrichtungen |
| `ideas.jsonl` | Idee-Intake, jeder Agent schreibt, Main-CEO routet (Kapitel 7.2) |

### 11.3 Repo-lokal, versioniert (kein `STATE_DIR`)

| Datei | Inhalt |
|---|---|
| `agent_profile.json` | Modell-/Limit-Profile (Kapitel 9) |
| `reach_estimators.json` | Fallback-Multiplikatoren für Reichweitenschätzung + Änderungshistorie |
| `index.html` | Aktuelle Landing Page |

---

## 12. Deployment und Kommunikation mit Railway

### 12.1 Build und Ausführung

`Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "crew.py"]
```

`railway.json`:

```json
{
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": { "cronSchedule": "0 */2 * * *" }
}
```

Railway baut das Docker-Image bei jedem Push auf `main` neu und startet den
Container gemäß Cron-Schedule (alle 2 Stunden, volle Stunde) - kein
Dauerbetrieb, kein Webserver, kein offener Port. Zwischen den Läufen
existiert kein laufender Prozess; der Zustand überlebt trotzdem, weil er im
angehängten Railway Volume liegt (`/data`, `STATE_DIR`) - siehe Kapitel 11
für die Nuance, was das genau heißt, und Kapitel 15 für die Historie dazu.
Mit jetzt 2 statt ursprünglich 6 Stunden läuft ein Zyklus dreimal so oft pro
Tag - `CYCLE_TOKEN_BUDGET` (Kapitel 9.4) gilt pro Zyklus, nicht pro Tag, das
Tagesbudget steigt also implizit mit; kein zusätzliches Guardrail dafür
eingebaut, nur zur Kenntnis.

### 12.2 Umgebungsvariablen in Railway setzen

Über das Railway-Dashboard (Service → Variables) oder das CLI:

```bash
railway variables set ANTHROPIC_API_KEY=sk-ant-...
railway variables set TELEGRAM_BOT_TOKEN=...
railway variables set TELEGRAM_CHAT_ID=...
railway variables set GITHUB_TOKEN=ghp_...
```

Siehe Kapitel 14 für die vollständige Liste.

### 12.3 Beobachtung

```bash
railway status              # aktueller Service-/Deployment-Status
railway deployment list      # Historie der letzten Deployments (BUILDING/SUCCESS/FAILED/REMOVED)
railway logs                 # Logs des aktuellen/letzten Laufs
```

`REMOVED`-Einträge in `deployment list` sind normal für einen Cron-Service -
der Container wird nach jedem Lauf wieder abgebaut, nicht dauerhaft am
Laufen gehalten.

### 12.4 Manuelles Auslösen / Volume-Zugriff

Ein Zyklus lässt sich nicht direkt per Railway-CLI "on demand" auslösen
(kein Trigger-Befehl für Cron-Services) - dafür entweder auf den nächsten
planmäßigen Lauf warten oder lokal mit `STATE_DIR` auf einen lokalen Pfad
gesetzt testen (siehe `checkup.py`). Der persistente Zustand
(`/data`) ist ausschließlich über einen laufenden Container einsehbar, nicht
direkt über das Railway-Dashboard als Dateibrowser.

---

## 13. Tests

`checkup.py` ist die vollständige, eigenständige Testsuite - **kein**
pytest, sondern ein einfacher Runner mit `globals()`-Auto-Discovery aller
`test_*`-Funktionen. Aufruf:

```bash
python checkup.py
```

Wichtige Eigenschaften:

- Läuft gegen ein **eigenes, temporäres** `STATE_DIR`
  (`tempfile.mkdtemp`), das nach dem Lauf wieder gelöscht wird - berührt nie
  den echten Produktionszustand.
- Ruft **nie** `crew.kickoff()` auf (würde echte Anthropic-API-Kosten
  verursachen) und **nie** `open_pull_request` mit einem echten Token (kein
  echter GitHub-Branch/PR als Nebeneffekt eines Testlaufs).
- Jedes Tool wird über sein `.run(...)`-Interface einzeln aufgerufen -
  direkter Funktionstest, keine LLM-Aufrufe.
- Deckt ab: Score-/Break-even-/Outcome-Formeln, jedes einzelne Tool
  inklusive aller Fehlerpfade, den `max_iter`-Patch (Rollen-Assertion, beide
  gepatchten Module), Agent-Profil-Struktur und -Ladefunktion,
  Tool-Zuordnung pro Agent, das `ConditionalTask`-Budget-Gate, alle
  Telegram-Kommandos (klassifizieren + anwenden).
- Wird routinemäßig gegen **beide** unterstützten crewai-Versionen
  ausgeführt (lokal `1.15.9`, produktiv gepinnt `1.15.11`), da mehrere
  crewai-Verhaltensweisen (siehe Kapitel 10.6/10.7) versionsabhängig direkt
  im Quellcode verifiziert wurden statt angenommen.
- Stand zuletzt: **230 Tests, 230/230 bestanden** auf beiden Versionen.

Ausgabe: Klartext-Report mit `[PASS]`/`[FAIL]`/`[ERR ]` pro Test, am Ende
eine Zusammenfassung; Exit-Code `0` nur wenn wirklich alles bestanden hat.

---

## 14. Umgebungsvariablen

| Variable | Pflicht? | Zweck |
|---|---|---|
| `ANTHROPIC_API_KEY` | Ja | Zugriff auf Claude für alle vier Agenten |
| `STATE_DIR` | Nein (Default `/data`) | Wurzelverzeichnis für den gesamten persistenten Zustand |
| `TELEGRAM_BOT_TOKEN` | Nein | Zyklus-Benachrichtigungen und Fernsteuerung (Kapitel 8.2) - ohne Token: Warnung im Log, kein Crash |
| `TELEGRAM_CHAT_ID` | Nein | Ziel-Chat für Telegram-Nachrichten, gleiches Verhalten wie oben |
| `GITHUB_TOKEN` | Nein | Erhöht das GitHub-API-Rate-Limit für Signup-Sync; **erforderlich** für `open_pull_request` (Dev-Agent) - ohne Token liefert das Tool einen klaren Fehler statt eine Aktion vorzutäuschen |
| `RAILWAY_ENVIRONMENT_ID` | Nein, von Railway gesetzt | Nur gelesen, nie gesetzt - Signal für `check_state_persistence` (Kapitel 9.7), dass der Prozess überhaupt in Railway läuft |
| `RAILWAY_VOLUME_MOUNT_PATH` / `RAILWAY_VOLUME_NAME` | Nein, von Railway gesetzt, "falls ein Volume angehängt ist" | Nur gelesen, nie gesetzt - `check_state_persistence` vergleicht `RAILWAY_VOLUME_MOUNT_PATH` gegen `STATE_DIR` |

---

## 15. Bekannte Grenzen und offene Punkte

- **`agent_profile.json` steht aktuell auf `"testing"`** (Claude Haiku 4.5,
  stark reduzierte Limits) - das ist ein bewusster, dateibasierter
  Temporär-Zustand für die aktive Entwicklungsphase, kein Produktionsbetrieb.
  Umschalten auf echte Qualität: `active_profile` auf `"normal"` setzen und
  neu deployen.
- **Nur eine Subsidiary registriert.** Die gesamte Main-CEO/Holding-Ebene
  (Cross-Subsidiary-Requests, Research-Archiv über mehrere Subsidiaries,
  `move_to_subsidiary`-Pivot-Entscheidung) ist funktionsfähig, aber mangels
  einer zweiten Subsidiary im Alltag noch nicht wirklich belastet.
- **Historie: `estimated_build_cost` war anfangs mit Agentur-Logik statt
  KI-Agenten-Ökonomie geschätzt worden.** `hyp_bootstrap_001` (die allererste
  Hypothese) setzte `estimated_build_cost=15000` für eine einfache Landing
  Page - ein plausibler Preis für ein menschliches Dev-Team/eine Agentur,
  aber um Größenordnungen zu hoch für das, was der Dev-Agent hier
  tatsächlich zahlt (LLM-Tokenkosten, real eher niedrige einstellige
  Dollarbeträge). Das verzerrte `break_even_users` auf 52 statt einer
  realistischen niedrigen einstelligen Zahl - genau der Fall, für den
  dieses System eigentlich entworfen wurde (schon 2 echte Nutzer können
  ein `build` rechtfertigen). Behoben durch das Pflichtfeld
  `build_cost_reasoning`, `SIMPLE_BUILD_COST_CEILING` (10 USD) mit
  Substanz-Anforderung darüber, und einen auf 1 Monat gesenkten
  Standard-`break_even_horizon_months` (Kapitel 5.1) - `task_ceo` enthält
  außerdem einen einmaligen Rekalibrierungs-Schritt, der bestehende
  Hypothesen mit alten, zu hohen Schätzungen selbstständig korrigiert.
- **Kein produktives Monitoring-Produkt existiert noch.** Das System befindet
  sich bewusst in der Hypothesis-Testing-Phase (Kapitel 1/5) - es hat noch
  kein einziges `build`-Outcome durchlaufen.
- **X (Twitter) hat keine kostenlose öffentliche Metrik-API mehr** - jede
  Reichweitenmessung dort braucht entweder eine kostenpflichtige API-Stufe
  (eine `spend`-Freigabe) oder manuell gelieferte `metrics_json`-Werte.
- **`discord_telegram`-Multiplikator in `reach_estimators.json` ist
  ausdrücklich ein unvalidierter Platzhalter** (siehe die `notes` in der
  Datei selbst) - sollte über `update_reach_multiplier` kalibriert werden,
  sobald genug echte Datenpunkte vorliegen.
- **Der `max_iter`-Rollen-Patch (`crewai_patches.py`) ist ein Workaround für
  einen crewai-eigenen Bug, kein Upstream-Fix** - sollte crewai das Verhalten
  irgendwann selbst korrigieren, kann der Patch entfernt werden (er ist
  defensiv geschrieben und würde bei geändertem crewai-Internal einfach
  übersprungen, aber nicht mehr nötig sein).
- **Kein manuelles "jetzt sofort einen Zyklus auslösen"** über Railway
  selbst (Kapitel 12.4) - nur der planmäßige 2h-Cron oder ein lokaler Testlauf.
- **Historie: `/data` war lange Zeit kein echtes Volume.** Frühere Versionen
  dieses Dokuments behaupteten unverifiziert, `/data` sei "das persistente
  Railway-Volume" - `railway volume list` zeigte tatsächlich lange Zeit
  keinerlei Volume für diesen Service. Jeder Redeploy (jeder Push nach
  `main`) hat dadurch den kompletten Zustand gelöscht; nur zwischen
  Cron-Ticks *desselben* Deployments (kein neuer Push dazwischen) blieb er
  erhalten, was das Problem lange verschleiert hat. Direkt per Log-Vergleich
  über mehrere Cron-Ticks desselben Deployments nachgewiesen (identische
  `created_at`-Zeitstempel über mehrere Stunden hinweg vs. ein leerer
  Roster unmittelbar nach jedem neuen Deploy). Inzwischen ist ein echtes
  Volume angehängt (`railway volume list` zeigt `data`, Status "Ready",
  Mount-Pfad `/data`) - der `check_state_persistence`-Tool-Aufruf (Kapitel
  9.7) prüft das jetzt bei jedem Zykluststart automatisch mit, damit ein
  erneutes stillschweigendes Fehlen nicht wieder unbemerkt bliebe.
- **`railway status --json` zeigte einen widersprüchlichen `cronSchedule`-
  Wert:** Auf Service-Instance-Ebene stand `"0 */6 * * *"`, während die
  Deployment-Metadaten (`fileServiceManifest`/`serviceManifest`, aus
  `railway.json`) bereits korrekt `"0 */3 * * *"` zeigten - über mehrere
  Deploys hinweg unverändert, also keine bloße Propagations-Verzögerung.
  Nicht per CLI direkt korrigierbar (keine `railway service`-Unterkommando
  dafür gefunden). **Update:** nach der Umstellung auf 2h per echten
  Cron-Lauf-Zeitstempeln verifiziert (Audit-Addendum) - sieben
  aufeinanderfolgende Läufe (22:04, 00:04, 02:01, 04:00, 06:03, 08:01, 10:00)
  lagen tatsächlich ~2h auseinander, die Service-Instance-Diskrepanz ist mit
  dem 2h-Deploy nicht mehr reproduzierbar.
- **Audit-Addendum (Dev/Growth-Limits, Lean-Startup-Tiefe,
  Pricing-Isolation, Idee-Intake):** Statusaudit bestätigte drei
  unabhängige Ursachen für veraltete Zyklus-Reports gleichzeitig: (1) der
  Report-Format-Rewrite aus einer früheren Session-Anfrage war nie
  tatsächlich implementiert (`send_cycle_summary` gibt bis heute rohe,
  fix-gekappte Task-Outputs pro Sektion aus, kein "Für den
  Aufsichtsrat"-Abschnitt existiert im Code), (2) `hyp_bootstrap_001` trug
  die alten Zahlen ($15.000/6 Monate/52) noch, weil das System seit kurz
  nach dem Rekalibrierungs-Deploy per Telegram `stop` pausiert war - der
  einmalige Selbstkorrektur-Schritt in `task_ceo` hatte schlicht noch nie
  die Chance zu laufen -, und (3) die `evidence_stage`-Leiter aus einer
  früheren Anfrage existierte überhaupt nicht im Code. Dev-Agent-Stall
  (drei-plus aufeinanderfolgende Zyklen mit identischem "need to provide
  complete HTML content"-Abbruch) direkt auf `agent_profile.json`s
  `testing`-Profil zurückgeführt: `max_iter=4`/`max_tokens=500` machen eine
  vollständige Landing-Page-HTML strukturell unfertigbar - in echten
  Cron-Logs bestätigt (drei fehlgeschlagene `open_pull_request`-Aufrufe
  ohne `file_content`, dann `max_iter` erreicht, nie darüber hinaus
  gekommen). Behoben in diesem Addendum: `growth`/`dev`-Limits angehoben
  (Kapitel 9.1), Evidence-Stage-Leiter (Kapitel 5.9), Payment-Intent-Test
  (Kapitel 5.10) und Idee-Intake/Routing (Kapitel 7.2) neu gebaut. Der
  Report-Format-Rewrite selbst ist weiterhin **nicht** umgesetzt - das war
  nicht Teil dieses Addendums und bleibt ein offener Punkt.
- **Pricing-/Ökonomie-Isolation zwischen Subsidiaries ist strukturell noch
  nicht gegeben, nur weil sie noch nie gebraucht wurde.** `tools.py` hat
  genau ein modulweites `STATE_DIR`, `hypotheses.jsonl`/`channels.jsonl`
  tragen kein `subsidiary_id`-Feld, und `crew.py` verdrahtet genau eine
  `Crew` fest auf `api-sentinel`. Kein hartcodierter Preis-Default wurde im
  Code/in den Prompts gefunden (nur illustrative Beispiel-Spannen wie
  "~EUR5/Monat" bzw. "~EUR29-99/Monat" in `task_ceo`s Pricing-Tier-
  Anleitung, `$49` in `hyp_bootstrap_001` ist reine Agenten-Entscheidung),
  aber falls je eine zweite Subsidiary operativ liefe (eigene Crew gegen
  dasselbe `tools.py`), würden ihre Hypothesen/Preise faktisch in dieselbe
  `hypotheses.jsonl` schreiben - keine bloß ungetestete Annahme, sondern ein
  echter Kollisionspfad. Bewusst nicht in diesem Addendum behoben (wäre ein
  größerer Umbau von `tools.py`s Kern-Datenmodell) - `register_subsidiary`
  markiert das jetzt explizit auf jedem neuen Record
  (`operative_capability`, Kapitel 7), damit es nirgends stillschweigend
  als "isoliert" missverstanden wird.
