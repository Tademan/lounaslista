import requests
import datetime
import sys
import urllib3
import json
import locale
import os
import time
import re
import ssl
import hashlib
import secrets
from bs4 import BeautifulSoup

try:
    import websocket
    NOSTR_AVAILABLE = True
except ImportError:
    NOSTR_AVAILABLE = False

# --- GLOBAALIT ASETUKSET ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
}

# Kiinteä 64-merkkinen hex-yksityisavain.
# Varmistaa, että uudet ajot korvaavat saman päivän vanhat listat Nostrissa (created_at perusteella).
BOT_PRIV_KEY = os.environ.get(
    "BOT_PRIV_KEY",
    "9814ba0c089136a88dc591dccce66df9791314cc836d3d900b25f6b91b2d0025"
)

def aseta_suomi_lokaali():
    for loc in ['fi_FI.UTF-8', 'fi_FI', 'Finnish']:
        try:
            locale.setlocale(locale.LC_TIME, loc)
            return True
        except locale.Error:
            continue
    return False

# --- PUHDAS PYTHON BIP-340 (SECP256K1 SCHNORR) NOSTRIA VARTEN ---

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

def _point_add(P1, P2):
    if P1 is None: return P2
    if P2 is None: return P1
    x1, y1 = P1
    x2, y2 = P2
    if x1 == x2 and y1 != y2: return None
    if x1 == x2:
        lam = (3 * x1 * x1 * pow(2 * y1, _P - 2, _P)) % _P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, _P - 2, _P)) % _P
    x3 = (lam * lam - x1 - x2) % _P
    return (x3, (lam * (x1 - x3) - y1) % _P)

def _point_mul(P, n):
    R = None
    for i in range(256):
        if (n >> i) & 1:
            R = _point_add(R, P)
        P = _point_add(P, P)
    return R

def _tagged_hash(tag, msg):
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()

def nostr_pubkey_hex(privkey_bytes):
    d0 = int.from_bytes(privkey_bytes, 'big')
    P = _point_mul((_GX, _GY), d0)
    return P[0].to_bytes(32, 'big').hex()

def nostr_sign_event(privkey_bytes, event_id_hex):
    msg = bytes.fromhex(event_id_hex)
    d0 = int.from_bytes(privkey_bytes, 'big')
    P = _point_mul((_GX, _GY), d0)
    d = d0 if (P[1] % 2 == 0) else _N - d0
    aux = secrets.token_bytes(32)
    t = (d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux), 'big')).to_bytes(32, 'big')
    k0 = int.from_bytes(_tagged_hash("BIP0340/nonce", t + P[0].to_bytes(32, 'big') + msg), 'big') % _N
    R = _point_mul((_GX, _GY), k0)
    k = k0 if (R[1] % 2 == 0) else _N - k0
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", R[0].to_bytes(32, 'big') + P[0].to_bytes(32, 'big') + msg), 'big') % _N
    sig = R[0].to_bytes(32, 'big') + ((k + e * d) % _N).to_bytes(32, 'big')
    return sig.hex()

# --- RAVINTOLAKIRJASTOT ---

