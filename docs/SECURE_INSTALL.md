# Sichere Installation und lokale Prüfung

Die App wird ohne eingebauten HTTPS-Server ausgeliefert. **Nicht direkt öffentlich
über HTTP betreiben.** Der Docker-Hostport bindet standardmäßig nur an
`127.0.0.1:3000`. Ein eigenständig verwalteter TLS-Reverse-Proxy muss
HTTPS-Zertifikate, Weiterleitung von HTTP auf HTTPS und HSTS sicher handhaben.

## Vor dem ersten Start

1. `.env.example` nach `.env` kopieren und alle Beispielgeheimnisse durch lokal
   erzeugte, lange Zufallswerte ersetzen; `.env` niemals committen.
2. `MEDIA45_HOST` und `MEDIA45_ORIGIN=https://...` korrekt setzen.
3. Docker-Hostport standardmäßig an `127.0.0.1` gebunden lassen.
4. Muss ein externer Proxy den Host erreichen, `MEDIA45_BIND_ADDRESS` nur
   auf einer abgesicherten Host-IP oder mit passenden Firewall-Regeln nutzen.
   `0.0.0.0` veröffentlicht den Port auf allen Host-Interfaces.
5. `MEDIA45_TRUST_PROXY_HTTPS=1` **nur** einsetzen, wenn der Proxy ankommende
   `X-Forwarded-Proto`-Header entfernt/überschreibt und Unbefugte Django nicht
   direkt erreichen können. Sonst Standardwert `0` beibehalten.
6. NAS-Mounts ausschließlich `:ro` an den dafür vorgesehenen Scheduler/Scanner
   vergeben. Web und Worker benötigen keinen NAS-Mount oder Docker-Socket.
7. Backups, Restore-Tests, Firewall, TLS-Konfiguration und Security-Updates
   bleiben Aufgabe des jeweiligen Betreibers.

## Offline-Sicherheitscheck

Im Projektverzeichnis ausführen:

```bash
python3 tools/security_check.py
python3 -m unittest discover -s tools -p 'test_security_check.py' -v
```

Der Check ist **nur lesend**, benötigt weder Docker noch Internetzugang und
zeigt keine Secret-Werte an. Er kontrolliert Konfigurationsgrundlagen und die
Standard-Portbindung. Hinweise zu Proxy-Headern sind bewusst keine Fehler,
weil nur der Betreiber seine Netzwerktopologie sicher beurteilen kann.

**Nicht geprüft:** tatsächliche TLS-/HSTS-Einstellungen am Proxy, Erreichbarkeit
von außen, Firewall-Regeln, laufende Container, vorhandene Backups und aktuelle
bekannte Software-Schwachstellen. Ein erfolgreiches Ergebnis ist kein
Penetrationstest oder Sicherheitszertifikat.

Bei einem Update wie gewohnt zuerst Betreiber-Backup und reguläre Django-Tests
berücksichtigen. Der Check benötigt keinen Neustart und ändert keine Daten.
