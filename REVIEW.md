# PV Monitor — Code & Konfiguration Review

> Erstellt: 2026-07-12  
> Reviewer: Hermes (claude-sonnet-4-6)  
> Zweck: Gesamtübersicht für manuelles oder AI-gestütztes Code-Review

---

## 1. Projektstruktur

```
pv-monitor/
├── backend/
│   ├── main.py            # FastAPI + SQLite + aiohttp (930 Zeilen)
│   ├── requirements.txt   # 3 Dependencies
│   └── Dockerfile
├── frontend/
│   └── index.html         # SPA: Tailwind CSS + Chart.js (1060 Zeilen)
├── Caddyfile              # Reverse Proxy: Port 8090
├── docker-compose.yml     # 2 Services: backend + caddy
├── .env                   # Secrets (nicht im Review)
└── .env.example           # Vorlage
```

**Stack:** FastAPI · SQLite3 · aiohttp · Caddy · Docker Compose · Tailwind CSS · Chart.js

---

## 2. Backend (`backend/main.py`)

### 2.1 Konfiguration (ENV → Laufzeit)

| Variable | Bedeutung | Default |
|---|---|---|
| `SHELLY_AUTH_KEY` | Shelly Cloud API-Key | `""` |
| `SHELLY_CLOUD_SERVER` | Cloud-Endpunkt | `https://shelly-46-eu.shelly.cloud` |
| `SHELLY_{n}_ID` | Cloud-Geräte-ID (n = 1–9) | – |
| `SHELLY_{n}_NAME` | Anzeigename | `"Gerät n"` |
| `SHELLY_{n}_INBETRIEBNAHME` | Datum YYYY-MM-DD | heute |
| `DB_PATH` | SQLite-Pfad | `/app/data/pv.db` |
| `POLL_INTERVAL` | Abfrageintervall (Sekunden) | `60` |

> ⚠️ `.env.example` enthält noch `SHELLY_1_IP` / `SHELLY_2_IP` / `STROMPREIS_CENT` / `ANLAGE_x_KOSTEN` — diese Felder werden im aktuellen `main.py` **nicht mehr ausgewertet**. Die .env.example ist veraltet.

### 2.2 Datenbankschema (SQLite)

```sql
readings (id, device_id, ts, power_w, total_wh, online)
    INDEX: device_id, ts

production_offset (id, device_id UNIQUE, offset_kwh, beschreibung, updated_at)

investments (id, device_id, betrag, datum, beschreibung, created_at)

strompreise (id, preis_cent, gueltig_ab, gueltig_bis, beschreibung, created_at)
    DEFAULT: 28.0 ct/kWh ab 2024-01-01
```

### 2.3 Shelly Cloud Polling

- **Intervall:** `POLL_INTERVAL` Sekunden (Default: 60s)
- **Multi-Device:** asyncio.gather über alle konfigurierten Geräte
- **Fehlerbehandlung:** Timeout / ClientError / Exception → `online=False`, `total_wh=NULL`
- **Shelly-Generationen:**
  - Gen1: `meters[0].power` / `.total` (Watt-Minuten!)
  - Gen2 EM: `emeters[0].power` / `.total`
  - Gen2 Pro (switch:0): `apower` / `aenergy.total`
  - Fallback: `apower` direkt auf `device_status`

> ⚠️ **Einheit-Bug-Risiko:** `total_wh` wird durch `60000` geteilt um kWh zu erhalten — das ist korrekt für **Watt-Minuten** (Gen1 EM), aber **falsch für echte Wh** (Gen2). Gen2-Geräte liefern `total` in Wh → Division durch 60000 ergibt zu kleine Werte (Faktor 1000 zu klein). Zu prüfen: Welche Geräte sind im Einsatz?

### 2.4 Berechnungslogik

#### Energieberechnung (total_wh)
```
_today_energy_kwh:   (MAX - MIN) / 60000   [für heute]
_total_energy_kwh:   offset_kwh + (MAX - MIN) / 60000   [gesamt]
_avg_daily_kwh_30d:  Ø(MAX-MIN pro Tag) / 60000   [letzte 30 Tage]
```

> ⚠️ `MAX - MIN` Methode funktioniert nur wenn `total_wh` **monoton steigt** und bei Mitternacht **nicht zurückgesetzt** wird. Wenn Shelly den Counter täglich zurücksetzt: Berechnung ist korrekt. Wenn Counter nie zurücksetzt: Berechnung über Tagesgrenzen liefert Unsinn.

#### Ersparnis mit historischen Preisen (`_total_savings_eur`)
- Iteriert über alle Preisperioden
- Energie pro Periode: MAX-MIN innerhalb des Zeitraums
- Offset wird zur ersten (ältesten) Periode addiert
- Rundet auf 2 Dezimalstellen

> ⚠️ Wenn eine Preisperiode **keinen DB-Eintrag** enthält (z.B. vor Monitoring-Start), ist die Energie = 0 — korrekt, da Offset nur in Periode 1 addiert wird.