def hae_herkkuhetki():
    import base64
    url = "https://herkkuhetkitali.fi/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        if res.status_code != 200: return [f"Yhteys estettiin (Virhe {res.status_code})"]
        res.encoding = 'utf-8'
        html = res.text
        try:
            from zoneinfo import ZoneInfo
            tanaan = datetime.datetime.now(ZoneInfo("Europe/Helsinki")).date()
        except ImportError:
            tanaan = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).date()

        pvm_vaihtoehdot = [
            f"{tanaan.day}.{tanaan.month}.{tanaan.year}",
            tanaan.strftime("%d.%m.%Y"),
            f"{tanaan.day}.{tanaan.month}.",
            tanaan.strftime("%d.%m.")
        ]
        viikonpaivat = ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai"]
        vp_tanaan = viikonpaivat[tanaan.weekday()] if tanaan.weekday() < 5 else None
        
        menu_data = ""
        if 'weeklyMenu' in html and 'dayName' in html: menu_data = html
        else:
            b64_matches = re.findall(r"base64,([^\"'\s>]+)", html)
            for b64 in b64_matches:
                b64_clean = re.sub(r'\s+', '', b64)
                b64_clean += "=" * ((4 - len(b64_clean) % 4) % 4)
                try:
                    decoded = base64.b64decode(b64_clean).decode('utf-8')
                    if 'weeklyMenu' in decoded:
                        menu_data = decoded
                        break
                except: continue

        if not menu_data:
            js_files = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html)
            for js_url in js_files:
                if not js_url.startswith('http'): js_url = "https://herkkuhetkitali.fi" + js_url
                try:
                    js_res = requests.get(js_url, headers=HEADERS, timeout=5, verify=False)
                    if 'weeklyMenu' in js_res.text:
                        menu_data = js_res.text
                        break
                except: continue

        if not menu_data: return []

        def korjaa_teksti(teksti):
            t = teksti.replace('\\/', '/')
            try: t = t.encode().decode('unicode-escape')
            except: pass
            try: t = t.encode('latin-1').decode('utf-8')
            except: pass
            return t.strip()

        days_data = re.split(r'\{id:\s*\d+\s*,\s*dayName:', menu_data)
        for i, day_block in enumerate(days_data):
            if i == 0: continue
            date_match = re.search(r'date\s*:\s*"([^"]+)"', day_block)
            lohko_pvm = date_match.group(1) if date_match else ""
            dayname_match = re.search(r'^\s*"([^"]+)"', day_block)
            lohko_vp = dayname_match.group(1) if dayname_match else ""
            
            is_today = any(pvm == lohko_pvm for pvm in pvm_vaihtoehdot)
            if not is_today and vp_tanaan and vp_tanaan.lower() in lohko_vp.lower(): is_today = True
                
            if is_today:
                tulos = []
                sections = re.findall(r'title\s*:\s*"([^"]+)"\s*,\s*items\s*:\s*\[(.*?)\]', day_block)
                for title, items_raw in sections:
                    title_clean = korjaa_teksti(title)
                    tulos.append(f"<strong>{title_clean}</strong>")
                    ruoat = re.findall(r'"([^"]+)"', items_raw)
                    for r in ruoat: tulos.append(f"• {korjaa_teksti(r)}")
                return tulos
        return []
    except Exception as e:
        print(f"Virhe Herkkuhetken haussa: {e}")
        return []

def hae_tellus():
    url = "https://www.compass-group.fi/ravintolat-ja-ruokalistat/foodco/kaupungit/helsinki/tellus/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(res.text, 'html.parser')
        script = soup.find('script', id='restaurant-structured-data')
        if not script: return []
        data = json.loads(script.string)
        pvm = datetime.date.today().strftime("%Y-%m-%d")
        tulos = []
        paivat = data.get('hasMenu', {}).get('hasMenuSection', [])
        paiva_data = next((p for p in paivat if p.get('validFrom') == pvm), None)
        if not paiva_data: return []
        
        for kategoria in paiva_data.get('hasMenuSection', []):
            kat_nimi = kategoria.get('name', '').strip()
            if kat_nimi: tulos.append(f"<strong>{kat_nimi}</strong>")
            for item in kategoria.get('hasMenuItem', []):
                ruoka = item.get('name', '').strip()
                desc = item.get('description', '').strip()
                diets = f" ({desc})" if desc else ""
                tulos.append(f"• {ruoka}{diets}")
        return tulos
    except: return []

def hae_por():
    url = "https://por.fi/menu/"
    try:
        aseta_suomi_lokaali()
        fmt = "%#d.%#m." if os.name == 'nt' else "%-d.%-m."
        haku = f"{datetime.date.today().strftime('%A').capitalize()} {datetime.date.today().strftime(fmt)}"
        res = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        res.raise_for_status() 
        soup = BeautifulSoup(res.text, 'html.parser')
        tag = next((t for t in soup.find_all('strong') if t.get_text(strip=True).startswith(haku)), None)
        if not tag: return [f"Virhe: Kuluvan päivän otsikkoa '{haku}' ei löytynyt."]
            
        p_tag = tag.parent
        for br in p_tag.find_all('br'): br.replace_with("||")
        kaikki_rivit = [r.strip() for r in p_tag.get_text().split("||") if r.strip()]
        
        tulos = []
        kerataan = False
        viikonpaivat = ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai", "Lauantai", "Sunnuntai"]
        for rivi in kaikki_rivit:
            if rivi.startswith(haku):
                kerataan = True
                continue
            elif kerataan and any(rivi.startswith(vp) for vp in viikonpaivat):
                break
            if kerataan: tulos.append(f"• {rivi}")
                
        if not tulos: return ["Virhe: Otsikko löytyi, mutta ruokia ei."]
        return tulos
    except Exception as e: return [f"Virhe listan haussa: {e}"]

