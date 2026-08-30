# ai-status

Kleines, dependency-freies CLI-Tool für die KI-Limitübersicht. Es verwendet dieselbe direkte Usage-Abfrage wie `../home-server`: Codex liest die lokalen `auth.json`-Dateien, Copilot verwendet das Token aus `gh auth token`. Die drei Abfragen laufen parallel; die Ausgabe wird alle 30 Sekunden aktualisiert.

```bash
python3 ai_status.py
```

Alternativ direkt aus diesem Verzeichnis:

```bash
./ai-status
```

Die Codex-Profilpfade entsprechen standardmäßig `~/.codex-account1` und `~/.codex-account2`. Sie können mit `CODEX1_HOME` und `CODEX2_HOME` überschrieben werden.

Ein einzelner Durchlauf:

```bash
python3 ai_status.py --once
```

Beispielausgabe:

```text
AI-Statusmonitor gestartet · Ctrl-C zum Beenden

KI-Limits · 30.08.2026 15:15:20 CEST
Status aktualisiert · nächste Abfrage in 30 s · drei Requests parallel …

Codex 1
  5-Stunden-Limit          24 / 100 % [#####---------------] · Noch 52 Min. · 0 Sek.
  Wochenlimit                4 / 100 % [#-------------------] · Noch 6 Tage · 19 Std.

Codex 2
  5-Stunden-Limit           0 / 100 % [--------------------] · Noch 2 Std. · 27 Min.
  Wochenlimit                0 / 100 % [--------------------] · Noch 6 Tage · 21 Std.

GitHub Copilot
  Premium-Interaktionen    25.000 / 25.000 Credits [####################] · KRITISCH · Noch 1 Tag · 8 Std.
```

Zusätzliche Optionen:

```text
--interval SEKUNDEN     Pause zwischen den Abfragen (Standard: 30)
--timeout SEKUNDEN      Timeout pro HTTP-/Login-Abfrage (Standard: 10)
```

Copilot zeigt wie im Webserver nur `Premium-Interaktionen`; Codex zeigt 5-Stunden- und Wochenlimit mit Verbrauch, Balken und der verbleibenden Reset-Zeit direkt in derselben Zeile. Die Anbieter werden in fester Reihenfolge ausgegeben, damit die 30-Sekunden-Snapshots gut vergleichbar bleiben. Tokens werden nur für den jeweiligen Request im Speicher verwendet und weder angezeigt noch gespeichert.

Tests:

```bash
python3 -m unittest -v
```
