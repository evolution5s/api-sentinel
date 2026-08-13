# API Sentinel

> **Hinweis zur Pflege:** Dieses Dokument beschreibt den Code-Stand in `crew.py`,
> `tools.py`, `holding.py`, `scoring.py`, `crewai_patches.py` und
> `agent_profile.json`. Insbesondere Kapitel 3 (Agenten) und Kapitel 9
> (Modelle/Token/Limits) müssen aktualisiert werden, sobald sich Rollen,
> Tools, Prompts oder Limits ändern - sie sind eine Momentaufnahme, keine
> automatisch generierte Doku. Siehe auch `OPERATING_MODEL.md`: dieselbe
> Momentaufnahme-Pflicht, aber als sequenzieller Entscheidungs-Walkthrough
> statt Kapitel-für-Komponente-Referenz - beide bei jeder relevanten
> Code-Änderung zusammen pflegen, nicht nur eine der beiden.

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
| `max_duration_days_by_stage` | `DEFAULT_PROPOSED_DURATION_CAPS` (`status='proposed'`) | Zeitbox-Obergrenze pro Evidence-Stage - siehe Kapitel 5.11, nur wirksam nach expliziter Telegram-Bestätigung |

Ändern nur über `update_subsidiary_policies` - erfordert eine bereits
freigegebene `request_approval`, exakt dieselbe Disziplin wie
`register_subsidiary`. Jede Änderung wird mit Zeitstempel, geändertem Feld,
Freigabe-ID und Begründung in `policy_history` auf dem Subsidiary-Record
protokolliert. `max_duration_days_by_stage` ist die eine Ausnahme von dieser
Freigabe-Pflicht: es wird direkt per Telegram-Bestätigung gesetzt (Kapitel
5.11, 8.2), nicht über `update_subsidiary_policies` - eine bereits explizit
vom Menschen angestoßene Bestätigung, kein neuer Freigabe-Weg.

**Wichtig:** Die konkrete Kanalliste für API Sentinel (r/algotrading,
r/quantfinance, r/quant, QuantConnect Forum/Discord, Elite Trader,
Trade2Win, quant.stackexchange.com) ist bewusst *nicht* Teil dieser
holdingweiten Policies - sie steht im Task-Text von `task_channel_strategy`
in `crew.py`, weil sie API-Sentinel-spezifisch ist. Jede neue Subsidiary
bekäme ihre eigene, andere Kanalliste.

### 4.3 Entscheidungs-Framework: Two-Way vs. One-Way Doors

Ein Textbaustein, der wörtlich (namentlich referenziert) in `ceo_agent`s und
`main_ceo_agent`s `Backstory` steht (`crew.py`) - keine einmalige
Hintergrundinfo für einen Fix, sondern ein Standing Operating Principle,
das jeden Zyklus aktiv angewendet werden soll. Grund für die Einführung:
das genaue Gegenteil-Muster, das `hyp_bootstrap_001` zum Verhängnis wurde
(Kapitel 15) - eine Two-Way-Door-Entscheidung (eine erste
Preis-Schätzung, ein Recherche-Plan) wurde mit übervorsichtigem Hedging
behandelt, während eine echte One-Way-Door-Entscheidung (Ökonomie und
Landing Page festgelegt, bevor das zugrunde liegende Problem überhaupt
bestätigt war) mit Two-Way-Door-Tempo durchgewunken wurde - genau
andersherum als richtig.

- **Two-Way Doors** (Bezos, Shareholder-Letter 1997): die meisten
  taktischen Entscheidungen auf Hypothesen-Ebene - welcher Kanal zuerst,
  was recherchiert wird, eine grobe erste Preis-Schätzung, ob eine Variable
  pivotiert wird. Billig zu testen, billig umzukehren - **schnell und
  selbstbewusst entscheiden**, nicht absichern oder über-formalisieren.
  Genau die Zone, in der der Sub-CEO ohnehin schon ohne Rückfrage agieren
  darf.
- **One-Way Doors**: bereits an Tier 1/2 gegated (echtes Spend,
  Veröffentlichung, Build-Commitments) - bleiben zu Recht vorsichtig und
  eskaliert. Das bestehende Drei-Tier-Modell (Tier 0 autonom, Tier 1
  freigabepflichtig, Tier 2 nur Mensch) ändert sich durch dieses Framework
  nicht - es verbessert die Qualität des Urteils *innerhalb* der Tier-0-Zone,
  die der Sub-CEO ohnehin schon hat.
- **Ground truth over assertion** (Anthropics eigene Guidance zum Bauen von
  Agenten): jede Behauptung in der Sub-CEO-eigenen Reasoning - "das Problem
  ist validiert", "die Zielgruppe erkennt den Bedarf", "die Kosten sind X" -
  muss auf etwas Abrufbares aus der eigenen Arbeit dieser Hypothese diesen
  Zyklus zurückführbar sein: ein echtes Tool-Ergebnis, ein geloggter
  Research-Fund, ein echt gepostetes Artefakt. Mechanisch erzwungen über den
  Anti-Copying-Tripwire (Kapitel 5.12) und die Artefakt-Gates (Kapitel 5.9/5.11).
- **Disagree and commit**, angewendet auf Main-CEO ↔ Sub-CEO: innerhalb des
  strategischen Rahmens (`set_strategic_direction`) gehören taktische
  Entscheidungen dem Sub-CEO - keine Rückfrage nötig für Dinge, die ohnehin
  in dessen eigener Spur liegen. Widerspricht die eigene Evidenz des
  Sub-CEO dem Rahmen, eskaliert er über einen echten Pivot-Vorschlag
  (`file_pivot_proposal`) - nie stillschweigendes Abdriften, nie
  stillschweigendes Befolgen eines Rahmens, dem die eigenen Daten
  widersprechen. Umgekehrt gilt dasselbe für den Main-CEO: übersteuert eine
  taktische Sub-CEO-Entscheidung nie direkt, sondern setzt bei echtem
  Dissens eine neue strategische Richtung.

---

## 5. Der Build-Measure-Learn-Loop (Hypothesis Engine)

Kernstück ist `task_ceo` (Sub-CEO) zusammen mit den reinen, testbaren
Funktionen in `scoring.py`. Ablauf pro Hypothese:

### 5.1 Anlegen einer Hypothese

Seit dem Structural-Rebuild-Addendum (Kapitel 15) hängen Pflichtfelder von
`evidence_stage` ab, nicht mehr von einer einzigen flachen Checkliste
(Bezos Two-Way/One-Way-Door-Rahmen, Kapitel 4.3). Immer erforderlich, an
jeder Hypothese (Bootstrap in Schritt 0 oder Pivot-Folgehypothese in
Schritt 5 von `task_ceo`):

- `hypothesis_type`: `value` (löst ein echtes Nutzerproblem) oder `growth`
  (hilft bei Distribution/Skalierung von bereits Validiertem).
- `impact_score`, `confidence_score` - eigenes ehrliches Urteil, gleiche
  Form wie bei einem Kanal (Kapitel 6.1) - das Ranking-Signal, mit dem
  konkurrierende Hypothesen-Ideen gegeneinander priorisiert werden, sobald
  mehr Ideen als Kapazität vorhanden sind (Kapitel 5.8).
- `evidence_stage` selbst - nicht mehr optional, jede Hypothese erklärt von
  Anfang an, wo sie tatsächlich steht (Kapitel 5.9).
- `duration_days` - die verpflichtende Zeitbox (immer erforderlich), plus
  optional `sample_size_trigger` für eine frühere Fälligkeit (Kapitel 5.6).
  Ab Bestätigung einer `max_duration_days_by_stage`-Policy (Kapitel 5.13)
  gilt zusätzlich eine Obergrenze pro Stage.
- Genau eine ungetestete Variable pro Test: bei einem Pivot-Folgetest
  `pivot_variable_changed`; beim ersten Versuch einer Linie (kein
  `prior_hypothesis_id` gesetzt, auch die allererste Hypothese überhaupt)
  `primary_variable_tested` - beide aus derselben Menge (`audience`/
  `price`/`copy`/`channel`/`timing`). Nie mehrere echt ungetestete
  Variablen in einen ersten Test bündeln (z.B. neue Zielgruppe *und* neuer
  Preis gleichzeitig) - sonst lässt sich das Ergebnis keiner der beiden
  Änderungen eindeutig zuordnen. Optional dazu `holding_constant_notes` -
  was bewusst konstant gehalten wird.