def hae_factory():
    url = "https://ravintolafactory.com/lounasravintolat/ravintolat/helsinki-pitajanmaki/"
    try:
        aseta_suomi_lokaali()
        fmt = "%A %#d.%#m.%Y" if os.name == 'nt' else "%A %-d.%-m.%Y"
        haku = datetime.date.today().strftime(fmt).capitalize()
        res = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(res.text, 'html.parser')
        h3 = next((t for t in soup.find_all('h3') if t.get_text(strip=True) == haku), None)
        if not h3: return []
        p_tag = h3.find_next_sibling('p')
        for br in p_tag.find_all('br'): br.replace_with("||")
        return [f"• {r.strip()}" for r in p_tag.get_text().split("||") if r.strip()]
    except: return []

def hae_antell():
    url = "https://www.antell.fi/lounas/helsinki/kuohu/"
    try:
        paivat = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        idx = datetime.date.today().weekday()
        if idx > 4: return []
        res = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        soup = BeautifulSoup(res.text, 'html.parser')
        paneeli = soup.find('section', id=f"panel-{paivat[idx]}")
        if not paneeli: return []
        
        tulos = []
        nappulat = paneeli.find_all('button', class_='accordion__button')
        for btn in nappulat: tulos.append(f"• {btn.get_text(strip=True)}")
        return tulos
    except: return []

def hae_faundori():
    url = "https://ravintolapalvelut.iss.fi/ravintola-faundori?lang=fi"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(res.text, 'html.parser')
        pvm = datetime.date.today().strftime("%Y-%m-%d")
        div = soup.find('div', class_='day_menu_container', attrs={'data-date': pvm})
        if not div: return []
        tulos, cat = [], ""
        for elem in div.find_all(['div', 'h3']):
            if elem.name == 'h3' or 'name_price_container' in elem.get('class', []):
                txt = elem.get_text(strip=True).replace('€', '').strip()
                if txt and not txt[0].isdigit(): cat = txt
            if 'menu_item_container' in elem.get('class', []):
                nimi = elem.find('span', class_='menu_item_name')
                if nimi: tulos.append(f"<b>[{cat}]</b> {nimi.get_text(strip=True)}" if cat else f"• {nimi.get_text(strip=True)}")
        return tulos
    except: return []

def hae_lasihelmi():
    url = "https://www.compass-group.fi/ravintolat-ja-ruokalistat/foodco/kaupungit/helsinki/lasihelmi/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(res.text, 'html.parser')
        script = soup.find('script', id='restaurant-structured-data')
        data = json.loads(script.string)
        pvm = datetime.date.today().strftime("%Y-%m-%d")
        menu = next((s.get('hasMenuItem', []) for s in data.get('hasMenu', {}).get('hasMenuSection', []) if s.get('validFrom') == pvm), None)
        return [f"• {item.get('name', '').strip()}" for item in menu if item.get('name')]
    except: return []

# --- HTML GENEROINTI ---

