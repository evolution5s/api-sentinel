# Arbeitsweise mit Claude in diesem Repo

Dieses Dokument legt fest, was Claude in api-sentinel ohne Rückfrage tun darf
und wo weiterhin nachgefragt werden muss. Ziel: weniger Unterbrechungen bei
Routinearbeit, aber die gleiche Vorsicht bei Dingen, die echten Schaden
anrichten oder den Scope stillschweigend erweitern können.

## Lokale Entwicklungsumgebung

`.venv/` in diesem Repo ist die einzige korrekte Python-Umgebung für lokale
Läufe von `checkup.py` o.ä. - nicht das System-`python`/`where python`
(landet auf Windows auf Inkscapes mitgeliefertem Python-Interpreter, ohne
`crewai` und den Rest von `requirements.txt`), und nicht irgendeine andere
global installierte Python-Version.

- **Venv-Python direkt aufrufen (bevorzugt, kein Aktivieren nötig):**
  `.venv/Scripts/python.exe checkup.py`
- **Venv aktivieren (PowerShell):** `.venv\Scripts\Activate.ps1`, danach
  reicht `python checkup.py`.
- **Falls `.venv/` fehlt oder kaputt ist** (z.B. `pyvenv.cfg` zeigt auf
  einen falschen `home`-Pfad statt einer echten Python-Installation): neu
  aufbauen aus der globalen Python-3.11-Installation unter
  `C:\Users\janal\AppData\Local\Programs\Python\Python311\python.exe -m venv
  .venv`, danach `.venv/Scripts/python.exe -m pip install -r
  requirements.txt`. `.venv/` ist in `.gitignore`, muss also nach jedem
  Neuaufbau lokal neu installiert werden, nie eingecheckt.

## Ohne Rückfrage erlaubt (Autopilot)

- **Lesende Operationen, immer:** `railway logs`, `railway status`,
  `railway deployment list`, `git status/diff/log`, Tests laufen lassen
  (`python checkup.py`), Dateien lesen/durchsuchen.
- **Lokale Git-Commits** in diesem Repo, jederzeit.
- **`git push origin main`**, wenn die Änderung eine der folgenden ist:
  - Bugfix für bestehenden, bereits vorhandenen Code (inkl. Dependency-Konflikte,
    Retry-/Reliability-Probleme wie heute).
  - Test-Ergänzungen/-Korrekturen.
  - Änderungen, die direkt aus einer bereits im Gespräch beschriebenen Aufgabe
    folgen (keine neue Idee, keine neue Fähigkeit).
- **Proaktives Prüfen von Railway-Logs/Status** nach jedem eigenen Push, ohne
  dass ich extra danach fragen muss - inkl. Nachforschen bei Fehlern.
- **Railway-Variablen setzen/lesen**, wenn sie eindeutig zur gerade
  besprochenen Aufgabe gehören (z.B. Telegram-Token, die wir gerade gemeinsam
  eingerichtet haben). Werte selbst nie ungefragt in den Chat drucken.
- **Kleinere, offensichtliche Eigenentscheidungen** bei mehrdeutigen
  Anweisungen treffen und im Nachhinein kurz begründen, statt vorher zu fragen.
- **Wenn nach `FIX.md` gefragt wird:** zuerst den echten, aktuellen Inhalt
  direkt vom Railway-Volume holen - nie eine lokale Kopie annehmen, es gibt
  keine, `FIX.md` lebt ausschließlich auf dem Volume. **Wichtig, empirisch
  bestätigt (2026-08-13):** `api-sentinel` ist ein Cron-Job-Service ohne
  laufenden Container zwischen den geplanten Läufen (`status: created`
  zwischen Ticks). Weder `railway run -- cat /data/...` (läuft lokal mit
  injizierten Env-Vars, kein echter Remote-Zugriff - `/data` existiert auf
  der lokalen Maschine schlicht nicht) noch `railway ssh` ("container is
  not running") noch `railway service files list/download` (scheitert mit
  SFTP-Timeout - nutzt laut Test denselben Live-Container-Mechanismus wie
  `ssh`, kein direkter Volume-Zugriff) funktionieren zwischen den Cron-
  Ticks. Echter Zugriff ist aktuell nur während eines tatsächlich laufenden
  Cron-Ausführungsfensters möglich (`railway ssh`/`railway service files`
  während der Container kurz aktiv ist).

  **Geprüfter manueller Trigger, kein kostenloser Workaround:**
  `railway restart` (ohne Rebuild) oder `railway redeploy` (bzw. im
  Dashboard "Restart"/"Redeploy", oder Cmd+K "Deploy Latest Commit")
  starten den Service sofort außerhalb des Cron-Plans - laut Railway-Doku
  gibt es dafür keinen separaten "Container nur kurz für SSH hochfahren"-
  Mechanismus, es ist derselbe Weg wie ein normales Redeploy. Das führt
  also den echten Start-Befehl (`python crew.py`) aus: ein vollständiger,
  echter, außerplanmäßiger Agenten-Zyklus mit echten Anthropic-API-Kosten
  und echten Seiteneffekten (mögliche Telegram-Nachrichten, Approval-
  Einträge, Zustandsänderungen) - nicht ein harmloses "kurz reinschauen".
  Deshalb: **nur nach expliziter Zustimmung von Jan einsetzen**, genau wie
  jede andere kostenpflichtige Aktion - danach das kurze Zeitfenster ab
  Container-Start für `railway ssh`/`railway service files` nutzen, bevor
  der Zyklus durchläuft und der Container wieder terminiert.

  Die frühere Formulierung, `railway run -- cat
  /data/_holding/FIX.md` liefere "den exakten Pfad", war falsch und ist
  hiermit korrigiert - siehe `OPERATING_MODEL.md` Abschnitt 6 für die volle
  Herleitung. Eine dauerhafte Lösung (Umbau von Cron-Job zu einem
  durchlaufenden Worker-Service mit interner Sleep-Loop-Schedule) wurde
  geprüft und bewusst NICHT umgesetzt (2026-08-13, Jans Entscheidung) - der
  Kostensprung von "nur pro Ausführung abgerechnet" zu "durchgehend
  abgerechnet" steht in keinem Verhältnis zum seltenen Bedarf an Live-
  Volume-Zugriff; der obige manuelle Trigger (mit Rückfrage) deckt den
  Bedarf bei Bedarf ab. Nicht erneut vorschlagen, außer der Bedarf ändert
  sich grundlegend (z.B. deutlich häufigerer Live-Debugging-Bedarf).

  Jeden gefundenen Abschnitt als eigene Addendum-artige Aufgabe bearbeiten,
  mit derselben Sorgfalt wie ein direkt von Jan eingefügtes Addendum. Nach
  Umsetzung im Commit/Summary explizit nennen, welche `FIX.md`-Einträge
  adressiert wurden, damit die nächste Zyklus-Archivierung
  (`fix_resolved: <id>` per Telegram) etwas Konkretes zum Abgleichen hat.
  Dieser Mechanismus selbst wendet nie etwas an - er schreibt nur nach
  `FIX.md`; jede tatsächliche Umsetzung bleibt ein separater, bewusster
  Schritt in einer eigenen Claude-Code-Session.