**Bei `research`/`community_engagement` (Two-Way Door - billig, schnell,
reversibel):** `estimated_build_cost`, `price_point_monthly`,
`break_even_horizon_months`, `break_even_users`, `build_cost_reasoning`
sind **noch nicht** erforderlich. Optional `rough_economics_note`
(Freitext) für eine Größenordnungs-Schätzung fürs eigene Planen (z.B.
"vermutlich EUR15-50/Monat, je nachdem was wir lernen - noch nicht
berechnet") - klar getrennt von den späteren, belastbaren Zahlen.
`compute_break_even()` verweigert die Berechnung an diesen Stages absichtlich
(`applicable: false`) - eine Platzhalter-Schätzung als präzise Zahl zu
tarnen ist genau das, was diese Änderung verhindern soll. `evidence_stage=
'research'` verlangt zusätzlich den Forschungsplan zuerst (Kapitel 5.11).

**Bei `landing_page`/`build` (Übergang zur One-Way Door - echte Kosten,
echtes Commitment):** `estimated_build_cost`, `price_point_monthly`,
`break_even_horizon_months`, `break_even_users`, `build_cost_reasoning`
werden Pflicht und müssen jetzt präzise, evidenzbasiert sein.
`estimated_build_cost` muss auf echter KI-Agenten-Ökonomie beruhen -
Dev-Agent-LLM-Aufrufe plus etwaige echte laufende Infra-Kosten (Hosting,
Domain) - **nie** auf einem Marktpreis/Agentur-/Freelancer-Satz; eine
Landing Page/ein Signup-Formular/ein kleines Backend-Skript kostet hier
realistisch einen niedrigen einstelligen Dollarbetrag in Tokens, keine
Hunderte oder Tausende. `build_cost_reasoning` muss die Kosten in echte
Komponenten aufschlüsseln, **spezifisch für diese eine Hypothese** - nie
aus Beispieltext in Instruktionen/früheren Addenda/dieser Dokumentation
übernommen oder umformuliert (mechanisch abgelehnt bei Übereinstimmung mit
bekannten Textbausteinen, Kapitel 5.12 - genau das disqualifizierte
`hyp_bootstrap_001`, Kapitel 15). Über `SIMPLE_BUILD_COST_CEILING` (10,0
USD) ohne substanzielle Begründung (mind.
`BUILD_COST_JUSTIFICATION_MIN_LENGTH` = 80 Zeichen, echter zusätzlicher
Token-/Iterationsaufwand, nicht "fühlt sich teurer an") wird abgelehnt.
`break_even_horizon_months` defaultet konzeptionell auf 1 Monat - länger
braucht ebenfalls `build_cost_reasoning`. `break_even_users` **nie** von
Hand geschätzt, sondern via `compute_break_even()`
(`scoring.compute_break_even_users`): `ceil(build_cost / (price_point_
monthly * horizon_months))`. Der Übergang selbst braucht zusätzlich
artefaktbelegte Historie durch `research` **und** `community_engagement`
(Kapitel 5.9) - oder eine vom Main-CEO genehmigte Stage-Skip-Anfrage
(Kapitel 5.9, 7.3).

Dazu optionale, freitextliche Reasoning-Felder (keine neue Pass/Fail-Hürde,
nur dokumentierte Abwägung, ebenfalls dem Anti-Copying-Tripwire unterworfen
für `defensibility_notes`): `defensibility_notes` (könnte ein Solo-
Entwickler das an einem Nachmittag mit einem LLM nachbauen?),
`pricing_tier_reasoning` (niedriger Preis braucht großes, scharfes
Painpoint + Volumen; höherer Preis braucht weniger Volumen, aber
langsamere Adoption), `expansion_notes` (Upsell-/B2B-Potenzial, rein
zukunftsgerichtet), `channel_fit_reasoning` (warum genau dieser Kanal zu
genau dieser Zielgruppe passt).

Vor der Formulierung einer neuen Hypothese: `read_knowledge_base(topic=...)`
prüfen (Kapitel 5.7) - vielleicht existiert schon eine distillierte
Erkenntnis zu diesem Thema/Kanal/Taktik aus einem früheren Zyklus. Research-
Evidence-Tier (`read_research_findings`/`log_research_finding`) ist jetzt
der Standard-erste Schritt (`evidence_stage='research'`), nicht optional
davor - Wettbewerbsprodukte, Forendiskussionen, Antworten auf einen echten
`own_question_post`. Günstiger und schneller als ein Live-Test, aber
schwächere Evidenz: kann `test_further`/`pivot`-Reasoning stützen, nie
allein zu `build` führen.

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
innerhalb von `[created_at, created_at + duration_days]` (zur Herkunft der
Signups selbst siehe Kapitel 6.4).

`evaluate_hypothesis(hypothesis_id)` ist reine Leseoperation (persistiert
selbst nichts, der Sub-CEO ruft danach `write_hypothesis` auf) und lehnt
mit einem klaren Fehler ab statt zu bewerten, wenn `measured.reach_estimate`
oder `break_even_users` noch fehlen. Rückgabe: `hypothesis_id`,
`conversions`, `estimated_reach`, `score`, `verdict`, `outcome`,
`break_even_users`, `pivot_attempts_so_far`. `verdict`
(`scoring.verdict_for_score`) ist ein rein menschenlesbares Zusatzlabel
neben dem eigentlichen `outcome` - dieselben Bandgrenzen wie die
Score-Klassifikation, aber als Prosa-Text, nicht als das, worauf die
Vier-Wege-Logik selbst verzweigt (das bleibt `classify_outcome`, Kapitel
5.3):

| Score-Band | `verdict` |
|---|---|
| `>= 0.7` | `strongly validated` |
| `>= 0.3` | `weakly positive` |
| `>= -0.3` | `inconclusive` |
| `>= -0.7` | `weakly negative` |
| `< -0.7` | `strongly devalidated` |

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

**Seit dem Structural-Rebuild-Addendum ein echtes Gate, kein
selbstunterschriebener Freibrief mehr.** Die vorherige Version (jeder
Stage-Skip per selbstgeschriebenem `stage_justification`-String erlaubt)
war zu leicht trivial zu erfüllen - genau das, was `hyp_bootstrap_001`
den direkten Sprung zur Landing Page erlaubte (Kapitel 15). Pflichtfeld
auf `write_hypothesis` (nicht mehr optional), geordnet von billigstem/
schwächstem zu teuerstem/stärkstem Signal: `EVIDENCE_STAGES = ["research",
"community_engagement", "landing_page", "build"]`.

- `evidence_stage='research'` verlangt zuerst den Forschungsplan (Kapitel
  5.11).
- `evidence_stage='community_engagement'` verlangt ein echtes, gepostetes
  (oder freigegeben-und-wartendes) `thread_reply`/`own_question_post`-
  Draft für diese Hypothese (`draft_content`) - eine bloße Behauptung
  reicht nicht.
- Der Übergang zu `landing_page`/`build` verlangt artefaktbelegte Historie
  durch **beide** früheren Stages: ein substanzielles `log_research_
  finding`-Ergebnis (Kapitel 5.11) **und** ein echtes `community_
  engagement`-Draft. Fehlt eines von beiden, wird `write_hypothesis`
  abgelehnt - **es sei denn**, eine vom Main-CEO genehmigte Stage-Skip-
  Anfrage (`file_stage_skip_request`/`decide_stage_skip_request`,
  `holding.py`, Kapitel 7.3) liegt für genau diese `hypothesis_id` und
  `target_stage` vor. Das ist die Main-CEO-Review, die einen self-written
  Freibrief ersetzt - der Main-CEO bestätigt nur, wenn das Überspringen
  wirklich zutrifft (z.B. Recherche ist für diese konkrete Frage wirklich
  nicht relevant), sonst geht es zurück zu den früheren Stages.
- `file_task_order(to_role="dev", ...)` prüft `evidence_stage` weiterhin,
  aber ohne eigenen Bypass mehr - das echte Gate sitzt jetzt vollständig
  bei `write_hypothesis`; ist `evidence_stage` erst `landing_page`/`build`,
  wurde es bereits verdient.

Die eigentliche Frage ("was ist der billigste Test, der diese Unsicherheit
tatsächlich auflöst") bleibt Ermessenssache des Sub-CEO (Kapitel 4.3,
Two-Way-Door-Prinzip) - das Gate erzwingt nur die Nachvollziehbarkeit
(echte Artefakte oder eine echte Main-CEO-Entscheidung), nicht das Urteil
selbst. Die meisten Hypothesen erreichen weiterhin schnell eine Landing
Page - das ist der bewährte Standardpfad dieses Systems - der Punkt ist,
dass es ein verdienter, evidenzbasierter Schritt ist, kein übersprungener.

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

### 5.11 Forschungsplan und Artefakt-Pflicht

**Vor** Beginn der Recherche loggt der Sub-CEO (auf dem Hypothesen-Record,
beim `write_hypothesis`-Aufruf, der `evidence_stage='research'` setzt) drei
Pflichtfelder - der Forschungsplan:

- `research_objective` - die eine konkrete Frage, die diese Recherche
  beantworten soll (z.B. "enthält r/algotrading, r/quantfinance oder das
  QuantConnect-Forum echte, aktuelle Erstpersonen-Berichte über API-Ausfälle,
  die Trading-Verluste oder operative Probleme verursacht haben - nicht nur
  allgemeines API-Geplauder").
- `research_confirming_criteria` / `research_disconfirming_criteria` -
  konkret und falsifizierbar (z.B. "bestätigend: 3+ eigenständige Threads
  in den letzten 6 Monaten mit einem echten Vorfall und dessen Auswirkung;
  widerlegend: nur allgemeines Geplauder oder nichts Relevantes gefunden").

**Nach** Abschluss der Recherche muss `log_research_finding` ein echtes,
abrufbares Artefakt liefern, keine Erzähl-Behauptung: welche Threads/Posts,
was dort stand (paraphrasiert, nie erfunden), wie viele, wie aktuell - oder
ein ebenso konkretes, ehrliches negatives Ergebnis ("X/Y/Z-Begriffe in
diesen Kanälen durchsucht, keine substanzielle Evidenz gefunden").
Mechanisch mit einer Mindestlänge erzwungen
(`RESEARCH_FINDING_MIN_LENGTH` = 80 Zeichen, gleiches Muster wie
`BUILD_COST_JUSTIFICATION_MIN_LENGTH`) - ein Einzeiler wird abgelehnt.
`evidence_stage` kann nicht über `research` hinaus fortschreiten, ohne dass
mindestens ein solches Artefakt für diese `hypothesis_id` existiert (Kapitel
5.9) - im Code geprüft, nicht auf eine Selbstauskunft vertraut.

**`source` braucht eine echte, abrufbare `https?://`-URL** (Citation-Fix-
Addendum) - eine bloße Bezeichnung wie "GitHub Issue #11957" ohne Link ist
für einen Menschen nicht nachvollziehbar. `log_research_finding` lehnt
`source` ohne echte URL mechanisch ab; einzige Ausnahme ein Quelltyp ohne
echten Link (ein persönliches Gespräch, eine private DM) - dann muss
`source` wörtlich mit `"kein Link:"` gefolgt von der Begründung beginnen,
dieselbe Ehrlichkeits-Disziplin wie bei anderen Pflichtfeldern, die
manchmal genuin nicht erfüllbar sind. Die letzte geloggte Quelle pro
Hypothese wird im Zyklus-Report inklusive dieser URL angezeigt
(`build_hypothesis_overview`), nicht nur als Kurzfassung ohne Beleg.

**Echte Recherche-Werkzeuge: `search_web` / `read_webpage`.** Damit dieses
Artefakt für ein wirklich neues Thema (nicht nur API-Sentinel) überhaupt
erreichbar ist, haben `ceo_agent`, `growth_agent` und `main_ceo_agent`
zwei echte Recherche-Tools:

- `search_web(query, num_results=5)` - Websuche über die Serper.dev-API
  (`google.serper.dev/search`, Umgebungsvariable `API-Sentinel-serper` -
  bewusst nicht `SERPER_API_KEY` genannt, siehe Kapitel 14 zur Namens-
  Abweichung). Ohne gesetzten Key liefert das Tool einen klaren Fehlertext
  statt eines stillen Fehlschlags.
- `read_webpage(url)` - holt eine echte Seite per `requests`, extrahiert
  Text via BeautifulSoup (Script/Style/Nav/Header/Footer entfernt),
  kappt auf `READ_WEBPAGE_MAX_CHARS` = 6000 Zeichen mit `truncated`-Flag.

**Live gegen den echten Key getestet (Real-Serper-Key-Addendum), nicht nur
implementiert.** Mit dem tatsächlich in Railway angelegten
`API-Sentinel-serper`-Key (per `railway run`, ohne den Wert je in den Chat
zu drucken) real ausgeführt: `search_web("QuantConnect forum broker API
outage postmortem")` lieferte fünf echte, verschiedene Treffer; einer davon
(ein QuantConnect-Forumsthread über einen unangekündigten API-Zugriffs-
Fehler im Free-Tier) wurde direkt per `read_webpage` gelesen und lieferte
echten, substanziellen Klartext - zusammen ein tatsächlich funktionierender
Such-dann-Lese-Durchlauf, genau wie ein Agent ihn nutzen würde. Ein
begleitender `search_web`-Testlauf gegen Reddit-lastige Begriffe fand
ebenfalls echte, relevante Treffer, aber `read_webpage` gegen einen davon
lieferte nur eine Bot-Verifizierungsseite zurück (HTTP 200, aber der Body
ist ein Anti-Bot-JS-Challenge-Skript, kein echter Post-Inhalt) - eine neue,
live bestätigte Erkenntnis: Reddit blockiert nicht nur den `.json`-Endpunkt
(Kapitel 6.2), sondern auch normale HTML-Seiten gegen nicht-browserartige
Anfragen. Ein darauf aufbauender `log_research_finding`-Aufruf mit dem
tatsächlich gefundenen/gelesenen Inhalt als `summary` wurde real
ausgeführt: kein Anti-Copying-Tripwire-Treffer (Kapitel 5.12), und der
`evidence_stage='research'`-Artefakt-Gate-Check bestätigt direkt, dass
dieser Fund den Artefakt-Anspruch erfüllt hätte. Test in `checkup.py`:
`test_search_web_live_real_key_returns_real_results` und
`test_search_web_then_read_webpage_live_pipeline`, beide überspringen sich
selbst sauber, wenn `API-Sentinel-serper` nicht gesetzt ist (Kapitel 13).

Implementierungsentscheidung (drei Optionen echt geprüft, nicht angenommen):
`crewai_tools`-Bordmittel (`SerperDevTool`/`ScrapeWebsiteTool`/
`WebsiteSearchTool`) sind lokal installiert, aber nicht in
`requirements.txt` - in Produktion nicht vorhanden, und `WebsiteSearchTool`
bräuchte zusätzlich ein Embedding/RAG-Setup, das hier nicht gebraucht wird.
Anthropics natives `web_search`-Servertool wurde im Quellcode der
crewai-Anthropic-Anbindung geprüft: die Tool-Konvertierung
(`_convert_tools_for_interference`) akzeptiert vorgeformte
Anthropic-Tool-Dicts nur, wenn sie `input_schema`+`name`+`description`
mitbringen - ein natives `web_search_20250305`-Tool hat das nicht und würde
in der normalen (fehlschlagenden) Konvertierung landen. Gewählt wurde daher
eine schlanke Eigenimplementierung gegen die Serper.dev-API: ein API-Key,
kein ungenutzter Ballast (kein pymupdf/pytube/youtube-transcript-api/
tiktoken), Kosten vergleichbar mit der `crewai_tools`-Variante.

**Auflösung der Forschung/Community-Engagement-Zirkularität.**
`own_question_post_replies` bleibt ein gültiger Recherche-Beleg, ist aber
strukturell erst *nach* `community_engagement` erreichbar - kann also nicht
der Weg sein, ein `research`-Artefakt aus dem Stand zu erzeugen. Der
Normalweg für ein neues Thema ist `search_web`/`read_webpage`: passive
Recherche, bevor überhaupt ein eigener Post existiert. Beide Pfade bleiben
gültig, aber nicht gleichrangig - das ist jetzt explizit in der
`ceo_agent`-Backstory und in den Tasks dokumentiert, keine stillschweigende
Annahme mehr.

### 5.12 Anti-Copying-Tripwire

Mechanischer Schutz gegen genau den Fehler, der `hyp_bootstrap_001`
zusätzlich disqualifizierte (Kapitel 15): `build_cost_reasoning` erwies
sich als nahezu wortgleiche Kopie von Instruktionstext dieses Repos, nicht
als eigenständig hergeleitete Begründung. `tools._instruction_echo_match`
prüft `build_cost_reasoning`/`defensibility_notes` (`write_hypothesis`),
`summary` (`log_research_finding`) und `reasoning`
(`file_stage_skip_request`, `holding.py`) gegen eine kleine, gepflegte
Liste bekannter Textbausteine aus den eigenen Docstrings/Incident-
Beschreibungen dieses Repos (z.B. "old-economy market-rate thinking",
"agency/freelancer/employee") - ein einfacher Substring-Abgleich, keine
Fuzzy-Logik, aber ausreichend, um wortwörtliche/nahezu-wortwörtliche
Wiederverwendung zu erkennen. Bei Treffer: Ablehnung mit dem konkret
gefundenen Textbaustein in der Fehlermeldung. Getestet (`checkup.py`)
gegen genau die Formulierungsfamilie des tatsächlichen Vorfalls.

### 5.13 Zeitbox-Policy pro Stage (`max_duration_days_by_stage`)

**Bewusst kein hier vorgegebener Wert** - das wäre derselbe Fehler wie die
alten hartcodierten Ökonomie-Zahlen. Zeitbox-Obergrenzen gehören in
dieselbe Kategorie wie die bestehenden Policy-Schalter (Kapitel 4.2): eine
Entscheidung für das Board/Aufsichtsrat, nicht etwas, das hier
vorgeschrieben wird.

`holding.SUBSIDIARY_POLICY_DEFAULTS["max_duration_days_by_stage"]`
(`tools.DEFAULT_PROPOSED_DURATION_CAPS`) liefert nur einen klar als
**vorgeschlagen** markierten Startwert (`status='proposed'`:
`research=3, community_engagement=5, landing_page=14, build=None` Tage) -
**nicht stillschweigend aktiv**. Solange `status='proposed'` ist, greift
keine Obergrenze; `write_hypothesis`s Zeitbox-Prüfung liest die Policy live
(`read_subsidiary_policies`) und wird erst scharf, sobald ein Mensch sie
per Telegram bestätigt oder anpasst:

- `duration_policy: confirm` - bestätigt die aktuell vorgeschlagenen Werte
  unverändert.
- `duration_policy: <research> <community_engagement> <landing_page>
  <build>` (Tage, `none` für kein Limit) - setzt eigene Werte und
  bestätigt in einem Schritt.

Nach Bestätigung: `duration_days` über der Obergrenze für den jeweiligen
`evidence_stage` wird abgelehnt, außer `duration_extension_approval_id`
zeigt auf einen bereits freigegebenen `request_approval`-Eintrag - der
bestehende Freigabe-Weg selbst ändert sich nicht, nur woher die
Obergrenze-Zahl kommt. Solange die Bestätigung noch aussteht, taucht sie
im Zyklus-Report unter "Für den Aufsichtsrat" auf (Kapitel 9.6), bis sie
entschieden ist.

### 5.14 Zahlungsbereitschafts-und-Größe-Scan (Payment-Propensity)

**Die Lücke, die dieser Scan schließt:** Jede bisherige Recherche
validiert, ob *ein konkretes Problem* real ist - nie die vorgelagerte,
allgemeinere Frage, ob diese Nische/Community überhaupt ein echtes Muster
zeigt, für Tools/Services zu bezahlen, unabhängig von der konkreten
Hypothese. Direkt motiviert durch Jans eigene Einschätzung zu
r/algotrading: starker Open-Source-/DIY-Kultur-Eindruck, kein sichtbares
Zahlungsmuster für Add-ons - ein Marktsignal, das bisher nie systematisch
erhoben wurde. Größe allein ist **nicht** das Signal - eine kleine
Audience mit starker, hochpreisiger Zahlungsbereitschaft kann genauso
attraktiv sein wie eine große mit schwacher, weil ein hoher genug
Preispunkt den Break-even schon mit einer Handvoll Kunden erreicht. Erst
Größe **kombiniert mit** tatsächlich beobachteter Zahlungsbereitschaft ist
das eigentliche Signal.

**Wann er läuft:** Einmal pro Channel (nicht pro Hypothese), bevor die
hypothesenspezifische Recherche aus Kapitel 5.11 beginnt, sobald eine
Hypothese `evidence_stage='research'` für einen Channel erreicht, der noch
keinen aktuellen Scan hat. `read_knowledge_base(channel=..., topic='payment
propensity')` prüft zuerst die Wissensbasis - existiert bereits ein
Eintrag, der nicht älter als `tools.PAYMENT_PROPENSITY_STALENESS_DAYS` (90
Tage, bewusst ein einfacher Konstantenwert statt eines dritten,
Telegram-bestätigten Governance-Parameters neben Zeitbox-Policy/
FIX-Thresholds - eine Cache-Frische-Einstellung, keine Entscheidung mit
echten Konsequenzen) ist, wird er wiederverwendet statt erneut zu scannen.

**Was gesucht wird:** Eine breite Suche über die **gesamte** Community
(bewusst nicht auf die konkrete Hypothesenfrage verengt), über `search_web`/
`read_webpage` (Kapitel 5.11), nach realen, konkreten Signalen in beide
Richtungen:

- **Bestätigend:** echte Belege, dass Leute für Trading-Bot-as-a-Service,
  bezahlte Signal-/Discord-Gruppen, bezahlte Datenfeeds, Premium-Exchange-
  Stufen, bezahlte Indikatoren/Marktplätze oder bezahlte Backtesting-
  Plattformen tatsächlich zahlen - inklusive grober Preispunkte, wo
  auffindbar (z.B. "$30/Monat Signal-Gruppe").
- **Widerlegend:** eine wiederkehrende Präferenz für kostenlose/Open-Source-
  Tools als Standard, explizite Zahlungsunwilligkeit, DIY-Eigenbau als
  beschriebene Norm selbst bei mehr Aufwand, erwähnte aber unpopuläre/
  gemiedene bezahlte Optionen.
- **Grobe Größe/Reichweite** desselben Channels - wiederverwendet aus dem
  bereits für dessen `impact_score`/`confidence_score` erhobenen Signal
  (Kapitel 6.1), nicht neu hergeleitet.

**Wie das Ergebnis geloggt wird:** Über `write_knowledge_entry(topic=
'payment propensity scan', channel=..., confidence=..., takeaway=...,
source_hypothesis_ids=[...])` - derselbe wiederverwendbare Speicher aus
Kapitel 5.7, nur jetzt konsequent mit `channel` getaggt. Der `takeaway`
muss eine echte Größe-versus-Preis-Einschätzung sein, **nie** ein flaches
Ja/Nein: grobe Audience-Größe, ob überhaupt reale Zahlungs-Evidenz
existiert, und - wo sie existiert - die grobe Preisspanne. Ein schwacher/
negativer Befund ist bei **jeder** Audience-Größe ein vollständiges,
wertvolles Ergebnis für sich - das System darf ihn nie positiv umdeuten
oder unter optimistischer Formulierung begraben, dieselbe
Ehrlichkeitspflicht wie überall sonst in diesem System (z.B.
`bury_reasoning`, Kapitel 5.4).

**Einfluss auf die Kanal-Bewertung (Kapitel 6.1):** Der gespeicherte
Befund fließt als **Kombination mit** der Audience-Größe in `impact_score`
ein, nie als Override, der Größe einfach übergeht - ein großer, aktiver
Channel mit starker widerlegender Zahlungs-Evidenz soll einen kleineren
Channel mit echter Evidenz für hochpreisige Zahlungsbereitschaft nicht
automatisch überholen. Die kombinierte Größe-und-Preis-Begründung landet
im Channel-eigenen `notes`-Feld (`write_channel`) - sichtbar am
Channel-Record selbst, nicht in einer einzelnen Hypothesen-Ökonomie
versteckt.

**Kein mechanisches Gate.** Der Befund blockiert `evidence_stage`-
Fortschritt nicht automatisch - die Sub-CEO-eigene, evidenzbasierte
Begründung für die Weiterinvestition in einen Channel muss ihn explizit
adressieren (Kapitel 4.3), keine starre Regel entscheidet vorab.

**Kein Formel-Code für `impact_score` existiert.** `scoring.py` wurde
vollständig geprüft: es gibt keine numerische Formel, die `impact_score`/
`confidence_score`/Reichweite zu einem Ranking kombiniert - das war schon
vor diesem Addendum reines Sub-CEO-Urteil in Freitext, und bleibt es. Die
Payment-Propensity-Gewichtung ist entsprechend reiner Task-Text
(`task_ceo`), keine Code-Formel - konsistent damit, wie `impact_score`
überall sonst in diesem System funktioniert.

**Echter Scan gegen r/algotrading, live durchgeführt (nicht simuliert).**
Über `search_web` mit dem echten `API-Sentinel-serper`-Key (`railway run`,
Kapitel 5.11): drei Suchdurchläufe fanden reale, gemischte Signale in
beide Richtungen - bestätigend ein Thread, der explizit
Preisbereitschaft abfragt ("What would you pay for a bot trading
platform?"), ein Vorschlag für ein $20/Monat-Signal-Abo in einem anderen
Thread, echte erwähnte Datenkosten in der Größenordnung ~$25/Monat für
Backtesting- bzw. Live-Datenzugriff, und ein Drittanbieterprodukt
(SpeedBot) mit gestaffelter Preisstruktur, das explizit dieselbe Audience
adressiert. Gleichzeitig widerlegend: **Freqtrade** - genau die
Open-Source-Plattform, die diese Subsidiary selbst als Zielgruppe hat -
taucht wiederholt als kostenloser Standard auf, dazu mehrere "I built and
open-sourced my own X"-Posts, ein sichtbares Wiederkehr-Muster von
Eigenbau statt Kauf. Ehrliches Gesamtbild: **gemischt** - reale, wenn auch
moderate Zahlungsbereitschaft existiert (~$20-25/Monat-Größenordnung),
konkurriert aber direkt mit einer echten, sichtbaren Free-/Open-Source-
Alternativkultur - weder eine klare Bestätigung noch die von Jan erwartete
klare Ablehnung. Die tatsächliche Subscriber-Zahl von r/algotrading wurde
in diesem Scan **nicht** verifiziert (Reddits eigene API blockiert diese
Umgebung, dasselbe bekannte Problem wie in Kapitel 6.2/5.11) - bewusst
nicht geraten, sondern als offene Lücke benannt. Live-Smoke-Test:
`test_payment_propensity_scan_live_reddit_algotrading` (`checkup.py`,
Kapitel 13), überspringt sich selbst ohne gesetzten Key.

### 5.15 Hypothesen-Backlog und ICE-Scoring (`hypothesis_backlog.jsonl`)

`MAX_ACTIVE_HYPOTHESES = 3` (Kapitel 5.8) begrenzt, wie viele Hypothesen
gleichzeitig aktiv **getestet** werden - nicht, wie viele Ideen dieses
System sammeln darf. `write_backlog_candidate` legt Kandidaten in einen
separaten, bewusst nie gedeckelten Pool ab: ein angrenzender Schmerzpunkt
aus einem Research-Finding, ein anderes Zielgruppen-Segment, ein
alternativer Preis-Winkel aus einem Payment-Propensity-Scan, auch etwas
Tangentiales aus der Recherche zu einem ganz anderen Thema - kein
Selbstzensur auf das gerade aktiv Getestete.

Jeder Kandidat bekommt drei Sub-Scores (1-10, mit je einer echten,
nachvollziehbaren `_grounding`-ID - ein Research-Finding, ein Knowledge-
Base-/Payment-Propensity-Verdikt, ein Channel-Signal, nie eine
unbegründete Zahl):

- **Impact** - relativ zur *aktuellen* strategischen Richtung der
  Subsidiary bewertet, keine feste Eigenschaft der Idee. Ändert sich die
  Richtung (`set_strategic_direction`), markiert `read_backlog` betroffene
  Einträge als `impact_stale` statt den alten Wert stillschweigend
  weiterzuverwenden oder ihn eigenmächtig neu zu berechnen.
- **Confidence** - wie sicher die Grundlage ist.
- **Ease** - wie billig/schnell ein erster Test wäre.

`scoring.compute_ice_score(impact, confidence, ease)` multipliziert die
drei zu einem Score 1-1000 (Standard-ICE-Konvention, ein Produkt, keine
gewichtete Summe) - dieselbe Formel für `read_backlog`s Ranking und den
Top-4-Block im Zyklus-Report, kein zweiter, paralleler Scoring-Pfad.

**Harte Regel, direkt von Jan vorgegeben (nicht nur ein
Governance-Vorschlag zur Bestätigung):** `MIN_BACKLOG_BEFORE_ACTIVE_
PROMOTION = 10` - `write_hypothesis` verweigert jede Beförderung einer
Hypothese auf `status='active'` (neu angelegt, oder eine bestehende
reaktiviert), solange nicht mindestens 10 echte, bewertete
Backlog-Kandidaten existieren (die zu befördernde Hypothese selbst zählt
nicht mit). Grund: ein Backlog mit weniger als 10 echten Kandidaten ist
nicht breit genug, um zu vertrauen, dass die beförderte Idee tatsächlich
die beste verfügbare ist, statt einfach die einzige, die je aufgeschrieben
wurde. Gilt nur für den Übergang IN `status='active'` - eine bereits aktive
Hypothese, die aktiv bleibt, löst die Prüfung bei einem unrelated Update
nicht erneut aus. Die eine Hypothese, die bereits aktiv war, bevor diese
Regel existierte (`hyp_research_001`), läuft unter Bestandsschutz weiter;
erst die *nächste* Beförderung wird gegen die Regel geprüft.

**Anti-Stagnation, jetzt mit echtem mechanischem Fallback statt nur
Erkennung:** `scoring.spare_capacity_produced_nothing` erkennt bereits
mechanisch, wenn ein Zyklus freie Testkapazität hatte und trotzdem keinen
neuen persistenten Zustand erzeugt hat - das allein führte aber lange zu
keiner Handlung, nur zu einer Zeile im Report. `crew.py`s `__main__` prüft
das jetzt nach jedem Zyklus: hat der Zyklus selbst schon eskaliert (ein
neuer `propose_idea`-Eintrag), passiert nichts weiter. Sonst feuert der
Code selbst, mechanisch, den letzten Eskalationsschritt der
Anti-Stagnations-Anweisung nach (`propose_idea`, sichtbar im Report als
"mechanisch nachgeholt"). Die beiden vorrangigeren Reaktionen - den
nächsthöchsten Backlog-Kandidaten aktiv befördern, oder Backlog-Grooming
vorantreiben - bleiben bewusst der Prompt-Instruktion überlassen, nicht
mechanisch automatisiert: eine echte Beförderung braucht einen
Forschungsplan, Channel, `evidence_stage` und Ökonomie-Felder, die sich
nicht sicher automatisch erzeugen lassen, ohne schlechter zu sein als gar
nichts zu tun.

`hypothesis_backlog.jsonl` liegt subsidiary-gescoped unter `STATE_DIR/
<subsidiary_id>/`, gleiche Ablage wie `hypotheses.jsonl` (Kapitel 11.1).

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

**Payment-Propensity fließt in `impact_score` ein (Kapitel 5.14).** Vor
dem Setzen/Aktualisieren von `impact_score` liest `task_channel_strategy`
`read_knowledge_base(channel=..., topic='payment propensity')` - existiert
ein Befund, wird er zusammen mit der Audience-Größe gewichtet, nie als
Override, der Größe übergeht. Ein neuer Channel ohne Scan (noch keine
Hypothese hat ihn erreicht) wird vorerst allein nach Größe/Fit bewertet -
kein Blockieren der Channel-Anlage, während auf den ersten Scan gewartet
wird.

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

**Reddit-`.json`-Auto-Fetch: live getestet, aktuell blockiert.** Direkt
gegen `fetch_reddit_public_metrics` verifiziert (Structural-Rebuild-
Addendum): ein echter Abruf gegen einen realen Reddit-Post liefert gerade
`403 Client Error: Blocked` - keine Annahme, ein tatsächlich beobachtetes
Ergebnis. `read_channel_metrics` degradiert dabei sauber (Fehler +
`fetch_note`, Fallback auf manuelles `metrics_json`), stürzt nicht ab -
aber der Auto-Fetch selbst ist damit aktuell **nicht funktionsfähig**, kein
verlässlicher, laufender Mechanismus. Siehe Kapitel 15 - als live
beobachtetes, zu überwachendes Risiko geführt, nicht als gelöst.
**Update (Real-Serper-Key-Addendum):** dieselbe Blockade wurde live auch
für normale Reddit-HTML-Seiten bestätigt, nicht nur für den `.json`-
Endpunkt - `read_webpage` gegen einen echten, per `search_web` gefundenen
Reddit-Post-Link liefert HTTP 200, aber der Seiteninhalt ist eine
Bot-Verifizierungsseite (Anti-Bot-JS-Challenge), kein echter Post-Text.
Reddit blockiert also nicht-browserartige Zugriffe konsistent über beide
Zugriffswege hinweg, nicht nur den einen bereits bekannten (Kapitel 5.11).

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

### 6.4 Signup-Erfassung ohne eigenes Backend (GitHub Issues)

Die Landing Page (`index.html`) hat **keinen** Server, keine Datenbank,
keinen eigenen API-Endpunkt für Signups - das Formular (`submitForm()`)
baut aus E-Mail, gewähltem Preis-Tier, Consent-Häkchen, Zeitstempel und der
fest im HTML stehenden `LANDING_PAGE_VARIANT_ID`-Konstante (pro Variante
unterschiedlich, z.B. `"lp_v1_default"`) direkt eine vorausgefüllte GitHub-
"New Issue"-URL
(`https://github.com/evolution5s/api-sentinel/issues/new?title=...&body=...`,
Issue-Titel mit dem Präfix `[Signup]`) und leitet den Browser dorthin um.
Der Mensch klickt auf GitHub selbst noch "Submit new issue" - keine
serverseitige Logik, kein API-Key im Client nötig, GitHub Issues fungiert
als kostenloser, öffentlicher Signup-Speicher.

`sync_signups_from_github()` (`tools.py`) holt periodisch alle offenen wie
geschlossenen Issues mit diesem Titel-Präfix über die **unauthentifizierte**
GitHub Search API (`in:title "[Signup]" is:issue`, 60 Requests/Stunde ohne
Token - `GITHUB_TOKEN` erhöht nur das Rate-Limit, ist hierfür nicht
zwingend), parst den Issue-Body per Regex nach den Feldern `Email:`/`Tier:`/
`Consent:`/`Timestamp:`/`Variant:` und hängt neue Datensätze (dedupliziert
über `issue_number`) an `signups.jsonl` an. Wird **automatisch bei jedem
`read_state()`-Aufruf** ausgeführt (nicht als eigenes Tool exponiert) - ein
Sync-Fehler landet als Text im `signup_source`-Feld von `read_state()`s
Antwort statt den Aufruf abzubrechen.

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
hat aktuell genau ein `STATE_DIR`, sogar einen hartcodierten Konstante
`OWN_SUBSIDIARY_ID = "api-sentinel"` (für `read_subsidiary_policies`-Lookups
ohne zirkulären Import von `holding.py`) - kein `subsidiary_id`-Feld auf
`hypotheses.jsonl`/`channels.jsonl` - und `crew.py` verdrahtet genau eine
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
  Pivot-Vorschlag auslöst, von der Sub-CEO-Seite aus) - es liefert nur die
  Zahlen, und der Main-CEO benennt einen möglichen Stillstand explizit im
  eigenen Zyklus-Report, falls das Muster das nahelegt - ohne eine auf dem
  Papier umsatzpositive Hypothese automatisch als echten Fortschritt zu
  werten, wenn das zugrunde liegende Problem nie wirklich validiert wurde.
- **Der Gesundheits-Check hat jetzt Zähne:** Ohne Gegenmaßnahme konnte
  `possible_stall=true` jeden Zyklus aufs Neue im Main-CEO-Report auftauchen
  und wieder verschwinden, ohne dass sich strukturell irgendetwas ändert -
  ein Signal, das niemand zwingend sieht oder bestätigt. Jetzt zählt
  `assess_subsidiary_trajectory` `consecutive_stall_cycles` auf dem
  Subsidiary-Record selbst mit (persistiert in `subsidiaries.jsonl`,
  Kapitel 11.2) und setzt den Zähler zurück, sobald ein `build` auftaucht.
  Ab `STAGNATION_ESCALATION_THRESHOLD` (6 aufeinanderfolgende Zyklen ohne
  `build` - bei einem 2h-Cron rund 12 Stunden durchgehender Stillstand,
  bewusst über einen einzelnen ungünstigen Report hinaus, aber nicht tagelang
  unbemerkt) wird `stagnation_escalated=true` gesetzt. Ab dann erscheint die
  Subsidiary **jeden** Zyklus fest im "Für den Aufsichtsrat"-Telegram-Block
  (Kapitel 9.6) - nicht als einmalige Randnotiz, sondern so lange, bis ein
  Mensch per `stagnation_ack: <subsidiary_id>` (Kapitel 8.2) quittiert, oder
  bis ein echter `build` den Trend von selbst durchbricht (dann läuft der
  Reset automatisch, ohne Zutun). Bewusst **kein** automatischer
  Pivot-Auslöser - die Board-Ebenen-Grenzen (Kapitel 3.4/4) bleiben
  unverändert; es macht nur sichtbar, was vorher stillschweigend im
  Report-Rauschen untergehen konnte.

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

### 7.3 Evidence-Stage-Skip-Review (`stage_skip_requests.jsonl`)

Das Main-CEO-Gegenstück zum Artefakt-Gate aus Kapitel 5.9 - dieselbe
Sub-CEO-reicht-ein/Main-CEO-entscheidet-Form wie Pivot-Vorschläge (Kapitel
7), bewusst als eigene Datei statt in `pivot_proposals.jsonl` überladen (ein
Stage-Skip ist eine schmalere, anders geformte Entscheidung als ein volles
Strategie-Pivot mit 8 Pflichtfeldern):

- `file_stage_skip_request(hypothesis_id, subsidiary_id, target_stage,
  reasoning)` - Sub-CEO. `reasoning` läuft durch denselben
  Anti-Copying-Tripwire wie `build_cost_reasoning` (Kapitel 5.12).
- `read_stage_skip_requests(status="")` - Main-CEO liest offene Anfragen.
- `decide_stage_skip_request(request_id, decision, reasoning)` -
  `decision` ∈ `approved`/`rejected`. Genehmigt nur, wenn das Überspringen
  tatsächlich zutrifft (z.B. Recherche ist für diese konkrete Frage
  wirklich nicht relevant) - kein Routine-Abnicken; sonst zurück zu den
  früheren Stages. Nicht erneut entscheidbar.

Eine genehmigte Anfrage erlaubt genau einen `write_hypothesis`-Aufruf, der
`evidence_stage` für genau diese `hypothesis_id`/`target_stage`-Kombination
ohne Artefakte setzt (Kapitel 5.9) - keine allgemeine Ausnahme für die
Hypothese insgesamt.

**Korrigierte Lücke (2026-08-13):** `main_ceo_agent` hatte
`read_stage_skip_requests`/`decide_stage_skip_request` seit dem
Structural-Rebuild-Addendum im eigenen Tool-Set, und die Backstory
beschrieb die Zuständigkeit - aber `task_main_ceo_review`s tatsächliche,
nummerierte Checkliste rief keins von beiden je auf. Eine offene
Stage-Skip-Anfrage konnte damit unbegrenzt liegen bleiben, ohne dass ein
operativer Schritt sie je gelesen/entschieden hätte - derselbe Fehlertyp
wie der bereits bekannte `complete_task_order`-Vorfall (ein Tool
vorhanden, eine Backstory, die es beschreibt, aber kein Task-Schritt, der
es tatsächlich aufruft). Jetzt als eigener Schritt 6.5 in
`task_main_ceo_review` verdrahtet: jeden Zyklus offene Anfragen lesen,
standardmäßig ablehnen (zurück zu den früheren Stages), nur bei
tatsächlich zutreffender, hypothesenspezifischer Begründung genehmigen.

### 7.4 FIX.md - Autonome Diagnose

Bisher konnte eine Subsidiary unbemerkt feststecken oder wiederholt am
gleichen Problem scheitern, ohne dass irgendetwas das automatisch
aufschreibt - der Trajektorie-Check (Kapitel 7.1) erfasst nur das
"Zero-Builds"-Muster. `FIX.md` ist ein zweistufiger Mechanismus: billige,
deterministische Zyklus-Checks ohne LLM-Aufruf, die bei Auslösung genau
einen eskalierten Diagnose-Aufruf auf einem stärkeren Modell nach sich
ziehen.

**Stufe 1 - sechs deterministische Checks, jeden Zyklus, kein LLM
(`holding.run_fix_checks`, aufgerufen aus `crew.py`s Cron-Loop nach
`crew.kickoff()`, nicht innerhalb eines CrewAI-Tasks):**

| Check | Erkennt | Vorgeschlagene Schwelle |
|---|---|---|
| `zero_state_streak` | keine neue Hypothese/kein neuer `knowledge_base`-Eintrag/Content-Draft/Task-Order-Fortschritt über N aufeinanderfolgende Zyklen | 3 Zyklen (braucht real 4 Aufrufe, da der erste nur die Baseline setzt) |
| `recurring_malformed_tool_calls` | dieselbe `_malformed_tool_calls`-Signatur (Kapitel 9.6) über mehrere Zyklen hinweg, nicht ein Einzelfall | 3 Zyklen |
| `channel_bury_streak` | N aufeinanderfolgende `bury`-Outcomes auf demselben Channel - echtes Channel-Fit-Problem statt Pech | 3 in Folge |
| `hypothesis_stuck_past_cap` | eine `status='active'`-Hypothese über der bestätigten Zeitbox-Obergrenze (Kapitel 5.13), nur relevant sobald `status='confirmed'` | - |
| `repeated_pivot_streak` | die letzten N aufgelösten Hypothesen resolven allesamt zu `outcome='pivot'` - dieses Repo hat keinen echten "Pivot-Cap"-Zähler, das ist der treue Real-Daten-Proxy für das, was das Addendum "wiederholte Pivot-Cap-Erschöpfung" nennt | 2 in Folge |
| `stale_approvals` | eine `status='pending'`-Freigabe, die ungewöhnlich lange unbeantwortet in `approval_queue.jsonl` liegt | 48 Stunden |

`holding.DEFAULT_PROPOSED_FIX_THRESHOLDS` markiert diese Werte als
`status='proposed'` - anders als die Zeitbox-Policy (Kapitel 5.13) gilt
hier aber **kein** "inaktiv bis bestätigt": die Checks laufen sofort gegen
die vorgeschlagenen Standardwerte, weil sie nur einen diagnostischen
Eintrag auslösen, nie eine Aktion blockieren - "proposed" bedeutet hier
"bitte bestätigen/anpassen", nicht "noch nicht scharf". Anpassbar per
Telegram: `fix_thresholds: confirm` oder `fix_thresholds: <zero_state_
streak_cycles> <malformed_tool_calls_cycles> <channel_bury_streak>
<repeated_pivot_streak> <stale_approval_hours>`.

**Stufe 2 - genau ein eskalierter Diagnose-Aufruf, nur wenn ein Check
tatsächlich auslöst (`crew.generate_fix_diagnosis`):** läuft auf
`claude-opus-5` statt dem laufenden `AGENT_PROFILE`-Modell (Kapitel 9.8) -
ein eigenständiger `crewai.LLM(...)`-Aufruf **außerhalb** der CrewAI-Task-
/Agent-Maschinerie, da dieser Mechanismus im plain-Python-Cron-Loop läuft,
nicht in einem Task. Der Prompt verlangt ausschließlich die real vom
Check-1-Aufruf gesammelte Evidenz (nie erfundene Details), eine
Kategorisierung als `technisch` oder `inhaltlich` (beide gleich wichtig -
das deckt Crashes/Code-Bugs genauso ab wie "das bewegt sich nicht Richtung
Umsatz"-Befunde), einen ehrlichen Confidence-Hinweis als erste Zeile, eine
konkrete Problem-Beschreibung, nummerierte Fix-Schritte und einen Hinweis
zur nötigen Test-Abdeckung - dieselbe Ehrlichkeitspflicht wie bei jedem
anderen Befund in diesem System, nie eine strategische Empfehlung
optimistischer darstellen, als die Evidenz hergibt.

**Ablage - eine feste Datei, Append-only, mit Archivierung:**
`STATE_DIR/_holding/FIX.md` (reiner Markdown-Text, nie JSONL) bekommt pro
Fund einen neuen, datierten Abschnitt angehängt (`## [<entry_id>]
<kategorie>: <headline>`), nie überschrieben. Ein strukturiertes
Sidecar-Log (`fix_entries.jsonl`, Kapitel 11.2) trägt dieselben Einträge
maschinenlesbar für Dedup/Archivierung. `fix_resolved: <entry_id>` per
Telegram markiert einen Eintrag als gelöst, verschiebt seinen Abschnitt
aus dem lebenden `FIX.md` in eine datierte `FIX_resolved_<datum>.md` und
hält `FIX.md` so schlank statt endlos wachsend.

**Abruf durch Claude Code selbst.** `FIX.md` liegt auf dem Railway-Volume,
nicht im lokalen Git-Repo - dieses Repos eigene `CLAUDE.md` (Abschnitt
"Ohne Rückfrage erlaubt") enthält jetzt eine Standing-Instruction: bei
einer Bitte, an `FIX.md` zu arbeiten, zuerst den echten, aktuellen Inhalt
per `railway run -- cat /data/_holding/FIX.md` (bzw. per `STATE_DIR`-
Umgebungsvariable aufgelöst) live holen, nie eine lokale Kopie annehmen -
es gibt keine. Jeder gefundene Abschnitt wird als eigene, addendum-artige
Aufgabe bearbeitet.

**Harte Leitplanke:** Dieser gesamte Mechanismus schreibt ausschließlich
nach `FIX.md`, `fix_entries.jsonl` und einem kurzen Telegram-Hinweis
(Kapitel 8.2) - es gibt keinen Codepfad von einem generierten Befund zu
einer tatsächlichen System-/Code-Änderung. Ein Mensch muss `FIX.md`
explizit in einer separaten Claude-Code-Session umsetzen lassen; der
Mechanismus selbst wendet nie etwas an.

### 7.5 Kaizen - Konsolidierter Selbstverbesserungs-Report

Eine laufende, kleinteiligere Reflexion, unabhängig davon, ob technisch
etwas kaputt ist - `FIX.md` (Kapitel 7.4) bleibt für "etwas steckt
tatsächlich fest oder scheitert wiederholt"; Kaizen läuft **jeden** echten
Zyklus. Überschneidet sich ein Kaizen-Punkt tatsächlich mit einer der
`FIX.md`-Schwellen, gehört der Befund dorthin, nicht doppelt in beide
Kanäle.

**Ein einziger, konsolidierter Report pro Zyklus - nicht einer pro
Agent.** `task_ceo` sammelt subsidiary-eigene Beobachtungen, jede
verankert in echten Zyklusdaten (eine konkrete Hypothesen-ID mit ihrem
realen Outcome, ein konkreter Channel mit realen Zahlen, eine konkret
abgelehnte oder unbeantwortete Freigabe) und legt sie als `kaizen_points`
auf `file_status_report` (Kapitel 7) ab - nie als eigenen, separaten
Kaizen-Report. `task_main_ceo_review` liest diese pro aktiver Subsidiary
und ruft genau **ein** `holding.file_kaizen_report(subsidiary_id=...,
kaizen_report=...)` auf, das sie mit den eigenen holdingweiten
Beobachtungen zu einem kombinierten Report verschmilzt - der einzige Ort
im gesamten Zyklus-Report, an dem Kaizen-Inhalt erscheint.

**Zwei Buckets:**

- **`selbst_umsetzbar`** - klein genug, um noch im selben Zyklus mit
  vorhandenen Tools umgesetzt zu werden (`status='acted'`), oder explizit
  zurückgestellt mit echtem Grund (`status='deferred'` +
  `deferred_reason`) - nie ein stiller No-op.
- **`fuer_aufsichtsrat`** - braucht Jan/Board (Policy-Änderung,
  Budget-/Spend-/Publish-/Deploy-/Pricing-/Legal-Entscheidung, alles über
  die bestehende Tier-1/2-Grenze hinaus). Persistiert in
  `kaizen_suggestions.jsonl` (Kapitel 11.2) **jeden** Zyklus, in dem
  solche Einträge auftreten - unabhängig davon, was per Telegram gezeigt
  wird.

**Zwei Leitplanken, im Code erzwungen, nicht nur per Instruktion:**

1. **Grounding-Pflicht.** Jeder Punkt, beide Buckets, muss ein `grounding`-
   Feld mitbringen, das tatsächlich als echte ID in dieser Subsidiary's
   aktuellen `hypotheses.jsonl`/`channels.jsonl` oder der globalen
   `approval_queue.jsonl` existiert - `holding._kaizen_grounding_exists`
   prüft das direkt gegen den echten Datenbestand, nicht nur auf
   Nicht-Leerheit. Generische Startup-Ratschläge ohne zitierten Fakt werden
   mechanisch abgelehnt, dieselbe Disziplin wie der Anti-Copying-Tripwire
   (Kapitel 5.12), nur gegen echten Zustand statt eine feste Phrasenliste
   geprüft.
2. **Tier-0-Grenze.** Ein `selbst_umsetzbar`-Eintrag wird abgelehnt, sobald
   sein `action`-Text spend/publish/deploy/pricing/legal in irgendeiner
   Form erwähnt (`holding._kaizen_tier_violation`, Stichwortliste
   angelehnt an `tools.APPROVAL_CATEGORIES`) - alles, was diese Kategorien
   berührt, gehört ausschließlich in `fuer_aufsichtsrat` und muss regulär
   durch `request_approval`. Kaizen darf nie zur Hintertür um die
   bestehende Freigabe-Grenze werden.

**Telegram-Dedup** folgt exakt demselben Kurzhinweis-Muster wie `FIX.md`
(Kapitel 7.4/8.2): nur die Anzahl neuer, noch nicht angezeigter
`fuer_aufsichtsrat`-Einträge, nie der volle Text jeden Zyklus erneut.

---

## 8. Mensch im Loop: Freigabe-Queue und Telegram-Fernsteuerung

### 8.1 Freigabe-Queue (`approval_queue.jsonl`)

Jede Aktion mit Kosten, rechtlicher Verpflichtung oder Öffentlichkeitswirkung
läuft über `request_approval(category, proposal, reasoning)`.
`category` ∈ `{spend, legal, publish, deploy, pricing}`. Der Eintrag landet
mit `status='pending'` in der Queue und wird **nie** vom System selbst
ausgeführt.

**`category='publish'` verlangt ein starres Template, keine Freitext-Prosa**
(Structural-Rebuild-Addendum, Kapitel 15): `proposal` muss ein JSON-String
mit exakt diesen Feldern sein - `platform`, `target_url`, `title` (oder
wörtlich `"kein Titel"`), `text` (wörtlicher Inhalt, exakt wie gepostet -
nie paraphrasiert), `footer` (oder wörtlich `"keiner"`), `hypothesis_id`,
`evidence_stage`, `is_experiment` (bool), `success_criterion` (konkret und
falsifizierbar, auch wenn die ehrliche Antwort "nein, reine Recherche, kein
Erfolgskriterium nötig" ist - nie weggelassen). `request_approval` lehnt
alles andere für diese Kategorie ab; `notify_new_pending_approvals` rendert
es über `_format_publish_proposal` mit den exakten deutschen Feld-Labels,
nie zu Prosa umgeflossen - zusätzliche Begründung steht als eigene Zeile
klar getrennt darunter.

**`category='publish'` wird automatisch dedupliziert, zwei Schichten**
(Duplicate-Approval-Addendum) - konfirmiert nötig, nachdem in der Praxis
mehrfach nahezu identische Freigabe-Anfragen für dieselbe Hypothese
gleichzeitig in der Queue standen:

a) **Innerhalb der offenen Queue** (`_find_duplicate_publish_approval`):
   existiert bereits ein Eintrag mit derselben `hypothesis_id`+`platform`
   und ähnlichem `text` (normalisierter Ähnlichkeitsvergleich,
   `PUBLISH_DEDUP_SIMILARITY_THRESHOLD = 0.85`, keine Byte-Identität
   nötig) - noch `pending`, oder innerhalb der letzten
   `PUBLISH_DEDUP_RECENT_DECISION_HOURS = 24`h entschieden - wird der neue
   Antrag nicht angelegt, sondern mit `{"skipped": true, "duplicate_of":
   <id>}` beantwortet.
b) **Gegen die gesamte, tatsächlich gepostete Historie**
   (`_find_similar_posted_content`): Abgleich gegen jeden
   `content_drafts.jsonl`-Eintrag mit `status='posted'` - also wirklich
   von einem Menschen bestätigt live, nicht nur genehmigt - über die
   **gesamte** Historie dieser Subsidiary, kein Zeitfenster. Ähnlicher
   Inhalt, **derselbe** Platform+Community: blockiert genauso wie (a).
   Ähnlicher Inhalt, **andere** Community (Cross-Forum-Muster - derselbe
   Text an mehrere Foren, genau das Signal, das Plattform-eigene
   Anti-Spam-Systeme beobachten und das dem 90/10-Prinzip widerspricht,
   Kapitel 6.3): **nicht** blockiert, aber als `similar_prior_posts`-Feld
   am Freigabe-Eintrag gespeichert und in der Telegram-Benachrichtigung
   klar als Warnung angezeigt, damit ein Mensch das Cross-Forum-Muster
   vor der Entscheidung sieht, nicht erst danach.

Ein Mensch entscheidet über:

```bash
python approve.py                       # offene Anfragen auflisten
python approve.py approve appr_ab12cd34
python approve.py reject appr_ab12cd34 [grund]
```

Vollständige Nutzungshinweise (was jede `category` konkret bedeutet, worauf
vor einer Genehmigung zu achten ist, und wie sich Telegram-Freigaben zu
diesem CLI verhalten - dieselbe `approval_queue.jsonl`, kein zweiter
Mechanismus) stehen als Docstring direkt am Anfang von `approve.py`, nicht
hier dupliziert.

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

**Nachrichten-Splitting an echten Grenzen, nicht mitten im Wort**
(Truncation-Fix-Addendum). Bestätigte reale Ursache für abgeschnittene
Reports ("Main-CEO mitten im Wort, Dev mitten im Satz"): **nicht** ein zu
knappes Modell-Token-Budget, sondern ein hartes `text[:2500]`/`[:1000]`/
`[:400]`-Slicing auf die rohen Task-Outputs, direkt vor dem Telegram-
Versand - unabhängig sowohl vom echten `max_tokens`-Limit als auch von
Telegrams eigenem 4096-Zeichen-Limit (`TELEGRAM_MAX_MESSAGE_LENGTH`).
Ersetzt durch: `TASK_SUMMARY_MAX_CHARS = 6000` als großzügigeres
Render-Budget pro Task-Abschnitt (bei einem echten Ausreißer darüber
hinaus sichtbar mit `[... gekuerzt ...]` markiert, nie stillschweigend
abgeschnitten), plus `_split_message_at_boundaries` (`tools.py`): jede
Nachricht über `TELEGRAM_SAFE_CHUNK_LENGTH = 4000` Zeichen wird an einer
echten Grenze geteilt - zuerst `\n---\n` (Abschnittstrenner), dann
Absatzumbruch, dann Zeilenumbruch, erst als letzter Ausweg ein harter
Schnitt (nur wenn ein einzelner Lauf ganz ohne Grenze länger als das Limit
ist). Mehrteilige Nachrichten werden nummeriert (`label="Business Update"`
→ `"Business Update (1/3)"`), damit klar ist, ob man den ganzen Report
gesehen hat. Gilt für jeden Sendeweg - Message A, Message B, und die
einzelnen Freigabe-Benachrichtigungen.

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
| `duration_policy: confirm` | Vorgeschlagene `max_duration_days_by_stage`-Werte unverändert bestätigen (Kapitel 5.13) |
| `duration_policy: <research> <community_engagement> <landing_page> <build>` | Eigene Werte setzen und in einem Schritt bestätigen (Tage, `none` für kein Limit, Kapitel 5.13) |
| `stagnation_ack: <subsidiary_id>` | Offene Stagnation-Eskalation quittieren (Kapitel 7.1) - setzt `stagnation_escalated=false` und den Zähler zurück; kein Effekt, wenn gerade keine offene Eskalation für diese Subsidiary existiert |
| `fix_resolved: <entry_id>` | `FIX.md`-Eintrag als gelöst markieren, Abschnitt in `FIX_resolved_<datum>.md` archivieren (Kapitel 7.4) |
| `fix_thresholds: confirm` | Vorgeschlagene FIX.md-Check-Schwellen unverändert bestätigen (Kapitel 7.4) |
| `fix_thresholds: <zero_state> <malformed> <bury_streak> <pivot_streak> <stale_hours>` | Eigene Schwellenwerte setzen und in einem Schritt bestätigen (Kapitel 7.4) |

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
    "testing": { "model": "claude-haiku-4-5", "cycle_token_budget": 250000, ... },
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
| `growth` | 4500 / 9 / 120s | 3000 / 30 / 600s |
| `dev` | 10000 / 6 / 90s | 8000 / 15 / 300s |
| `sub_ceo` (`ceo_agent`) | 4000 / 15 / 240s | 8000 / 50 / 900s |
| `main_ceo` | 2500 / 6 / 120s | 4000 / 25 / 600s |

`cycle_token_budget`: `testing` = 250.000, `normal` = 1.000.000 Tokens pro
Zyklus insgesamt. Die `testing`-Werte wurden 2026-08-12 nochmal um das
5-Fache angehoben (Token-Starvation-Addendum, Schritt 3) - reale Evidenz
für abgeschnittene Posts/Zwischenschritte und Growth, das `max_iter`
erreichte, ohne die Aufgabe fertigzustellen; unten stehende
Log-Begründung bezieht sich auf die noch frühere erste Anhebung.

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

USD pro Million Tokens (`pricing.py::PRICING_TABLE`):

| Modell | Base-Input | Cache-Write (5m) | Cache-Write (1h) | Cache-Hit/Read | Output |
|---|---|---|---|---|---|
| `claude-haiku-4-5` | 1,00 | 1,25 | 2,00 | 0,10 | 5,00 |
| `claude-sonnet-5` (vor 2026-09-01) | 2,00 | 2,50 | 4,00 | 0,20 | 10,00 |
| `claude-sonnet-5` (ab 2026-09-01) | 3,00 | 3,75 | 6,00 | 0,30 | 15,00 |

**Report-Aufbau: eine einzige Telegram-Nachricht** (Structural-Rebuild-
Addendum, Kapitel 15 - die vorherige zweite "formatierte Kosten-Tabelle"-
Nachricht war vollständig redundant zu `_usage_headline()`/
`_usage_detail_line()` in der Hauptnachricht, dieselben Zahlen nur
retabelliert; Wegfall verliert keine Information und entfernt zugleich ein
echtes Formatierungsfehler-Risiko - eine Prosa-Nachricht mit
Hypothesen-IDs/Sonderzeichen unter `parse_mode="Markdown"` zu senden hätte
genau das Risiko zurückgeholt, das die alte Zwei-Nachrichten-Trennung
eigentlich vermeiden sollte). `_usage_headline()` steht als eigene,
unmissverständliche Zeile (`Gesamt-Tokens diesen Zyklus: X (Y%
Zyklus-Budget) - Kosten: $Z`) direkt am Anfang, gefolgt von der neuen
**Hypothesen-Übersicht** (`build_hypothesis_overview()`/
`_format_hypothesis_overview()`, Kapitel 5.9-nah) - pro aktiver Hypothese
ID, `evidence_stage`, eine Statuszeile, der letzte geloggte Research-Fund
(oder "keine Erkenntnis geloggt"), der nächste konkrete Schritt (älteste
offene Task-Order, oder "keine offene Task-Order") - lesbar für sich
allein, ohne jeden Agenten-Report einzeln durchzugehen. Danach Warnungen/
Telegram-Kommando-Logs, `_usage_detail_line()` (Agent-Profil/Modell,
Prompt-/Completion-/Cache-Aufteilung, `max_tokens` pro Agent), die
Pro-Agent-Abschnitte (Channel-Strategie/Wachstum/Sub-CEO/Main-CEO/Dev,
weiterhin gekürzt), und ganz am Ende optional **"Für den
Aufsichtsrat"** (`_aufsichtsrat_lines()`) - erscheint nur bei mindestens
einem von drei konkreten Auslösern: offene Freigaben in der Queue, eine
noch nicht bestätigte `max_duration_days_by_stage`-Policy (Kapitel 5.13),
oder offene Stage-Skip-Anfragen (Kapitel 7.3) - sonst komplett
weggelassen, kein Pflichtabschnitt.

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

### 9.8 Modell-Eskalation für FIX.md (`claude-opus-5`)

Einzige Ausnahme vom `AGENT_PROFILE`-Modell (Kapitel 9.1): `crew.fix_llm`
ist eine eigene, fünfte `crewai.LLM(model="anthropic/claude-opus-5", ...)`-
Instanz, ausschließlich für `crew.generate_fix_diagnosis` (Kapitel 7.4) -
nie über `_ANTHROPIC_KWARGS` geroutet, damit ein `agent_profile.json`-
Wechsel für die vier Routine-Agenten diesen Aufruf nie versehentlich
mitverändert. Feuert selten (nur wenn ein FIX.md-Check tatsächlich
auslöst), daher ist der Mehrpreis eines stärkeren Modells für diesen einen
Aufruf begrenzt und lohnt sich für die Qualität des Befunds.

**Korrektur gegenüber dem ursprünglichen Addendum-Text:** Das Addendum
nannte `claude-opus-4-8` - ein echtes, aber laut Anthropics eigener
Modell-Dokumentation inzwischen als "Legacy" geführtes Modell, zum
**identischen** Preis wie das aktuelle `claude-opus-5` ($5 Input / $6.25
5-Min-Cache-Write / $10 1-Std-Cache-Write / $0.50 Cache-Hit / $25 Output,
je MTok - gegen Anthropics eigene Preis-Dokumentation bestätigt am
2026-08-11, `pricing.py`). Da beide Modelle gleich viel kosten und
`claude-opus-5` das aktuelle, nicht-superseded Modell ist, wurde
`claude-opus-5` verwendet statt des im Addendum-Text genannten Legacy-
Modells.

`pricing.PRICING_TABLE["claude-opus-5"]` trägt diese Rate ein, damit der
Zyklus-Kosten-Report (Kapitel 9.6) einen FIX.md-Diagnose-Aufruf sichtbar
mitrechnet statt ihn als versteckte Ausgabe zu behandeln.

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
aufgerufen (`crewai_patches.apply_patches()`, ganz oben in `crew.py`, noch
vor dem `from tools import ...`). Aktuell **zwei** Patches, beide defensiv
implementiert: ändert sich crewais interne Struktur, überspringt
`apply_patches()` den betroffenen Patch mit einer Log-Warnung statt beim
Import zu crashen.

**1) `_patch_max_iterations_final_answer_role`** - wenn ein Agent `max_iter`
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
auch nach dem Patch (Rolle ist `"user"`).

**2) `_patch_disable_strict_tool_schemas`** - `convert_tools_to_openai_
schema()` bäckt unbedingt `"strict": True` in jedes Tool-Schema, ohne
Opt-out pro Tool/Agent. Anthropics natives Tool-Use erzwingt einen harten
Deckel von 20 "strict"-Tools pro Request und lehnt den gesamten Aufruf mit
einem 400 ab, sobald ein einzelner Agent 21+ Tools hat ("Too many strict
tools (21). The maximum number of strict tools supported is 20.") - kein
Randfall, sondern eine Schwelle, die dieses Repos eigene Agenten mit der
Zeit zwangsläufig überschreiten (in Produktion reproduziert: `ceo_agent`
überschritt 20 Tools, jeder ihm zugewiesene Task scheiterte komplett an
`crew.kickoff()`). Der Patch entfernt das `"strict"`-Flag, nachdem crewai
jedes Tool-Schema gebaut hat, an beiden Referenzstellen
(`crewai.utilities.agent_utils` und `crewai.agents.crew_agent_executor`).
Unproblematisch, weil jedes Tool in diesem Repo seine eigenen Argumente
bereits selbst validiert und einen JSON-Fehler statt eines Absturzes bei
schlechtem Input zurückgibt (siehe `tools.py`/`holding.py`) - Anthropics
providerseitige Strict-Schema-Durchsetzung abzuschalten nimmt hier also kein
echtes Sicherheitsnetz weg, nur eine künstliche Obergrenze für die
Tool-Anzahl pro Agent. `ceo_agent` hat inzwischen 25 Tools (Kapitel 3.3) -
ohne diesen Patch wäre der Zyklus bereits an der 21-Tool-Schwelle
zerbrochen.

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

### 11.1 Subsidiary-Ebene (`tools.py`, pro Subsidiary unter `STATE_DIR/<subsidiary_id>/`)

**Datenisolation auf einem einzigen Railway-Service.** Eine frühere Aussage
in diesem Repo, eine zweite Subsidiary bräuchte einen eigenen Railway-
Service, war eine falsche Annahme - korrigiert. Subsidiary-gebundene
Dateien liegen jetzt unter `STATE_DIR/<subsidiary_id>/...` statt flach
direkt unter `STATE_DIR`; jeder Record trägt zusätzlich ein
`subsidiary_id`-Feld (auto-gesetzt beim Schreiben, nicht vom Aufrufer
mitgegeben). Welche Subsidiary gerade "aktiv" ist, steuert ein
Modul-weiter Kontext in `tools.py` (`set_active_subsidiary(id)`/
`get_active_subsidiary()`), den `crew.py` vor jedem `crew.kickoff()`
für die jeweilige Subsidiary setzt (Kapitel 11.1.2) - kein Threading eines
`subsidiary_id`-Parameters durch jede einzelne Tool-Signatur.

**Migration statt Breaking Change.** Bestehende Daten aus dem alten flachen
Layout (`STATE_DIR/hypotheses.jsonl` usw.) werden nicht gelöscht oder
manuell verschoben, sondern beim ersten Zugriff pro Prozess automatisch
nach `STATE_DIR/api-sentinel/...` kopiert und mit `subsidiary_id=
"api-sentinel"` nachgerüstet (`_migrate_legacy_file_if_needed`, einmal pro
Prozess und Datei gecacht). Die alte Datei bleibt dabei unangetastet liegen
- kein destruktiver Schritt. Für die aktuell laufende Subsidiary
(`api-sentinel`) ändert sich dadurch inhaltlich nichts; `checkup.py` deckt
die Migration mit einem eigenen Test ab (Kapitel 13).

**Nicht subsidiary-gebunden geblieben (bewusste Entscheidung, nicht
übersehen):** `approval_queue.jsonl` bleibt global unter
`STATE_DIR/_global/`, weil `approve.py` direkt gegen einen festen Pfad
läuft und eine einzige, holdingweite Freigabe-Queue operativ sinnvoller ist
als fragmentierte Queues pro Subsidiary. `system_paused.json` und
`telegram_update_offset.txt` bleiben ebenfalls global (`STATE_DIR/_global/`)
- beides ist systemweiter, nicht geschäftsspezifischer Zustand.

| Datei | Inhalt |
|---|---|
| `hypotheses.jsonl` | Alle Hypothesen (aktiv/evaluiert/begraben) inkl. Ökonomie, Outcome, Pivot-Kette |
| `channels.jsonl` | Kanal-Roster (Bullseye) |
| `signups.jsonl` | Echte Signups, aus GitHub Issues synchronisiert |
| `task_orders.jsonl` | Sub-CEO → Growth/Dev Aufträge |
| `content_drafts.jsonl` | Entworfene/geplante/entfernte Community-Posts |
| `research_findings.jsonl` | Research-Evidence-Tier-Einträge |
| `knowledge_base.jsonl` | Distillierte Takeaways pro Thema/Kanal/Taktik (siehe Kapitel 5.7) |
| `usage_history.jsonl` | Token-Nutzung pro Zyklus |
| `last_cycle_note.txt` | Kontinuitätsnotiz für den nächsten Zyklus |

Global, `STATE_DIR/_global/` (nicht subsidiary-gebunden, siehe oben):

| Datei | Inhalt |
|---|---|
| `approval_queue.jsonl` | Menschliche Freigabe-Queue (die *einzige* im ganzen System) |
| `system_paused.json` | Pause-Status (Telegram `stop`/`start`) |
| `telegram_update_offset.txt` | Zuletzt verarbeitetes Telegram-Update |

#### 11.1.2 Bekannte Grenze: statischer Task-Text

`crew.py` durchläuft pro Zyklus alle aktiven Subsidiaries in einer
Schleife, setzt vor jedem `crew.kickoff()` den aktiven Kontext
(`tools.set_active_subsidiary(sub_id)`) und übergibt `subsidiary_id` als
Kickoff-Input (crewai-natives `{subsidiary_id}`-Platzhalter-Interpolation
in den Task-Texten) - kein hartcodiertes `OWN_SUBSIDIARY_ID` und keine
Annahme von genau einer Subsidiary mehr im Code. Heute läuft dabei
effektiv weiterhin nur ein Crew-Durchlauf, weil nur `api-sentinel` aktiv
ist. Offen bleibt: die Task-*Inhalte* selbst (Kanal-Kandidaten,
Freqtrade/CCXT-Framing usw.) sind weiterhin API-Sentinel-spezifischer
Fließtext, nicht pro Subsidiary dynamisch generiert - eine zweite aktive
Subsidiary bekäme heute technisch isolierte Daten, aber inhaltlich noch
dieselben, unpassenden Formulierungen in den Tasks. Bewusst nicht in
diesem Schritt behoben (ein größerer Folgeschritt, kein Blocker für die
Datenisolation selbst) - siehe Kapitel 15.

### 11.2 Holding-Ebene (`holding.py`, `STATE_DIR/_holding/`)

| Datei | Inhalt |
|---|---|
| `subsidiaries.jsonl` | Subsidiary-Register inkl. Policies + Policy-Historie + Stagnation-Zähler (`consecutive_stall_cycles`, `stagnation_escalated`, `stagnation_escalated_at`, Kapitel 7.1) |
| `pivot_proposals.jsonl` | Pivot-Vorschläge der Sub-CEOs |
| `cross_subsidiary_requests.jsonl` | Cross-Subsidiary-Anfragen |
| `status_reports.jsonl` | Sub-CEO → Main-CEO Berichte |
| `strategic_directions.jsonl` | Main-CEO → Sub-CEO Ausrichtungen |
| `ideas.jsonl` | Idee-Intake, jeder Agent schreibt, Main-CEO routet (Kapitel 7.2) |
| `stage_skip_requests.jsonl` | Evidence-Stage-Skip-Anfragen, Sub-CEO reicht ein, Main-CEO entscheidet (Kapitel 7.3) |
| `fix_entries.jsonl` | Strukturiertes Sidecar-Log zu `FIX.md` (id/category/headline/subsidiary_id/check_type/resolved/telegram_notified_at, Kapitel 7.4) |
| `fix_thresholds.jsonl` | Ein einzelner, bestätigter Override-Record für die FIX.md-Check-Schwellen, ersetzt `holding.DEFAULT_PROPOSED_FIX_THRESHOLDS` vollständig sobald per Telegram gesetzt (Kapitel 7.4) |
| `kaizen_suggestions.jsonl` | `fuer_aufsichtsrat`-Einträge aus dem konsolidierten Kaizen-Report (Kapitel 7.5) |

**`FIX.md` / `FIX_resolved_<datum>.md`** liegen ebenfalls unter
`STATE_DIR/_holding/`, sind aber bewusst **reiner Markdown-Text**, keine
JSONL-Datei - append-only, nie überschrieben (Kapitel 7.4). Abruf per
`railway run -- cat /data/_holding/FIX.md` (Kapitel 12.4, `CLAUDE.md`).

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

**Korrigiert 2026-08-13, empirisch gegen die echte Railway-CLI getestet -
die vorherige Fassung dieses Abschnitts war falsch.** `api-sentinel` ist
ein Cron-Job-Service: zwischen den planmäßigen Läufen existiert **kein
laufender Container** (`status: created`). Drei CLI-Wege wurden real
getestet, keiner funktioniert zwischen den Ticks:

- `railway run -- cat /data/...` läuft **lokal** mit injizierten Railway-
  Env-Vars, nicht remote - `/data` existiert auf der lokalen Maschine
  schlicht nicht, das Scheitern hat nichts mit dem Inhalt der Datei zu tun.
- `railway ssh` scheitert explizit mit `"Your service's container is not
  running (status: created)"`.
- `railway service files list/download` (auf den ersten Blick ein
  vielversprechender Kandidat für direkten Volume-Zugriff ohne
  Live-Container) scheitert ebenfalls, mit `Failed to initialize SFTP
  session / Timeout` - nutzt laut Test denselben Live-Container-Mechanismus
  wie `ssh`, keinen direkten Block-Storage-Zugriff.

**Es gibt doch einen manuellen Trigger** - anders als hier vorher
behauptet: `railway restart` (ohne Rebuild) oder `railway redeploy` (bzw.
im Dashboard "Restart"/"Redeploy", oder Cmd+K "Deploy Latest Commit")
starten den Service sofort außerhalb des Cron-Plans. Laut Railway-Doku
gibt es dafür aber keinen separaten "Container nur kurz hochfahren"-
Mechanismus - es ist derselbe Weg wie ein normales Redeploy und führt den
echten Start-Befehl (`python crew.py`) aus: ein vollständiger, echter,
außerplanmäßiger Agenten-Zyklus mit echten Anthropic-API-Kosten und
echten Seiteneffekten (Telegram-Nachrichten, Approval-Einträge), nicht
ein kostenloses "kurz reinschauen".

**Bewusst weder `allow` noch `deny`, jedes Mal einzelne Rückfrage
(Stand 2026-08-13).** Kurze Historie: erst fiel auf, dass
`.claude/settings.json`s bestehende `deny`-Einträge für `railway service
restart`/`railway service redeploy` die funktional identischen
Top-Level-Aliasse `railway restart`/`railway redeploy` gar nicht
abdeckten (eine echte Lücke) - als Sofortmaßnahme wurden dann alle vier
Schreibweisen komplett in `deny` aufgenommen, was aber über das Ziel
hinausschoss: eine `deny`-Regel lässt sich nicht per Rückfrage umgehen,
der Trigger war damit komplett gesperrt statt nur zustimmungspflichtig.
**Korrigiert:** alle vier Schreibweisen wieder aus `deny` entfernt und
bewusst ungelistet gelassen - dieselbe Kategorie wie `railway ssh`: weder
pauschal erlaubt noch pauschal verboten, sondern bei jeder Nutzung eine
echte, einzelne Rückfrage. Volle Historie in
`PERMISSION_REQUESTS_reviewed_2026-08-13.md`. Praktischer Zugriffsweg:
entweder das kurze Zeitfenster eines tatsächlich laufenden planmäßigen
Zyklus abpassen (`railway ssh`/`railway service files` während der
Container aktiv ist), oder - nach expliziter Rückfrage, da echte Kosten
anfallen - bewusst einen Extra-Zyklus per `railway restart` auslösen und
das kurze Fenster direkt danach nutzen. Details, die konkrete Herleitung
und Jans Entscheidung gegen einen dauerhaften Umbau zu einem
Always-on-Worker (der das Problem strukturell lösen würde, aber
durchgehende statt nur nutzungsbasierte Kosten bedeutet): siehe
`CLAUDE.md`s `FIX.md`-Abschnitt und `OPERATING_MODEL.md` Kapitel 6.

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
- Stand zuletzt: **389 Tests, 389/389 bestanden, konsistent über mehrere
  volle Läufe** (2026-08-13). `test_read_kaizen_actions_and_suggestions_
  filter_by_since` war ein echter, reproduzierbarer Timing-Flake (zwei
  `datetime.now()`-Zeitstempel dicht beieinander - ~3 von 8 Läufen
  schlugen fehl, real gemessen; bestätigt bereits auf einem Commit vor
  dieser Session vorhanden, also keine Regression aus einer der jüngeren
  Änderungen). **Behoben:** der Test pinnt jetzt die `created_at`-Werte
  seiner beiden Kaizen-Reports über `holding._read`/`_write` auf ein
  deterministisches, monoton steigendes Sequenzfeld (`_monotonic_iso`)
  statt sich auf die reale Wall-Clock-Nähe zweier Schreibvorgänge zu
  verlassen - 10/10 Läufe in genau der zuvor fehlschlagenden Sequenz
  bestätigt deterministisch. Drei weitere Tests
  (`test_search_web_live_real_key_returns_real_results`,
  `test_search_web_then_read_webpage_live_pipeline`,
  `test_payment_propensity_scan_live_reddit_algotrading`) sind echte
  Live-Smoke-Tests gegen die reale Serper.dev-API - sie überspringen sich
  selbst sauber (kein Fail/Error), wenn `API-Sentinel-serper` nicht gesetzt
  ist, damit die restliche Suite nie von einem echten externen Key/Budget
  abhängt. Lokal ohne Key: alle drei übersprungen. Per `railway run` mit
  dem echten Key: alle Tests bestanden, alle drei Live-Tests
  tatsächlich ausgeführt (Real-Serper-Key-Addendum, zuletzt erneut bestätigt
  im FIX.md/Kaizen/Payment-Propensity-Addendum mit dem echten
  r/algotrading-Scan aus Kapitel 5.14). Der eskalierte `claude-opus-5`-
  Aufruf (Kapitel 9.8) selbst wird **nie** in `checkup.py` real ausgeführt
  - dieselbe Disziplin wie beim Verzicht auf `crew.kickoff()` oben, echte
  Anthropic-Kosten werden hier nie automatisch verursacht; stattdessen prüft
  `test_fix_llm_uses_opus_5_and_is_independent_of_agent_profile` die reale
  Verdrahtung strukturell (Modell-String, Objekt-Identität, Pricing-Tabelle)
  ohne den Aufruf selbst zu tätigen, und
  `test_generate_fix_diagnosis_parses_structured_response`/`_falls_back_
  when_call_fails` decken die Parsing-/Fallback-Logik über einen
  injizierten `llm_call`-Fake ab.
- `test_all_task_descriptions_and_agent_backstories_interpolate_cleanly`
  (Crash-Fix-Addendum, siehe Kapitel 15) ruft `crew.crew._interpolate_inputs(...)`
  - crewais echten internen Mechanismus, den `kickoff()` vor jedem
  Agenten-Lauf aufruft - direkt gegen das echte, produktive `Crew`-Objekt
  auf, mit denselben Inputs wie `crew.py`s `__main__`. Kein Mock: dieser
  Test hätte den echten 2026-08-11-Absturz (ein literales `{n}` in
  `task_dev`s Beschreibung) tatsächlich gefangen - direkt verifiziert,
  indem der Test vor dem Fix lief und mit exakt derselben Fehlermeldung
  fehlschlug wie in Produktion.

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
| `API-Sentinel-serper` | Nein | Zugriff auf `search_web` (Serper.dev, Kapitel 5.11); ohne Key liefert das Tool einen klaren Fehlertext statt eines stillen Fehlschlags - `read_webpage` braucht keinen Key. **Name weicht bewusst von der sonstigen `UPPER_SNAKE_CASE`-Konvention ab** (Bindestriche, gemischte Groß-/Kleinschreibung) - das ist die exakte Bezeichnung, die tatsächlich in Railway angelegt wurde, kein Code-Stilfehler; `os.environ.get(...)` matcht exakte Strings unabhängig vom Namensschema, also funktioniert es, aber der Name wurde bewusst nicht "aufgeräumt", weil das die echte Railway-Variable brechen würde. Serper.dev: ~2.500 kostenlose Anfragen, danach kostenpflichtig - real gegen den echten Key getestet (Real-Serper-Key-Addendum), siehe Kapitel 15. |
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
  ein `build` rechtfertigen). Erste Korrektur: das Pflichtfeld
  `build_cost_reasoning`, `SIMPLE_BUILD_COST_CEILING` (10 USD) mit
  Substanz-Anforderung darüber, und ein auf 1 Monat gesenkter
  Standard-`break_even_horizon_months` (Kapitel 5.1), plus ein einmaliger
  Rekalibrierungs-Schritt in `task_ceo`. **Update (Structural-Rebuild-
  Addendum):** Direkt im letzten echten Zyklus gefunden - `hyp_bootstrap_001`s
  `build_cost_reasoning` erwies sich zusätzlich als nahezu wortgleiche
  Kopie von Instruktionstext dieses Repos, nicht als eigenständig
  hergeleitete Begründung. Das allein disqualifiziert alles darauf
  Aufgebaute. Statt einer weiteren Rekalibrierung wird die Hypothese jetzt
  über einen einmaligen `task_ceo`-Schritt (0.5) via `write_hypothesis`
  **begraben** (`status='buried'`) - mit der konkreten Sequenz als
  `bury_reasoning` (Ökonomie vor jeder Recherche berechnet, keine
  Research-Findings für diese Hypothese, kopierter `build_cost_reasoning`-
  Text), nicht mit einem vagen Label. Da das System während der gesamten
  Session pausiert war (Telegram `stop`), hatte dieser Schritt zum
  Zeitpunkt dieses Commits noch keine Gelegenheit zu laufen - Claude Code
  hat die STATE_DIR-Datei bewusst nicht selbst editiert, siehe das
  Präzedenzmuster der Rekalibrierung oben. Das zugrunde liegende Problem/
  die Zielgruppe kann sauber neu getestet werden, beginnend bei
  `evidence_stage='research'` mit einem echten Forschungsplan (Kapitel
  5.11), sobald echte Sorgfaltspflicht das rechtfertigt.
- **Kein produktives Monitoring-Produkt existiert noch.** Das System befindet
  sich bewusst in der Hypothesis-Testing-Phase (Kapitel 1/5) - es hat noch
  kein einziges `build`-Outcome durchlaufen.
- **X (Twitter) hat keine kostenlose öffentliche Metrik-API mehr** - jede
  Reichweitenmessung dort braucht entweder eine kostenpflichtige API-Stufe
  (eine `spend`-Freigabe) oder manuell gelieferte `metrics_json`-Werte.
- **`discord_telegram`-Multiplikator in `reach_estimators.json` ist
  ausdrücklich ein unvalidierter Platzhalter** (siehe die `notes` in der
  Datei selbst). **Korrigiert (2026-08-13, Dead-Code-Audit):**
  `update_reach_multiplier` (`scoring.py`) ist entgegen der vorherigen
  Formulierung hier von **keinem Agenten aufrufbar** - kein `@tool`,
  nirgends in `crew.py`s `tools=[...]`-Listen verdrahtet. Eine
  Rekalibrierung ist damit aktuell nur manuell/außerhalb des Systems
  möglich (direktes Editieren von `reach_estimators.json`), nicht etwas,
  das `ceo_agent` selbst tun kann, auch wenn genug Datenpunkte vorlägen.
  Siehe `OPERATING_MODEL.md` Kapitel 5 für den vollständigen
  Dead-Code-Befund.
- **Der `max_iter`-Rollen-Patch (`crewai_patches.py`) ist ein Workaround für
  einen crewai-eigenen Bug, kein Upstream-Fix** - sollte crewai das Verhalten
  irgendwann selbst korrigieren, kann der Patch entfernt werden (er ist
  defensiv geschrieben und würde bei geändertem crewai-Internal einfach
  übersprungen, aber nicht mehr nötig sein).
- **Korrigiert (2026-08-13):** Es gibt doch ein manuelles "jetzt sofort
  einen Zyklus auslösen" (`railway restart`/`railway redeploy`, Kapitel
  12.4) - die vorherige Behauptung hier war falsch. Es ist aber kein
  kostenloser "nur kurz nachschauen"-Mechanismus: es führt den echten
  `python crew.py`-Zyklus mit echten API-Kosten und Seiteneffekten aus.
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
  dem 2h-Deploy nicht mehr reproduzierbar. **Erneut beobachtet (2026-08-13):**
  ein späterer `railway status --json`-Check zeigte wieder zwei
  unterschiedliche Werte in zwei Feldern derselben Antwort (`"0 */6 * * *"`
  service-seitig vs. `"0 */2 * * *"` in den Deployment-Metadaten) - noch
  nicht abschließend reproduzierbar/geklärt, siehe `OPERATING_MODEL.md`
  Kapitel 6 für den aktuellen Stand; ein direkter Dashboard-Check bleibt der
  zuverlässigste Weg, das zu klären.
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
  Report-Format-Rewrite selbst war zu diesem Zeitpunkt weiterhin nicht
  umgesetzt - **Update:** im nachfolgenden Structural-Rebuild-Addendum
  (siehe unten) nachgeholt (Kapitel 9.6).
- **Historie: Pricing-/Ökonomie-Isolation zwischen Subsidiaries war
  strukturell nicht gegeben, nur weil sie noch nie gebraucht wurde** -
  `tools.py` hatte genau ein modulweites `STATE_DIR` ohne `subsidiary_id`-
  Feld, `crew.py` verdrahtete genau eine `Crew` fest auf `api-sentinel`;
  eine zweite operative Subsidiary hätte faktisch in dieselbe
  `hypotheses.jsonl` geschrieben - ein echter Kollisionspfad, keine bloß
  ungetestete Annahme. **Update (Real-Research/Multi-Subsidiary-Addendum):**
  behoben - `subsidiary_id` auf jedem subsidiary-gebundenen Record,
  per-Subsidiary `STATE_DIR`-Layout, kein hartcodierter Single-Crew-
  Aufbau mehr (Kapitel 11.1). Weiterhin offen: die Task-*Inhalte* selbst
  sind noch nicht pro Subsidiary dynamisch (Kapitel 11.1.2), und der
  eigentliche Multi-Subsidiary-Betrieb (mehrere aktive Subsidiaries
  gleichzeitig durchlaufen lassen) ist als kleinerer Folgeschritt bewusst
  noch nicht angegangen - heute läuft weiterhin effektiv nur
  `api-sentinel`.
- **Reddit-`.json`-Auto-Fetch ist ein live bestätigtes, laufend zu
  überwachendes Risiko, kein gelöstes Problem.** Siehe Kapitel 6.2 für den
  Befund (`403 Client Error: Blocked` bei einem echten Testabruf) und
  Kapitel 8.1/Tabelle zu `read_channel_metrics` für den sauberen
  Fallback-Pfad, der einen Absturz verhindert.
- **Real-Research/Multi-Subsidiary/Health-Check-Addendum:** drei
  unabhängige Ergänzungen in einem Schritt. (1) `search_web`/`read_webpage`
  (Serper.dev + BeautifulSoup) geben `ceo_agent`/`growth_agent`/
  `main_ceo_agent` echte, passive Web-Recherche - vorher war das
  Forschungsplan-/Artefakt-Gate aus Kapitel 5.11 für ein wirklich neues
  Thema unerfüllbar, weil `own_question_post_replies` strukturell erst nach
  `community_engagement` erreichbar ist (Kapitel 5.11 dokumentiert die
  Auflösung explizit). Drei Optionen echt geprüft (nicht angenommen):
  `crewai_tools`-Bordmittel (installiert, aber nicht in `requirements.txt`,
  `WebsiteSearchTool` bräuchte zusätzlich RAG/Embeddings), Anthropics
  natives `web_search`-Servertool (im crewai-Anthropic-Quellcode geprüft:
  die Tool-Konvertierung akzeptiert kein rohes `web_search_20250305`-Dict
  ohne `input_schema`), gewählt wurde eine schlanke Eigenimplementierung
  gegen Serper.dev. (2) Multi-Subsidiary-Datenisolation auf einem
  einzigen Railway-Service (Kapitel 11.1) - Migration, kein Breaking
  Change, `api-sentinel` lief nach der Umstellung unverändert weiter.
  (3) Der Gesundheits-Check hat jetzt Zähne: `consecutive_stall_cycles` +
  `STAGNATION_ESCALATION_THRESHOLD` (6 Zyklen) eskalieren in einen
  persistenten "Für den Aufsichtsrat"-Eintrag, der erst per
  `stagnation_ack` verschwindet, nie automatisch (Kapitel 7.1). 14 neue
  `checkup.py`-Tests (273 -> 287).
- **Real-Serper-Key-Addendum:** der oben unter (1) beschriebene
  Web-Recherche-Pfad war zu diesem Zeitpunkt implementiert, aber noch nie
  gegen einen echten Key live getestet - es gab schlicht noch keinen. Nach
  Anlage der Railway-Variable `API-Sentinel-serper` (bewusst dieser exakte,
  von der `UPPER_SNAKE_CASE`-Konvention abweichende Name, Kapitel 14): Code
  auf den echten Variablennamen umgestellt (vorher fälschlich als
  `SERPER_API_KEY` angenommen); direkt gegen die laufende Railway-Umgebung
  verifiziert, dass die Variable nach dem automatischen Redeploy tatsächlich
  zur Laufzeit lesbar ist (`railway run`, nie der Wert selbst in den Chat
  gedruckt); ein echter `search_web`-Aufruf lieferte fünf echte Treffer zu
  einer echten API-Sentinel-relevanten Frage; ein Treffer davon wurde per
  `read_webpage` real gelesen (voller, substanzieller Klartext); ein
  begleitender Test entdeckte dabei, dass Reddit auch normale HTML-Seiten
  gegen nicht-browserartige Zugriffe blockiert (Bot-Verifizierungsseite
  statt Inhalt) - dieselbe Blockade wie beim `.json`-Endpunkt (Kapitel 6.2),
  jetzt für einen zweiten Zugriffsweg live bestätigt; ein echter
  `log_research_finding`-Aufruf mit dem tatsächlich gefundenen Inhalt
  bestand den Anti-Copying-Tripwire und den `evidence_stage='research'`-
  Artefakt-Gate-Check. 2 neue `checkup.py`-Tests (287 -> 289), beide echte
  Live-Smoke-Tests mit sauberem Self-Skip ohne Key (Kapitel 13).
- **Crash-Fix-Addendum (2026-08-11): der Zyklus schlug an zwei
  aufeinanderfolgenden echten Läufen (18:04 und 20:02 UTC) mit derselben
  Warnung fehl - nichts lief seit dem letzten Deploy, kein LLM-Aufruf, kein
  Bury, keine Recherche.** Ursache real reproduziert (nicht geraten):
  crewais `Crew._interpolate_inputs()` - dieselbe Methode, die `kickoff()`
  vor jedem Agenten-Lauf aufruft - interpoliert JEDE Task-`description`
  UND jede Agent-`role`/`goal`/`backstory` gegen dieselben Inputs
  (`{"subsidiary_id": ...}`), bevor irgendein Agent startet. `task_dev`s
  Beschreibung enthielt die wörtliche Beispiel-Namensregel
  `lp_v{n}_{label}.html` - `{n}` matcht crewais Platzhalter-Regex
  (`\{[A-Za-z_][A-Za-z0-9_-]*\}`), `n` existiert aber nirgends in den
  Inputs, also `KeyError` -> `ValueError("Missing required template
  variable 'n' not found in inputs dictionary")`, exakt die Meldung aus
  der Warnung. Lokal reproduziert (`crew.crew._interpolate_inputs(...)`
  gegen den echten, produktiven `Crew`-Aufbau) und nach dem Fix erneut
  gegen dieselbe Reproduktion verifiziert, dass sie nicht mehr auftritt -
  kein bloßes "sollte jetzt gehen". Fix: `lp_v{n}_{label}.html` ->
  `lp_v<N>_<label>.html` (spitze statt geschweifte Klammern - Doppeln der
  geschweiften Klammern, das Python-`.format()`-Konventionen entsprechen
  würde, funktioniert bei crewais eigenem Regex nachweislich NICHT, direkt
  gegen den Quellcode geprüft statt angenommen). Jede andere Task-
  Beschreibung und jede Agent-Backstory wurde auf dasselbe Muster geprüft
  (kein weiterer Treffer) - und ein neuer, echter Regressionstest
  (`test_all_task_descriptions_and_agent_backstories_interpolate_cleanly`,
  Kapitel 13) ruft jetzt denselben echten crewai-Mechanismus in jedem
  `checkup.py`-Lauf auf, sodass diese Fehlerklasse künftig vor dem Deploy
  auffliegt.<br><br>
  Unabhängig davon zwei echte, im Code bestätigte Zusatzbefunde:
  (1) `evidence_stage` wurde für Hypothesen von vor Einführung dieses
  Felds (`hyp_bootstrap_001`) nie zurückgefüllt - `write_hypothesis`s
  Merge-Logik überschreibt bei einem Update nur die im Patch enthaltenen
  Felder, kein Default für fehlende Bestandsfelder. Jetzt behoben durch
  eine echte, evidenzbasierte Backfill-Migration
  (`_backfill_missing_evidence_stage_if_needed`, gleiches
  Einmal-pro-Prozess-Muster wie die Subsidiary-Migration, Kapitel 11.1):
  leitet die Stage aus tatsächlich vorhandenen Signalen ab (reale
  Ökonomie/`landing_page_live` -> `landing_page`, ein echtes
  Community-Engagement-Artefakt -> `community_engagement`, sonst der
  sichere Standard `research`) statt zu raten, überschreibt nie eine
  bereits gesetzte gültige Stage. (2) Die "Nächster Schritt"-Zeile im
  Hypothesen-Überblick war tatsächlich naiv - `build_hypothesis_overview()`
  zeigte unbedingt die **älteste** offene Task-Order, unabhängig von
  `evidence_stage` oder Relevanz. Direkt im Code verifiziert, nicht
  angenommen: das erklärt, warum `order_ee8905ab` ("Build landing page
  lp_v1_bootstrap" - die älteste offene Order im gesamten Projekt,
  entstanden lange vor Evidence-Stage-Gating) beharrlich als "nächster
  Schritt" auftauchte. Das ist NICHT allein durch den Absturz erklärt -
  es ist ein eigenständiger, latenter Bug, der bei jeder Order-Anhäufung
  wieder zuschlagen würde, auch ohne Absturz. Fix: zeigt jetzt die
  **neueste** offene Order (die beste verfügbare Ground-Truth-Näherung an
  "aktuell relevant", da Task-Orders selbst keine `evidence_stage`-
  Momentaufnahme tragen) und hängt bei mehreren offenen Orders die Anzahl
  an, statt sie stillschweigend zu verstecken - eine Anhäufung wird damit
  selbst zum sichtbaren Signal. 15 neue `checkup.py`-Tests (289 -> 298: 1
  Templating-Regressionstest, 2 für die Next-Action-Logik, 6 für die
  Evidence-Stage-Migration, plus die bereits gezählten). Zwei
  Nachfolge-Fragen (siehe unten) waren zum Zeitpunkt dieses Commits noch
  offen: warum der Absturz erst jetzt auftrat statt schon während der
  ganzen bisherigen Session, und ob das Begraben einer Hypothese ihre
  offenen Task-Orders wirklich in den Daten schließt oder sie nur aus der
  Anzeige verschwinden lässt.
- **Crash-Fix-Verifikations-Addendum (2026-08-11, direkte Fortsetzung):**
  zwei Nachfolge-Fragen zum obigen Fix, mit echter Evidenz beantwortet,
  nicht angenommen. **(1) Warum jetzt erst?** `git log -S` zeigt: die
  Zeichenkette `lp_v{n}_{label}.html` existiert unverändert seit dem
  allerersten substanziellen Commit dieses Projekts
  (`1d73638`, 2026-08-05) - nicht neu. Aber `crew.kickoff(inputs=...)`
  mit einem tatsächlich befüllten Inputs-Dict (`{"subsidiary_id": ...}`)
  wurde erst mit `8c623ae` (2026-08-10, 09:30 UTC, das Multi-Subsidiary-
  Addendum aus derselben Session) eingeführt - davor lief
  `crew.kickoff()` ganz ohne `inputs`, und crewais eigener
  Interpolationscode bricht früh ab, wenn `inputs` leer ist
  (`if not inputs: return`) - Interpolation lief also in dieser ganzen
  Session vorher **nie**, der Platzhalter lag die ganze Zeit inert im
  Text. `8c623ae` aktivierte Interpolation zum ersten Mal überhaupt im
  Leben dieses Projekts, und genau die beiden nächsten geplanten
  Cron-Läufe danach (18:04, 20:02 UTC, beide nach dem 09:30-Uhr-Deploy)
  waren die ersten beiden, die abstürzten - lückenlos konsistente
  Zeitlinie, kein Widerspruch offen. **(2) Schließt Bury die Orders
  wirklich, oder versteckt es sie nur?** Direkt im Code geprüft: nein,
  vorher nicht wirklich - die einzige Kopplung war eine Freitext-
  Anweisung in `task_ceo`s Bury-Schritt an den Sub-CEO, selbst
  `complete_task_order` für jede offene Order aufzurufen; `ceo_agent`
  hatte `complete_task_order` aber nie in seiner eigenen Tool-Liste
  (`crew.py`, gegen `test_ceo_agent_tools_match_spec` verifiziert) - die
  Anweisung war so, wie geschrieben, gar nicht ausführbar. Jetzt
  mechanisch behoben: `write_hypothesis` schließt beim Setzen von
  `status='buried'` selbst jede noch offene, an diese Hypothese gebundene
  Task-Order (`status='done'`, `result` nennt den Grund) - unabhängig
  davon, welcher Agent oder welche Anweisung das Bury ausgelöst hat, und
  für jedes künftige Bury-Ereignis, nicht nur `hyp_bootstrap_001`. Echt
  mit einem Vorher/Nachher-Check verifiziert (nicht angenommen): eine
  offene Order vor dem Bury-Aufruf, `status='done'` mit einem `hyp_id`-
  und `'buried'`-haltigen `result` danach, bereits erledigte Orders
  bleiben unangetastet, Orders anderer Hypothesen bleiben unangetastet.
  5 neue `checkup.py`-Tests (298 -> 303).
- **Structural-Rebuild-Addendum (Entscheidungs-Framework, Bury Bootstrap,
  Research-Rigor, Reporting, Approvals):** der vollständige, konsolidierte
  Nachfolger der obigen Audit-Addendum-Punkte. Kernbefund:
  `hyp_bootstrap_001`s `build_cost_reasoning` erwies sich als nahezu
  wortgleiche Kopie von Instruktionstext dieses Repos - keine eigenständige
  Reasoning, disqualifiziert alles darauf Aufgebaute unabhängig von der
  konkreten Zahl. Statt einer weiteren Rekalibrierung: Bury (Kapitel 5.1
  oben). Eingeführt: das Two-Way/One-Way-Door-Entscheidungs-Framework
  (Kapitel 4.3, wörtlich in beiden Agenten-Backstories referenziert, nicht
  nur einmalig gelesen); Pflichtfelder jetzt stufenabhängig statt einer
  flachen Checkliste (Kapitel 5.1); ein echtes Evidence-Stage-Gate mit
  Artefakt-Pflicht statt eines selbstunterschriebenen
  `stage_justification`-Strings (Kapitel 5.9, ersetzt die vorherige
  Version vollständig); Main-CEO-Review für Stage-Skips
  (`stage_skip_requests.jsonl`, Kapitel 7.3); ein Forschungsplan mit
  Bestätigungs-/Widerlegungs-Kriterien vor Recherchebeginn plus
  Mindestlänge für Research-Findings (Kapitel 5.11); ein mechanischer
  Anti-Copying-Tripwire gegen wiederverwendeten Instruktionstext (Kapitel
  5.12) - getestet gegen genau die Formulierungsfamilie des tatsächlichen
  Vorfalls; eine Zeitbox-Policy pro Stage als **Vorschlag**, nicht
  hartcodiert, nie stillschweigend aktiv (Kapitel 5.13); ein starres
  Template für `category='publish'`-Freigaben, keine Freitext-Prosa mehr
  (Kapitel 8.1); und endlich der Report-Format-Rewrite, der seit dem
  vorherigen Audit-Addendum offen war - eine einzige Telegram-Nachricht
  mit echter Hypothesen-Übersicht statt zwei redundanter Nachrichten
  (Kapitel 9.6). 43 neue `checkup.py`-Tests (230 -> 273). Wichtige
  Einschränkung: Claude Code hat keinen Zugriff auf die laufende
  Railway-Volume-Instanz - der Bury-Schritt und alle neuen Gates greifen
  erst, wenn der Sub-CEO tatsächlich einen echten Zyklus durchläuft
  (System war während dieser Session per Telegram `stop` pausiert).
- **FIX.md/Kaizen/Payment-Propensity-Addendum:** drei zusammenhängende, aber
  eigenständige Ergänzungen. **FIX.md** (Kapitel 7.4): sechs
  deterministische, LLM-freie Zyklus-Checks (`holding.run_fix_checks`)
  erkennen eine still feststeckende oder wiederholt scheiternde Subsidiary
  - bei Auslösung genau ein eskalierter Diagnose-Aufruf auf `claude-opus-5`
  (Kapitel 9.8, Korrektur gegenüber dem ursprünglich genannten, inzwischen
  als Legacy geführten `claude-opus-4-8` - identischer Preis, aktuelles statt
  superseded Modell), geschrieben als datierter Abschnitt in eine feste,
  append-only `STATE_DIR/_holding/FIX.md` mit strukturiertem Sidecar-Log
  (`fix_entries.jsonl`) für Dedup/Archivierung (`fix_resolved: <id>` per
  Telegram). Harte Leitplanke, mechanisch durch Konstruktion: der Mechanismus
  schreibt ausschließlich nach `FIX.md`/`fix_entries.jsonl`, kein Codepfad zu
  einer tatsächlichen Anwendung führt von dort weiter - `CLAUDE.md` bekam
  eine Standing-Instruction zum Live-Abruf per `railway run -- cat
  /data/_holding/FIX.md`, da die Datei nur auf dem Volume existiert, nie im
  lokalen Repo. **Kaizen** (Kapitel 7.5): ein einziger, konsolidierter
  Selbstverbesserungs-Report pro Zyklus (`holding.file_kaizen_report`,
  aufgerufen von `task_main_ceo_review`, nie separat von `task_ceo`), zwei
  Buckets (`selbst_umsetzbar`/`fuer_aufsichtsrat`) mit zwei im Code
  erzwungenen Leitplanken: jeder Punkt muss eine real existierende
  Hypothesen-/Channel-/Freigabe-ID zitieren (`_kaizen_grounding_exists`
  prüft das gegen den echten Datenbestand, nicht nur Nicht-Leerheit), und
  `selbst_umsetzbar` wird abgelehnt, sobald der Text spend/publish/deploy/
  pricing/legal in irgendeiner Form erwähnt - Kaizen darf nie zur Hintertür
  um die bestehende Freigabe-Grenze werden. **Payment-Propensity** (Kapitel
  5.14, verankert in der Bullseye-Kanalbewertung aus Kapitel 6.1): ein
  Zahlungsbereitschafts-und-Größe-Scan pro Channel (nicht pro Hypothese),
  motiviert durch Jans Beobachtung, dass r/algotrading trotz aktiver
  Nutzung keine sichtbare Zahlungskultur zeigen könnte - Größe allein ist
  nicht das Signal, Größe kombiniert mit tatsächlicher Zahlungsbereitschaft
  (und bei welchem Preis) ist es. Echt gegen r/algotrading getestet (nicht
  nur implementiert): gemischter, ehrlich berichteter Befund - reale, wenn
  auch moderate Zahlungsbereitschaft (~$20-25/Monat-Größenordnung, ein
  Drittanbieter mit gestaffelter Preisstruktur direkt für diese Audience),
  aber eine ebenso reale, sichtbare Open-Source-/DIY-Gegenkultur
  (Freqtrade selbst, mehrere "I built and open-sourced my own X"-Posts) -
  weder klare Bestätigung noch klare Ablehnung, absichtlich nicht in eine
  Richtung schöngeredet. Alle drei Teile: 31 neue `checkup.py`-Tests
  (303 -> 334), in drei separaten Commits umgesetzt, jeder eigenständig
  grün getestet.