def luo_html_raportti(data_lista, pvm):
    ajoaika = datetime.datetime.now().strftime("%H:%M:%S")
    html_template = f"""<!DOCTYPE html>
<html lang="fi">
<head>
    <meta charset="UTF-8">
    <title>Lounas Pitäjänmäki {pvm}</title>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; margin: 0; padding: 20px; }}
        h1 {{ text-align: center; color: #1a1a1a; margin-bottom: 5px; }}
        .timestamp {{ text-align: center; color: #666; font-size: 0.9em; margin-bottom: 30px; font-style: italic; }}
        .grid {{ display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; }}
        .card {{ background: white; width: calc(25% - 20px); min-width: 250px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); display: flex; flex-direction: column; overflow: hidden; }}
        .card-header {{ background: #2c3e50; color: white; padding: 15px; position: relative; }}
        .card-header h2 {{ margin: 0; font-size: 1.1em; padding-right: 60px; }}
        .copy-btn {{ position: absolute; right: 10px; top: 50%; transform: translateY(-50%); background: #34495e; color: white; border: 1px solid #ffffff44; padding: 5px 10px; border-radius: 4px; cursor: pointer; font-size: 0.75em; }}
        .copy-btn:hover {{ background: #1abc9c; }}
        .card-content {{ padding: 15px; flex-grow: 1; font-size: 0.85em; color: #444; }}
        ul {{ list-style: none; padding: 0; margin: 0; }}
        li {{ margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid #f8f8f8; }}
        strong {{ color: #d35400; display: block; margin-top: 8px; }}
        .card-footer {{ padding: 10px; background: #fafafa; text-align: center; border-top: 1px solid #eee; }}
        .card-footer a {{ font-size: 0.8em; color: #3498db; text-decoration: none; font-weight: bold; }}
        @media (max-width: 1200px) {{ .card {{ width: calc(33.33% - 20px); }} }}
        @media (max-width: 900px) {{ .card {{ width: calc(50% - 20px); }} }}
        @media (max-width: 600px) {{ .card {{ width: 100%; }} }}
    </style>
    <script>
        function copyToClipboard(id, restaurantName, btn) {{
            const text = document.getElementById(id).innerText;
            const tempInput = document.createElement("textarea");
            tempInput.value = restaurantName + "\\n" + text.replace(/• /g, "");
            document.body.appendChild(tempInput);
            tempInput.select();
            document.execCommand("copy");
            document.body.removeChild(tempInput);
            const originalText = btn.innerText;
            btn.innerText = "Kopioitu!";
            setTimeout(() => btn.innerText = originalText, 2000);
        }}
    </script>
</head>
<body>
    <div style="text-align: center; margin-top: 20px;">
        <a href="https://tademan.github.io/lounaslista/lounaslistat.html" style="font-weight: bold; color: #3498db; text-decoration: none; font-size: 1.1em;">Siirry parempaan versioon &rarr;</a>
    </div>
    <h1>🍴 Lounaslistat Pitäjänmäki {pvm}</h1>
    <div class="timestamp">Päivitetty (skripti ajettu): {pvm} klo {ajoaika}</div>
    <div class="grid">"""

    for i, r in enumerate(data_lista):
        safe_id = f"menu-{i}"
        rivit_html = "".join([f"<li>{rivi}</li>" for rivi in r["rivit"]]) if r["rivit"] else "<li style='color:gray'>Ei listaa saatavilla.</li>"
        safe_name = r['nimi'].replace("'", "\\'")
        
        html_template += f"""
        <div class="card">
            <div class="card-header">
                <h2>{r['nimi']}</h2>
                <button class="copy-btn" onclick="copyToClipboard('{safe_id}', '{safe_name}', this)">Kopioi</button>
            </div>
            <div class="card-content" id="{safe_id}"><ul>{rivit_html}</ul></div>
            <div class="card-footer"><a href="{r['url']}" target="_blank">Lähde &rarr;</a></div>
        </div>"""

    html_template += """
    </div>
</body>
</html>"""
    
    with open("lounas.html", "w", encoding="utf-8") as f:
        f.write(html_template)
    return os.path.abspath("lounas.html")

# --- NOSTR GENEROINTI ---