## Weiterhin Rückfrage nötig

- **Neue externe Integrationen, Agenten oder Tools hinzufügen** (neue APIs,
  neue Fähigkeiten, neue Drittanbieter-Services) - das ist genau die Art von
  Scope-Erweiterung, die beim ungefragten Binance/Bybit-Watcher schiefging.
  Gilt auch für "das wäre doch sinnvoll"-Ideen, nicht nur explizite Bitten.
- **Alles, was Geld kostet oder verpflichtend wird** - deckt sich mit der
  `category='spend'`-Grenze, die der CEO-Agent selbst einhalten muss. Diese
  Grenze gilt für mich genauso.
- **Destruktive/schwer umkehrbare Git-Operationen**: force-push, reset --hard,
  Branches/Tags löschen, Historie umschreiben.
- **Architekturentscheidungen mit größerer Tragweite**: neue Kern-Datenstrukturen,
  Umbau bestehender Verträge zwischen Agenten/Tools, Entfernen bestehender
  Sicherheitsmechanismen (Approval-Queue, Score-Formel, etc.).
- **Alles außerhalb dieses Repos** (andere Projekte, andere Railway-Services,
  E-Mails/Nachrichten an Dritte).

## Regel-Wachstum bei Freigaben

**Regel-Wachstum bei Freigaben.** Immer wenn eine Aktion eine
Freigabe-Abfrage auslöst, die noch nicht durch eine `allow`-Regel in
`.claude/settings.json` abgedeckt ist, und Jan sie genehmigt: nach der
Genehmigung explizit fragen, ob diese Art von Aktion künftig dauerhaft
erlaubt werden soll (z.B. "Soll ich `Bash(railway up:*)` dauerhaft
erlauben?"). Bei Ja: die konkrete Regel sofort zu `.claude/settings.json`
hinzufügen - als präzises Muster, das genau dem gerade genehmigten Fall
entspricht, kein zu breiter Auffangposten (z.B. `Bash(railway up:*)`,
nicht `Bash(*)`) - und diese Änderung committen, damit sie über Sessions
hinweg bestehen bleibt, ohne dass Jan die Datei selbst manuell pflegen
muss. Bei Nein: nicht hinzufügen, und beim nächsten Mal erneut fragen -
ein "Nein" ist keine dauerhafte Ablehnung, nur ein "noch nicht".

**Nie über diesen Weg eine `deny`-Regel aufweichen.** Wenn etwas, das Jan
genehmigt, nur durch das Umgehen eines bestehenden `deny`-Eintrags möglich
gewesen wäre, nicht anbieten, das als neue `allow`-Regel dauerhaft zu
machen - das ist eine bewusste Grenze (Force-Push, Secrets,
Ressourcen-Löschung) und braucht ein eigenes, explizites Gespräch über die
Grenze selbst, keine beiläufige "soll ich mir das merken"-Nachfrage.

**Präzise Muster statt breiter Kategorien bevorzugen.** Eine Regel sollte
genau das abdecken, was tatsächlich gemacht wurde, nicht "gleich mal" eine
größere Kategorie öffnen - die Allow-Liste wächst einen echten,
beobachteten Bedarf nach dem anderen, dieselbe Disziplin, die an anderer
Stelle in diesem Repo schon für `stage_justification`/Reasoning-Feld-
Durchsetzung gilt: verdiente Spezifität, keine spekulative Breite.

## Warum diese Grenze so gezogen ist

Reversible, lokale oder rein-lesende Aktionen kosten im schlimmsten Fall
Zeit. Push-nach-main für bekannte/besprochene Arbeit ist im schlimmsten Fall
ein weiterer Fix-Commit. Scope-Erweiterungen und Geldausgaben sind dagegen
genau die zwei Kategorien, die in diesem Projekt schon einmal wirklich
schiefgelaufen sind (ungefragte Exchange-Überwachung, ein Pinning-Fehler der
den Build 17 Minuten lang lahmgelegt hat) - dort bleibt die Rückfrage.
