Drei Dinge, die Sie noch tun müssen:

Ihre aktuelle index.html in index.template.html umbenennen (bleibt als Vorlage unverändert)
Den ANTHROPIC_API_KEY als Repository-Secret hinterlegen — er steht so nie im Browser
Unter Settings → Actions die Schreibrechte für Workflows aktivieren

Die Live-Daten (Besucherzähler, Sport, Wechselkurse) laufen weiterhin direkt im Browser, da diese APIs
keinen Schlüssel brauchen — nur die KI-Recherche wird in den nächtlichen Build verlagert.
