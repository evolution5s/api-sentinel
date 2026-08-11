# Arbeitsweise mit Claude in diesem Repo

Dieses Dokument legt fest, was Claude in api-sentinel ohne Rückfrage tun darf
und wo weiterhin nachgefragt werden muss. Ziel: weniger Unterbrechungen bei
Routinearbeit, aber die gleiche Vorsicht bei Dingen, die echten Schaden
anrichten oder den Scope stillschweigend erweitern können.

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