#### Break-Even Berechnung
```
avg_daily_eur = (total_kwh / days_since) × strompreis
days_to_breakeven = (kosten - savings_eur) / avg_daily_eur
```
> ℹ️ Sinnvoll als Näherung. Saisonschwankungen werden nicht berücksichtigt.

### 2.5 API Endpoints

| Method | Path | Beschreibung |
|---|---|---|
| GET | `/health` | Healthcheck (Geräteanzahl) |
| GET | `/api/status` | Aktueller Status aller Geräte |
| GET | `/api/chart/24h` | Stundenmittelwerte letzte 24h |
| GET | `/api/chart/30days` | Tagesenergie letzte 30 Tage |
| GET | `/api/amortisation` | Amortisationsstand pro Gerät |
| GET | `/api/projection` | Jahresprojektion (30-Tage-Basis) |
| GET | `/api/strompreise` | Alle Strompreise |
| POST | `/api/strompreise` | Neuen Preis hinzufügen |
| DELETE | `/api/strompreise/{id}` | Preis löschen (mind. 1 bleibt) |
| GET | `/api/investments` | Alle Investitionen |
| POST | `/api/investments` | Neue Investition |
| DELETE | `/api/investments/{id}` | Investition löschen |
| GET | `/api/production-offset` | Historische Offsets |
| POST | `/api/production-offset` | Offset setzen/überschreiben |

> ℹ️ Kein Auth auf keinem Endpoint. Caddy könnte BasicAuth vorschalten (derzeit nicht konfiguriert).

### 2.6 Bekannte Issues / Risiken

| # | Schwere | Beschreibung |
|---|---|---|
| 1 | 🔴 HIGH | `total_wh / 60000` — Einheit unklar: Gen2 liefert Wh (nicht Wmin) → Faktor 1000 falsch |
| 2 | 🟡 MED | Kein API-Auth: Jeder im Netz kann Investitionen/Preise ändern |
| 3 | 🟡 MED | `datetime.utcnow()` deprecated in Python 3.12+ → `datetime.now(timezone.utc)` |
| 4 | 🟡 MED | SQLite `check_same_thread=False` + kein Connection-Pool → bei Last race conditions möglich |
| 5 | 🟢 LOW | `.env.example` stimmt nicht mit aktuellem Code überein (veraltete Felder) |
| 6 | 🟢 LOW | Polling-Loop hat kein exponential backoff bei dauerhaftem API-Fehler |
| 7 | 🟢 LOW | `CORS allow_origins=["*"]` — in Produktion sollte auf eigene Domain eingeschränkt werden |

---

## 3. Frontend (`frontend/index.html`)

### 3.1 Architektur
- **Single-Page-App**, kein Build-Step, kein Framework
- **CDN:** Tailwind CSS + Chart.js (kein lokaler Asset-Server nötig)
- **Auto-Refresh:** alle 30 Sekunden via `setInterval`
- **Dark Mode:** Tailwind `darkMode: 'class'` (immer dark, kein Toggle)

### 3.2 Seiten / Tabs
1. **Dashboard:** Status-Karten → 24h-Liniendiagramm → 30-Tage-Balken → Amortisation → Jahresprojektion
2. **Einstellungen:** Strompreise (CRUD) · Investitionen (CRUD) · Historischer Offset

### 3.3 Sicherheit Frontend
- XSS-Schutz: `escHtml()` Funktion vorhanden und verwendet ✅
- Keine sensiblen Daten im Frontend gespeichert ✅
- API-Base relativ (`/api/...`) — kein hardcodierter Host ✅

---

## 4. Infrastruktur

### docker-compose.yml
```yaml
services:
  backend:   # FastAPI, Port intern 8000, DB-Volume
  caddy:     # Reverse Proxy, Port 8090 extern

volumes: db-data, caddy-data, caddy-config
networks: internal (bridge, kein externes Expose des Backends)
```

### Caddyfile
```
:8090 {
    handle /api/*  → reverse_proxy backend:8000
    handle         → static /srv/frontend (SPA fallback)
}
```
> ℹ️ Kein HTTPS konfiguriert (lokales Netz). Kein Auth. Kein Rate-Limiting.

---

## 5. Dependencies

```
fastapi==0.111.0
uvicorn[standard]==0.30.1
aiohttp==3.9.5
```

> ℹ️ Sehr schlanke Dependency-Liste. Stand 2026-07: fastapi 0.111.0 ist nicht mehr aktuell (aktuell ~0.115.x). Kein `pydantic>=2` explizit gepinnt — FastAPI 0.111 bringt Pydantic v2 mit.

---

## 6. Offene Fragen für Review

1. **Einheit `total_wh`:** Welches Shelly-Modell ist "Öfner Außen"? Gen1 EM (Watt-Minuten) oder Gen2 (Wh)? → Entscheidet ob Division durch 60000 korrekt ist.
2. **Counter-Reset:** Setzt Shelly den `total`-Counter täglich um Mitternacht zurück oder akkumuliert er seit Reset?
3. **Auth:** Soll die Einstellungsseite passwortgeschützt werden?
4. **HTTPS:** Ist externer Zugriff geplant? Wenn ja: TLS in Caddy aktivieren.

---

*Ende des Reviews*
