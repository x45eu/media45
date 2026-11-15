# media45

Selbst gehosteter Film- und Serienkatalog auf Django/PostgreSQL. Keine Werbung,
kein Tracking und keine Medienwiedergabe. Filme und Serien werden anhand
bestätigter IMDb-Kennungen geführt; TMDB liefert serverseitige Metadaten und
Poster. Die NAS-Medien bleiben immer **read-only**.

## Funktionen

- Responsiver, dunkler Katalog mit Suche, Filtern, Status und Detailseiten.
- Medienwünsche, Benutzerkonten, private Wunschhistorie und Gotify-Hinweise.
- Passwort, verpflichtende TOTP-Absicherung für Administratoren und Passkeys.
- Lesen von Film- und Serien-Dateinamen aus dem NAS ohne Öffnen, Verändern
  oder Verschieben von Mediendateien.
- Metadatensuche über TMDB, einschließlich alternativer Titelschreibweisen.
  Vorschläge werden ausschließlich durch Administratoren freigegeben.
- Gesicherte Sammelfreigabe von bis zu 20 Treffern pro Seite, mit Filter
  „Genau ein Vorschlag“. Markieren ist **keine** automatische Freigabe.
- Lokal zwischengespeicherte TMDB-Poster in einem Docker-Volume.

## Projektumfang

media45 ist ein Katalog für **Filme und Serien**.
Die einzigen NAS-Roots für den Scanner sind:

- `MEDIA45_MOVIES_PATH` → `/media/movies`
- `MEDIA45_SERIES_PATH` → `/media/series`

Die beiden Host-Pfade werden ausschließlich in der privaten, nicht getrackten
`.env` konfiguriert. In `.env.example` stehen generische Pfade als Beispiele;
diese müssen vor Aktivierung von Scanner oder Scheduler durch die tatsächlichen
LXC-/Docker-Host-Pfade ersetzt werden. Die Container-Mounts bleiben `:ro`.
Der echte Hostname wird ebenfalls nur in der privaten `.env` als
`MEDIA45_HOST` und `MEDIA45_ORIGIN` festgelegt; das öffentliche Beispiel
verwendet die reservierte Domain `media.example.org`.

Die Verzeichnisse sind ausschließlich lesend (`:ro`) in den separaten
Scanner/Scheduler eingebunden, **nie** in den Web- oder Worker-Container.
`compose.scan.example.yaml` als Vorlage für lokale `compose.scan.yaml`
verwenden. Alte lokale Overlay-Dateien müssen beim Umstieg ebenfalls auf
die zwei Video-Mounts reduziert werden.

## Als App auf Handy und Tablet (PWA)

media45 lässt sich über den Browser auf dem Startbildschirm installieren,
ohne App-Store, Drittanbieter-Dienste oder ein weiteres Benutzerkonto.

- **Android (Chrome):** media45 über HTTPS öffnen und
  „App installieren“ wählen; alternativ das Browsermenü verwenden.
- **iPhone/iPad (Safari):** media45 über HTTPS öffnen, „Teilen“ wählen
  und „Zum Home-Bildschirm“ antippen.
- Die App öffnet im eigenständigen Fenster; Anmeldungen und Passkeys laufen
  unverändert über den vorhandenen sicheren WebAuthn-/Django-Zugang.

**Datenschutz:** Die PWA legt keine offline lesbaren Filmcover, privaten
Katalogseiten, Metadaten oder Zugangsdaten in einem Service-Worker-Cache ab.
Der Service Worker ist absichtlich ohne Netzwerk-/Fetch-Cache implementiert.
Ohne Verbindung zum eigenen Server funktioniert der Katalog nicht; die PWA
synchronisiert keine Nutzerdaten mit GitHub oder anderen Clouds.
Wie bei jeder Webseite können Betriebssystem und Browser ihre eigenen
temporären Caches verwenden.

**Voraussetzung:** Das Mobilgerät muss media45 über eine gültige HTTPS-URL
erreichen können (zum Beispiel über den bereits vorhandenen privaten
VPN-/Reverse-Proxy-Zugang). Ein reiner HTTP-Aufruf per LAN-IP ist im
Regelfall nicht für PWA-Installation und Service Worker geeignet.
Manifest und Icons werden von media45 selbst ausgeliefert.

## Lokale Katalogfilter

Im Katalog kannst du nach Titel oder IMDb-ID suchen und zusätzlich Medientyp,
Verfügbarkeit, **Genre**, **Erscheinungsjahr** und **TMDB-Mindestbewertung**
auswählen. Auch eine Sortierung nach der höchsten TMDB-Bewertung ist möglich.
Die erweiterten Kriterien stecken auf kleineren Bildschirmen hinter
„Weitere Filter“; sie lassen sich ohne JavaScript ausklappen.

