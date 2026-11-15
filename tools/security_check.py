#!/usr/bin/env python3
"""Offline configuration guardrails; never prints values from .env."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            raise ValueError(f"Ungültiger .env-Eintrag in Zeile {num}")
        key, value = s.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"Ungültiger .env-Schlüssel in Zeile {num}")
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise ValueError(f"Ungültige Anführungszeichen in .env-Zeile {num}")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def assess(root: Path) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ok: list[str] = []
    env_file = root / ".env"
    if not env_file.is_file():
        return ["Private .env fehlt (siehe .env.example)."], warnings, ok
    if env_file.is_symlink():
        warnings.append(".env ist ein Symlink; Ziel und Berechtigungen selbst prüfen.")
    if os.name == "posix" and (env_file.stat().st_mode & 0o077):
        warnings.append(".env ist für Gruppe/Andere lesbar oder schreibbar; chmod 600 prüfen.")
    try:
        env = read_env(env_file)
    except (OSError, UnicodeError, ValueError) as exc:
        return [f".env konnte nicht sicher gelesen werden: {type(exc).__name__}"], warnings, ok

    key = env.get("SECRET_KEY", "")
    if len(key) < 40 or key.startswith("REPLACE_"):
        errors.append("SECRET_KEY fehlt, ist Platzhalter oder zu kurz (mindestens 40 Zeichen).")
    else:
        ok.append("SECRET_KEY ist gesetzt und nicht als Beispielwert erkennbar.")
    db_pass = env.get("POSTGRES_PASSWORD", "")
    if not db_pass or db_pass.startswith("REPLACE_"):
        errors.append("POSTGRES_PASSWORD fehlt oder ist ein Beispielwert.")
    else:
        ok.append("PostgreSQL-Passwort ist gesetzt.")

    host = env.get("MEDIA45_HOST", "").strip()
    origin = env.get("MEDIA45_ORIGIN", "").strip()
    try:
        parsed = urlsplit(origin)
        valid_origin = (parsed.scheme == "https" and bool(parsed.hostname)
                        and parsed.username is None and parsed.password is None
                        and not parsed.path.rstrip("/") and not parsed.query and not parsed.fragment)
        if not valid_origin:
            errors.append("MEDIA45_ORIGIN muss eine reine HTTPS-Origin ohne Pfad/Query sein.")
        elif parsed.netloc != host:
            errors.append("MEDIA45_HOST muss zum Host einschließlich optionalem Port der MEDIA45_ORIGIN passen.")
        else:
            ok.append("MEDIA45_HOST und HTTPS-Origin passen zusammen.")
    except ValueError:
        errors.append("MEDIA45_ORIGIN hat eine ungültige URL-Syntax.")

    bind = env.get("MEDIA45_BIND_ADDRESS", "127.0.0.1").strip()
    try:
        address = ipaddress.ip_address(bind)
        if address.is_loopback:
            ok.append("Docker-Hostport ist auf Loopback begrenzt.")
        else:
            warnings.append("Docker-Hostport ist nicht auf Loopback begrenzt: Zugriff per Firewall/Proxy einschränken.")
    except ValueError:
        errors.append("MEDIA45_BIND_ADDRESS muss eine gültige IPv4- oder IPv6-Adresse sein.")

    trust = env.get("MEDIA45_TRUST_PROXY_HTTPS", "0")
    if trust not in ("0", "1"):
        errors.append("MEDIA45_TRUST_PROXY_HTTPS darf nur 0 oder 1 sein.")
    elif trust == "1":
        warnings.append("Proxy-HTTPS-Vertrauen aktiv: Proxy muss X-Forwarded-Proto bereinigen und direkten Zugriff sperren.")

    compose = root / "compose.yaml"
    settings = root / "media45" / "settings.py"
    try:
        compose_text = compose.read_text(encoding="utf-8")
        settings_text = settings.read_text(encoding="utf-8")
        if '${MEDIA45_BIND_ADDRESS:-127.0.0.1}:3000:8000' not in compose_text:
            errors.append("Erwartete sichere Standard-Portbindung in compose.yaml nicht gefunden.")
        else:
            ok.append("Sichere Docker-Portvorgabe ist im Repository enthalten.")
        for token in ("DEBUG = False", "SESSION_COOKIE_SECURE = True", "CSRF_COOKIE_SECURE = True", "SESSION_COOKIE_HTTPONLY = True"):
            if token not in settings_text:
                errors.append(f"Erwartete Django-Grundeinstellung nicht gefunden: {token}")
        if not any(x.startswith("Erwartete Django-Grundeinstellung") for x in errors):
            ok.append("Django-Grundschutzschalter sind im Code vorhanden.")
    except OSError:
        errors.append("compose.yaml oder media45/settings.py konnte nicht gelesen werden.")

    # These are intentionally reminders, not claims about network reality.
    warnings.append("HTTPS-Zertifikat, Proxy-Redirect und HSTS sind offline nicht überprüfbar.")
    warnings.append("Backups, tatsächliche Firewall-Regeln und Software-CVEs sind nicht Bestandteil dieses Checks.")
    return errors, warnings, ok


def main() -> int:
    parser = argparse.ArgumentParser(description="media45: Offline-Prüfung der Sicherheitskonfiguration (ohne Geheimnisausgabe)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    errors, warnings, ok = assess(args.root)
    print("media45 – lokaler Sicherheitscheck (offline, nur lesend)")
    for message in ok:
        print("[OK]", message)
    for message in warnings:
        print("[HINWEIS]", message)
    for message in errors:
        print("[FEHLER]", message)
    print(f"Ergebnis: {len(errors)} Fehler, {len(warnings)} Hinweise")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
