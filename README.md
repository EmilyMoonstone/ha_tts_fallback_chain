# TTS Fallback Chain für Home Assistant

Eine Custom Integration, die mehrere TTS-Dienste **in Reihe schaltet**. Sie
stellt eine eigene TTS-Entität bereit (z. B. `tts.tts_fallback_kette`). Diese
fragt die eingerichteten TTS-Dienste nacheinander an und nimmt den ersten, der
funktioniert.

Typisches Beispiel:

| Stufe | Dienst | Stimme |
|------:|--------|--------|
| 1 | Google Gemini TTS, **Free Tier** (`tts.backend_google_ai_tts_free`) | Callirrhoe |
| 2 | Google Gemini TTS, **bezahlter API-Key** (`tts.google_ai_tts`) | Callirrhoe |
| 3 | **Home Assistant Cloud** (`tts.home_assistant_cloud`) | Katja |

Ist das kostenlose Kontingent aufgebraucht (HTTP 429 / `RESOURCE_EXHAUSTED`),
springt die Kette automatisch auf den bezahlten Key. Fällt auch der aus,
übernimmt Home Assistant Cloud. Du musst nichts mehr manuell umstellen.

## Funktionen

- **Beliebig viele Stufen** in fester Reihenfolge. Jede TTS-Entität, die in Home
  Assistant eingerichtet ist, kann eine Stufe sein.
- **Eigene Stimme pro Stufe:** Die Stimme wird als Dropdown mit den Stimmen des
  jeweiligen Dienstes angeboten. Du kannst sie auch frei eintippen.
- Optional pro Stufe eine **feste Sprache** und **weitere TTS-Optionen**.
- **Fehler und langsame Erstellung werden unterschiedlich behandelt:**
  - **Fehler** (z. B. 503 „high demand“ oder 429 Kontingent): Die nächste Stufe
    startet sofort, und die fehlerhafte Stufe wird pausiert (siehe unten).
  - **Langsam** (Timeout der Stufe abgelaufen, aber noch kein Fehler): Die
    Stufe wird **nicht abgebrochen und nicht pausiert**. Die nächste Stufe
    startet zusätzlich parallel, und das zuerst fertige Audio wird genommen.
    Liefert die langsame Stufe doch noch zuerst, wird ihr Audio verwendet.
- **Timeout pro Stufe:** Es gibt einen Standard-Timeout (20 s), den du pro Stufe
  überschreiben kannst, z. B. kurz für Free und lang für den bezahlten Key.
- **Maximale Gesamtwartezeit** (Standard 120 s): Liefert bis dahin keine Stufe
  Audio, wird die Ansage abgebrochen.
- **Pausen (Cooldown), nur nach Fehlern:** Eine fehlerhafte Stufe wird eine
  Zeit lang übersprungen, damit nicht jede Ansage erst in den Fehler läuft.
  - nach einem normalen Fehler: Standard 5 Minuten
  - nach einem Kontingent- oder Rate-Limit-Fehler (429): Standard 60 Minuten
  - Timeouts lösen keine Pause aus.
  - Sind alle Stufen pausiert, werden sie trotzdem der Reihe nach versucht.
    Eine Ansage fällt also nie nur wegen einer Pause aus.
- **Sensor „Zuletzt genutzter TTS-Dienst“:** Zeigt, welche Stufe zuletzt
  geantwortet hat und wie lange die Erstellung gedauert hat. Als Attribute gibt
  es pro Stufe: Erfolge, Fehler, Anzahl langsamer Anfragen, letzte und
  durchschnittliche Dauer, den letzten Fehler und das Ende der Pause. Mit den
  Dauern kannst du die Timeouts begründet festlegen.
- **Button „Pausen zurücksetzen“:** Alle Stufen werden sofort wieder normal
  versucht.
- **Events** für eigene Automationen:
  - `tts_fallback_chain_stage_failed` (nur echte Fehler) enthält `stage`,
    `entity_id`, `error`, `quota_error`, `duration`, `cooldown_until` und
    `message`.
  - `tts_fallback_chain_stage_slow` (Timeout, die Stufe läuft weiter) enthält
    `stage`, `entity_id`, `waited` und `message`.
  - `tts_fallback_chain_all_stages_failed` enthält `message` und `errors`.