Genre und Bewertung kommen ausschließlich aus den lokal in PostgreSQL
gespeicherten TMDB-Metadaten. Abgelaufene (älter als 90 Tage) oder nicht zur
bestätigten IMDb-ID passende Cache-Einträge werden dabei ausgeschlossen.
Medien ohne Bewertung bleiben in der ungefilterten Übersicht sichtbar.
Die Suche überträgt keinerlei Katalog- oder NAS-Daten an externe Dienste.

## Mobile Navigation

Auf Smartphones und Tablets mit höchstens 920 Pixeln Breite wird die
Hauptnavigation als kompaktes, aufklappbares **Menü** dargestellt.
Die Links hängen weiterhin von Anmeldung und Berechtigungen ab
(„Admin“ und „Freigaben“ nur für entsprechende Konten).
Das Menü nutzt native HTML-Elemente und speichert weder den Menüstatus
noch Benutzerinformationen im Browser. Die installierte PWA bleibt
unverändert nutzbar.

## Installation und Aktualisierung

Die Anwendung enthält ausschließlich Filme und Serien. Die tatsächlichen
NAS-Hostpfade werden nur in der privaten `.env` definiert und als read-only
Mounts für den Scanner/Scheduler freigegeben.

Vor einem Update sollte der Betreiber eine funktionierende Sicherung seines
Containers bzw. der PostgreSQL-Daten besitzen. media45 selbst stellt keine
eigene Backup-Oberfläche bereit.

```sh
git pull --ff-only origin main
docker compose build web worker
docker compose run --rm web python manage.py check
docker compose run --rm web python manage.py makemigrations --check --dry-run
docker compose run --rm web python manage.py test catalog
docker compose run --rm web python manage.py migrate
docker compose up -d --no-deps --force-recreate web worker
```

## Medien inventarisieren und erkennen

```sh
docker compose -f compose.yaml -f compose.scan.yaml build scanner
docker compose -f compose.yaml -f compose.scan.yaml run --rm scanner python manage.py scan_media --root all --dry-run
docker compose -f compose.yaml -f compose.scan.yaml run --rm scanner python manage.py scan_media --root all
docker compose run --rm web python manage.py group_media
docker compose run --rm web python manage.py match_media --root movies --limit 20
docker compose run --rm web python manage.py match_media --root series --limit 20
```

Zur erneuten Recherche schon geprüfter Gruppen `--retry-empty` oder
`--group-id ID --refresh` verwenden. `--all` durchsucht alle noch
nicht geprüften Gruppen, ohne die Zuordnungen automatisch zu bestätigen.

Freigaben: `/review/`. Benachrichtigungen und Cover sind separate Aufgaben.
Mit `fetch_covers --status` wird der lokale Posterbestand ohne Netzwerkzugriff
gezählt. `fetch_covers --limit 12` versucht jetzt **zwölf tatsächlich fehlende
oder abgelaufene Poster** zu laden; bereits zwischengespeicherte Titel
verbrauchen das Limit nicht. Bei Wiederholung werden vorhandene Poster
übersprungen. `--refresh` erzwingt dagegen neue Downloads, `--all`
arbeitet alle fehlenden ab. Die Cover bleiben ausschließlich im lokalen
Docker-Volume `coverdata`.

```sh
docker compose run --rm web python manage.py fetch_covers --status
docker compose run --rm web python manage.py fetch_covers --limit 12
docker compose run --rm web python manage.py fetch_covers --status
```

## Erweiterte TMDB-Detailseiten

Nach dem vollständigen Posterdownload können die freigegebenen Filme und
Serien zusätzlich mit einer deutschen Handlungsbeschreibung, Genres,
Laufzeit (bei Serien: pro Episode) und TMDB-Bewertung angezeigt werden.

Die Daten werden **ausschließlich über die bestätigte IMDb-ID** bei TMDB
aufgelöst und in PostgreSQL zwischengespeichert. Bei zweideutigen oder
ungültigen Zuordnungen erfolgt kein Import. Das Betrachten der Detailseite
verursacht keine API-Anfragen und ändert weder Titel, Freigaben noch NAS-Dateien.

Vor dem ersten Abruf die neue Migration einspielen:

```sh
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py fetch_details --status
docker compose run --rm web python manage.py fetch_details --limit 12
docker compose run --rm web python manage.py fetch_details --status
```

Mit `--limit 12` werden bis zu zwölf **fehlende oder abgelaufene** Datensätze
aktualisiert, andere zählen nicht zum Limit. `--all` bearbeitet alle,
`--media-id 60` nur einen Titel, `--refresh` erzwingt die Aktualisierung.
Die Daten werden nach 90 Tagen als abgelaufen eingestuft und bei einem
späteren Batch erneut geprüft. Für den Abruf ist der private
`TMDB_READ_ACCESS_TOKEN` in der lokalen `.env` erforderlich.

## Testanfragen und verwaiste Katalogeinträge