def julkaise_nostriin(data_lista, pvm_str):
    if not NOSTR_AVAILABLE:
        print("\nVAROITUS: 'websocket-client'-kirjastoa ei löydy. Nostr-julkaisu ohitettu.")
        print("Asenna ajaen: pip install websocket-client")
        return

    print("\nJulkaistaan ruokalistat Nostr-verkkoon Lounas Laituria varten...")
    
    privkey_bytes = bytes.fromhex(BOT_PRIV_KEY.strip())
    pubkey = nostr_pubkey_hex(privkey_bytes)
    print(f"Botin julkinen avain (pubkey): {pubkey}")

    relays = [
        'wss://nos.lol',
        'wss://relay.damus.io',
        'wss://offchain.pub',
        'wss://relay.primal.net'
    ]

    try:
        d, m, y = pvm_str.split('.')
        iso_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    except ValueError:
        iso_date = datetime.date.today().strftime("%Y-%m-%d")

    event_viestit = []
    perus_aika = int(time.time())

    for idx, r in enumerate(data_lista):
        puhdas_nimi = r['nimi'].replace(" (tarjotin saatavilla)", "").strip()
        
        tags = [
            ['t', 'lounaslaituri-menu-v1'],
            ['restaurant', puhdas_nimi],
            ['date', iso_date],
            ['source', r['url']]
        ]

        puhtaat_rivit = []
        for rivi in r['rivit']:
            puhdas = re.sub(r'<[^>]+>', '', rivi).replace('• ', '').strip()
            if puhdas:
                tags.append(['item', puhdas])
                puhtaat_rivit.append(puhdas)
        
        if not puhtaat_rivit or (len(puhtaat_rivit) == 1 and puhtaat_rivit[0].startswith("Virhe")):
            continue
        
        content = f"Lounas: {puhdas_nimi} ({iso_date})\n\n" + "\n".join(f"- {rivi}" for rivi in puhtaat_rivit)

        # Eriytetään aikaleimat sekunnilla, jotta relayt eivät hylkää samanaikaisia viestejä
        created_at = perus_aika + idx

        commitment = json.dumps(
            [0, pubkey, created_at, 1, tags, content],
            separators=(',', ':'),
            ensure_ascii=False
        )
        event_id = hashlib.sha256(commitment.encode('utf-8')).hexdigest()
        sig = nostr_sign_event(privkey_bytes, event_id)

        event_obj = {
            "id": event_id,
            "pubkey": pubkey,
            "created_at": created_at,
            "kind": 1,
            "tags": tags,
            "content": content,
            "sig": sig
        }
        event_viestit.append((puhdas_nimi, event_id, json.dumps(["EVENT", event_obj], ensure_ascii=False)))

    if not event_viestit:
        print(" -> Ei julkaistavia listoja.")
        return

    ssl_opts = {"cert_reqs": ssl.CERT_NONE} if hasattr(ssl, "CERT_NONE") else {}
    ws_headers = ["User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"]

    for relay_url in relays:
        try:
            ws = websocket.create_connection(relay_url, timeout=8, sslopt=ssl_opts, header=ws_headers)
            ws.settimeout(4.0)
            hyvaksytyt = 0
            
            for ravintola_nimi, ev_id, msg in event_viestit:
                ws.send(msg)
                try:
                    while True:
                        vastaus_raw = ws.recv()
                        vastaus = json.loads(vastaus_raw)
                        if vastaus[0] == "OK" and vastaus[1] == ev_id:
                            if vastaus[2] is True:
                                hyvaksytyt += 1
                            else:
                                print(f"    [!] {relay_url} hylkäsi ({ravintola_nimi}): {vastaus[3]}")
                            break
                        elif vastaus[0] == "NOTICE":
                            print(f"    [Notice] {relay_url}: {vastaus[1]}")
                except Exception:
                    pass
                time.sleep(0.25)

            ws.close()
            print(f" -> {relay_url}: Hyväksytty {hyvaksytyt}/{len(event_viestit)} listaa.")
        except Exception as e:
            print(f" -> Virhe yhdistettäessä relaylle {relay_url}: {e}")

# --- PÄÄOHJELMA ---

def aja_haku():
    os.system('cls' if os.name == 'nt' else 'clear')
    tanaan = datetime.date.today().strftime('%d.%m.%Y')
    print(f"PÄIVITETÄÄN LOUNASLISTOJA ({tanaan})...\n")
    
    ravintolat = [
        ("Tellus", hae_tellus, "https://www.compass-group.fi/ravintolat-ja-ruokalistat/foodco/kaupungit/helsinki/tellus/"),
        ("POR", hae_por, "https://por.fi/menu/"),
        ("Factory", hae_factory, "https://ravintolafactory.com/lounasravintolat/ravintolat/helsinki-pitajanmaki/"),
        ("Antell", hae_antell, "https://www.antell.fi/lounas/helsinki/kuohu/"),
        ("Herkkuhetki", hae_herkkuhetki, "https://herkkuhetkitali.fi/"),
        ("Faundori", hae_faundori, "https://ravintolapalvelut.iss.fi/ravintola-faundori?lang=fi"),
        ("Lasihelmi", hae_lasihelmi, "https://www.compass-group.fi/ravintolat-ja-ruokalistat/foodco/kaupungit/helsinki/lasihelmi/")
    ]

    keratty_data = []
    for nimi, fn, url in ravintolat:
        print(f" -> Haetaan {nimi}...")
        nayton_nimi = nimi
        if nimi in ["Tellus", "POR", "Lasihelmi"]:
            nayton_nimi += " (tarjotin saatavilla)"
            
        keratty_data.append({"nimi": nayton_nimi, "url": url, "rivit": fn()})

    polku = luo_html_raportti(keratty_data, tanaan)
    print(f"\nVALMIS! HTML-tiedosto luotu: {polku}")
    
    # Nostr-päivitys
    julkaise_nostriin(keratty_data, tanaan)
    
    print("\nYksittäiset listat voit nyt kopioida suoraan selaimesta 'Kopioi'-napilla.")

if __name__ == "__main__":
    aja_haku()