- Die Ansagen der Kette landen ganz normal im TTS-Cache von Home Assistant. Eine
  wiederholte Ansage verbraucht also kein Kontingent.

## Installation über HACS

1. HACS → oben rechts **⋮** → **Benutzerdefinierte Repositories**.
2. Repository: `https://github.com/EmilyMoonstone/ha_tts_fallback_chain`,
   Typ: **Integration** → Hinzufügen.
3. In HACS nach **TTS Fallback Chain** suchen → **Herunterladen**.
4. Home Assistant **neu starten**.

Manuell geht es auch: Kopiere den Ordner `custom_components/tts_fallback_chain`
nach `config/custom_components/` und starte Home Assistant neu.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „TTS Fallback Chain“**.
2. Allgemeine Einstellungen: Name, Standardsprache, Standard-Timeout,
   Gesamtwartezeit und Pausen.
3. **Stufe 1:** Google AI TTS Free wählen, danach als Stimme **Callirrhoe**.
   Den Timeout leer lassen, dann gilt der Standard von 20 s.
4. **Weitere Stufe hinzufügen** → Google AI TTS (bezahlt), Stimme **Callirrhoe**,
   Timeout z. B. **45 s**. Gemini erzeugt die komplette Audiodatei, bevor es sie
   schickt, und braucht bei langen Texten oder hoher Last entsprechend länger.
5. **Weitere Stufe hinzufügen** → Home Assistant Cloud, Stimme **Katja**
   (`KatjaNeural`).
6. **Fertig, speichern.**

Die neue Entität `tts.<name>` kannst du jetzt überall auswählen, wo man eine
TTS-Engine auswählen kann: Assist-Pipeline, Music Assistant, `tts.speak`, …

Später ändern: Integration → **Konfigurieren**. Standard-Timeout,
Gesamtwartezeit und Pausen änderst du direkt. Für neue Stufen, eine andere
Reihenfolge, andere Stimmen oder Timeouts pro Stufe setzt du den Haken
**„Stufen neu festlegen“**. Die bisherigen Einstellungen werden dabei als
Vorschlag übernommen.

### Stimmen-IDs

| Dienst | Stimme | ID |
|--------|--------|----|
| Google Gemini TTS | Callirrhoe | `callirrhoe` |
| Home Assistant Cloud (de-DE) | Katja | `KatjaNeural` |

Im Dropdown steht immer auch die ID in Klammern.

## Beispiel

```yaml
action: tts.speak
target:
  entity_id: tts.tts_fallback_kette
data:
  media_player_entity_id: media_player.wohnzimmer
  message: "Die Waschmaschine ist fertig."
```

Benachrichtigung, wenn auf die bezahlte Stufe gewechselt wird:

```yaml
triggers:
  - trigger: event
    event_type: tts_fallback_chain_stage_failed
    event_data:
      stage: 1
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.quota_error }}"
actions:
  - action: persistent_notification.create
    data:
      title: TTS
      message: "Google-Free-Kontingent aufgebraucht, nutze jetzt den bezahlten Key."
```

## Hinweise

- **Streaming:** Die Kette erzeugt die Ansage erst, wenn der ganze Text
  vorliegt, weil sie bei einem Fehler denselben Text an die nächste Stufe geben
  muss. Bei sehr langen Assist-Antworten beginnt die Sprachausgabe dadurch etwas
  später als bei einer Streaming-Engine.
- Fehler einzelner Stufen erscheinen weiterhin im Log des jeweiligen Dienstes.
  Die Kette schreibt zusätzlich eine Warnung, welche Stufe übersprungen wurde.
- Eine Fallback-Kette kann nicht selbst Stufe einer Kette sein, weil das eine
  Endlosschleife ergeben würde.

## Entwicklung

```bash
pip install -r requirements_test.txt   # Python 3.14
python -m pytest
```