Unter **Meine Wünsche** kann jede angemeldete Person ihre eigenen Anfragen
über **Löschen** und eine Bestätigungsseite entfernen. Andere Nutzer können
deren Wünsche nicht löschen. Der Administrator kann alle Anfragen weiterhin
im Django-Admin verwalten.

Eine Medienanfrage und ihr Katalogeintrag sind getrennte Datensätze.
Wenn ein Administrator eine Anfrage im Django-Admin löscht, entfernt media45
den Katalogeintrag nur, wenn er weiterhin **Angefragt** ist, keinerlei
NAS-Dateien verknüpft hat und auch keine andere Person ihn angefragt hat.
Vorhandene oder bereits bearbeitete Titel bleiben erhalten.

Ältere, bereits gelöschte Testanfragen können einen verwaisten Katalogeintrag
hinterlassen. Zum Prüfen (ohne Datenänderung):

```sh
docker compose run --rm web python manage.py cleanup_orphan_requests
```

Erst nach Prüfung der angezeigten Titel gezielt entfernen, zum Beispiel
`cleanup_orphan_requests --media-id 123 --apply`. Ohne `--media-id`
würde `--apply` **alle** aufgelisteten, ungenutzten Anfrage-Titel
entfernen. Der Befehl greift niemals auf NAS-Dateien zu.

## Öffentliches Repository und Datenschutz

`.env`, `compose.scan.yaml`, Zugangsdaten und lokale Backups gehören niemals
ins Repository. Vor dem Freigeben prüfen, ob persönliche Hostnamen, IP-Adressen,
Benutzer, echte E-Mail-Adressen oder NAS-Pfade in Code, Issues, Logs oder
Git-Historie vorkommen. Ein neuer Commit überschreibt **nicht** die
Git-Historie; bereits veröffentlichte Angaben bleiben in alten Commits
auffindbar. GitHub-Repository-Eigentümer und Commit-Autoren sind ebenfalls
öffentlich zuordenbar und werden durch neutrale Beispieldateien nicht anonym.

Bei versehentlich veröffentlichten Zugangsdaten sofort die betroffenen
Passwörter/Tokens rotieren und anschließend die Git-Historie separat prüfen.
Die echte lokale `.env` auf dem Server darf für eine GitHub-Anonymisierung
nicht mit generischen Beispielwerten überschrieben werden.

## Konfiguration und Betrieb

Benötigt eine private `.env` mit Datenbank-Zugang, langem `SECRET_KEY`,
HTTPS-Origin, `TMDB_READ_ACCESS_TOKEN` und optionalen Gotify-Zugangsdaten.
Keine Geheimnisse einchecken. Netzwerkrouting, Reverse Proxy und Backup
vor einem öffentlichen Betrieb eigenständig absichern. Alle Tests vor
Migration/Deployment ausführen.

`compose.automation.yaml` enthält einen optionalen Scheduler für
Video-Inventur und kurze TMDB-Abgleiche. Er benötigt nur die beiden
read-only Video-Mounts. Für Poster sind die TMDB-Nutzungsbedingungen
und Quellenhinweise unter `/quellen/` zu beachten.

## Netzwerksicherheit bei Neuinstallationen

**Kein eingebauter HTTPS-Server.** Die Standardinstallation bindet den
Web-Port ausschließlich an `127.0.0.1:3000` auf dem Docker-Host. Ein
vertrauenswürdiger TLS-Reverse-Proxy muss HTTPS bereitstellen und HTTP auf
HTTPS umleiten, bevor die Anwendung für Dritte erreichbar gemacht wird.
Auf dem TLS-Endpunkt nach korrekter HTTPS-Einrichtung auch HSTS konfigurieren.
Die `MEDIA45_ORIGIN` muss die tatsächliche `https://`-URL enthalten.

Nur wenn ein externer Proxy im **abgesicherten internen Netz** den Docker-Host
erreichen muss, kann die private `.env` beispielsweise enthalten:

```dotenv
MEDIA45_BIND_ADDRESS=0.0.0.0
```

**Achtung:** Das öffnet Port 3000 auf allen Host-Netzwerkinterfaces. Dafür
müssen Firewall und Netzwerkzugriff eigenständig auf den Proxy begrenzt sein.
Bei Bedarf statt `0.0.0.0` gezielt eine Host-IP-Adresse wählen.

Standardmäßig vertraut Django **keinen** `X-Forwarded-Proto`-Headern. Nur
wenn der Reverse-Proxy vom Client kommende Forwarded-Header zuverlässig
entfernt/überschreibt **und** direkter untrusted Zugriff auf Django gesperrt
ist, kann in der privaten `.env` zusätzlich stehen:

```dotenv
MEDIA45_TRUST_PROXY_HTTPS=1
```

Diese Option niemals allein als HTTPS-Schutz verstehen: TLS, Redirect und
HSTS bleiben Aufgaben des vertrauenswürdigen Proxys. Die Django-Deploy-Checks
warnen deshalb weiterhin über deaktivierten App-Redirect und App-HSTS.
