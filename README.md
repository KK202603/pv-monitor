# ☀️ PV Monitor

Echtzeit-Überwachung deiner Shelly-basierten Photovoltaik-Anlagen – inkl. Energiestatistik, Amortisationsrechner und Jahresprojektion.

## Funktionen

- **Live-Leistungsanzeige** (Watt) pro Anlage
- **Tagesenergie & Ersparnis** (kWh / EUR)
- **24h Liniendiagramm** – Leistungsverlauf stündlich
- **30-Tage Balkendiagramm** – tägliche Energiemenge gestapelt
- **Amortisationsrechner** – Fortschrittsbalken + Break-Even-Datum
- **Jahresprojektion** – Hochrechnung auf Basis der letzten 30 Tage
- **PWA-ready** – auf dem Homescreen installierbar
- **Auto-Refresh** alle 30 Sekunden
- **Dark Theme**, systemfonts, kein Tracking

## Stack

| Komponente | Technologie |
|---|---|
| Backend | Python 3.12, FastAPI, SQLite3, aiohttp |
| Frontend | Single HTML, Tailwind CSS CDN, Chart.js CDN |
| Reverse Proxy | Caddy v2 (automatisches HTTPS via Let's Encrypt) |
| Deployment | Docker Compose |

---

## 🚀 Schnellstart (5 Schritte)

### 1. Dateien kopieren / Repo klonen

```bash
git clone https://github.com/dein-user/pv-monitor.git
cd pv-monitor
```

### 2. `.env` anpassen

```bash
cp .env.example .env
nano .env   # oder vim / code
```

Wichtige Felder:

```env
SHELLY_1_IP=192.168.1.42       # IP deines ersten Shelly im lokalen Netz
SHELLY_1_NAME=PV Waschhütte    # Anzeigename
SHELLY_1_MODEL=gen2             # gen1 oder gen2

SHELLY_2_IP=192.168.1.43
SHELLY_2_NAME=PV Öfner
SHELLY_2_MODEL=gen2

STROMPREIS_CENT=28              # Aktueller Strompreis in Cent/kWh
ANLAGE_1_KOSTEN=399.00          # Anschaffungskosten Anlage 1
ANLAGE_2_KOSTEN=349.00          # Anschaffungskosten Anlage 2
INBETRIEBNAHME_1=2024-03-15     # Für Amortisationsberechnung
INBETRIEBNAHME_2=2024-06-01
```

### 3. `Caddyfile` anpassen

Ersetze `deine-domain.de` durch deine echte Domain:

```bash
sed -i 's/deine-domain.de/pv.beispiel.de/g' Caddyfile
```

Außerdem die E-Mail-Adresse für Let's Encrypt anpassen:

```
{
    email deine@email.de
}
```

### 4. Starten

```bash
docker compose up -d --build
```

Logs beobachten:

```bash
docker compose logs -f
```

### 5. Browser öffnen

```
https://deine-domain.de
```

Caddy bezieht automatisch ein gültiges TLS-Zertifikat von Let's Encrypt.  
Beim ersten Start kann es 10–30 Sekunden dauern bis HTTPS aktiv ist.

---

## ⚠️ Wichtige Hinweise

### Netzwerkzugang zu den Shellys

Die Shelly-Geräte müssen vom **Docker-Host** aus per HTTP erreichbar sein.  
Das funktioniert in folgenden Szenarien:

| Szenario | Lösung |
|---|---|
| Server & Shellys im **gleichen LAN** | Direkt – einfach IP eintragen |
| **Heimserver** (Raspberry Pi, NAS) im selben Netz | Direkt – kein Aufwand |
| **Cloud-Server** (Hetzner, DigitalOcean, etc.) | Tailscale VPN einrichten (siehe unten) |

### Tailscale für Cloud-Server (empfohlen)

Wenn dein Server in der Cloud läuft und die Shellys zu Hause sind:

```bash
# 1. Tailscale auf dem Server installieren
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# 2. Tailscale auf dem Heimrouter oder einem Heimserver (z.B. Pi)
#    als Exit-Node oder Subnet-Router einrichten
sudo tailscale up --advertise-routes=192.168.1.0/24

# 3. Im Tailscale-Admin die Route freischalten

# 4. Shelly-IPs im .env auf Tailscale-IPs (100.x.x.x) anpassen
```

Alternativ: Shelly Cloud API verwenden (benötigt Internet-Verbindung der Shellys  
und einen Shelly Cloud Account – nicht im Scope dieser App, aber erweiterbar).

### Datenbank & Updates

- Die SQLite-Datenbank liegt im Docker Volume `db-data` und bleibt bei Updates erhalten.
- Beim Update einfach:

```bash
git pull
docker compose up -d --build
```

Die Daten bleiben vollständig erhalten.

### Nur 1 Gerät betreiben

Wenn du nur ein Shelly-Gerät hast, trage nur `SHELLY_1_*` in die `.env` ein und lasse `SHELLY_2_*` weg.  
Das Backend erkennt automatisch, wie viele Geräte konfiguriert sind.

---

## Verzeichnisstruktur

```
pv-monitor/
├── docker-compose.yml      # Service-Orchestrierung
├── Caddyfile               # Reverse Proxy + HTTPS
├── .env.example            # Konfigurationsvorlage
├── .env                    # Deine Konfiguration (nicht einchecken!)
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py             # FastAPI + Polling + SQLite
└── frontend/
    └── index.html          # Single-Page-App
```

## API-Endpunkte

| Endpunkt | Beschreibung |
|---|---|
| `GET /api/status` | Aktueller Gerätestatus (Leistung, Energie heute) |
| `GET /api/chart/24h` | Stündliche Leistungswerte der letzten 24h |
| `GET /api/chart/30days` | Tägliche Energiemengen der letzten 30 Tage |
| `GET /api/amortisation` | Amortisationsstand pro Anlage |
| `GET /api/projection` | Jahresprojektion pro Anlage |
| `GET /health` | Health-Check für Container-Orchestratoren |

---

## Lizenz

MIT – frei verwendbar und anpassbar.
