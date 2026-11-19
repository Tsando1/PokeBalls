import os
import re
import json
import time
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import requests

# Inizializzazione Firebase Admin
import firebase_admin
from firebase_admin import credentials, firestore

if not firebase_admin._apps:
    cred_dict = None

    # 1. Da GitHub Secret FIREBASE_SERVICE_ACCOUNT
    secret_val = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if secret_val:
        try:
            cred_dict = json.loads(secret_val)
        except Exception as e:
            print(f"Errore parsing JSON da FIREBASE_SERVICE_ACCOUNT: {e}")

    # 2. Cerca in qualsiasi variabile d'ambiente con JSON
    if not cred_dict:
        for val in os.environ.values():
            val_s = str(val).strip()
            if val_s.startswith("{") and "private_key" in val_s and "project_id" in val_s:
                try:
                    cred_dict = json.loads(val_s)
                    break
                except Exception:
                    pass

    # 3. File locale
    if cred_dict:
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase inizializzato correttamente da variabile d'ambiente.")
    elif os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
        print("Firebase inizializzato da serviceAccountKey.json.")
    else:
        raise RuntimeError("Credenziali Firebase non trovate! Configura il secret FIREBASE_SERVICE_ACCOUNT su GitHub.")

db = firestore.client()

LANG_CODE_TO_NAME = {
    "1": "English",
    "2": "French",
    "3": "German",
    "4": "Spanish",
    "5": "Italian",
    "7": "Japanese",
    "10": "Korean"
}

NAME_TO_CODE = {
    "english": "1",
    "french": "2",
    "german": "3",
    "spanish": "4",
    "italian": "5",
    "japanese": "7",
    "korean": "10",
    "en": "1",
    "fr": "2",
    "de": "3",
    "es": "4",
    "it": "5",
    "jp": "7",
    "kr": "10"
}

def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def build_filtered_url(base_url, language_val, is_sealed):
    clean_url = base_url.split("#")[0].strip()
    sep = "&" if "?" in clean_url else "?"
    params = []

    lang_id = None
    if language_val:
        l_str = str(language_val).strip().lower()
        if l_str in NAME_TO_CODE:
            lang_id = NAME_TO_CODE[l_str]
        elif l_str in LANG_CODE_TO_NAME:
            lang_id = l_str
        elif l_str.isdigit():
            lang_id = l_str

    if lang_id and "language=" not in clean_url:
        params.append(f"language={lang_id}")
    if not is_sealed and "minCondition=" not in clean_url:
        params.append("minCondition=2")

    if not params:
        return clean_url
    return f"{clean_url}{sep}{'&'.join(params)}"

def send_discord_alert(webhook_url, item_title, item_url, image_url, offer, target_price):
    if not webhook_url or not webhook_url.startswith("http"):
        return
    embed = {
        "title": f"🔥 Offerta sotto soglia: {item_title}",
        "url": item_url,
        "color": 3066993,
        "fields": [
            {"name": "Prezzo Trovato", "value": f"**{offer['price']:.2f} €**", "inline": True},
            {"name": "Soglia Impostata", "value": f"{target_price:.2f} €", "inline": True},
            {"name": "Venditore", "value": offer.get("seller", "N/D"), "inline": True},
            {"name": "Dettagli", "value": f"{offer.get('condition', '')} | {offer.get('language', '')}".strip(" |"), "inline": False}
        ],
        "footer": {"text": "Cardmarket Cloud Tracker • GitHub Actions"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        print(f"  [DISCORD] Notifica inviata per {item_title} a {offer['price']:.2f} € (status: {resp.status_code})")
    except Exception as e:
        print(f"  [DISCORD ERROR] Errore invio Discord: {e}")

def fetch_html_stealth(target_url, driver=None):
    """
    Doppio motore di scaricamento:
    1. curl_cffi con impersonazione Chrome (velocissimo, < 1 secondo)
    2. Fallback su SeleniumBase UC-Mode (Undetected Chrome con bypass Cloudflare)
    """
    # Motore 1: curl_cffi
    try:
        from curl_cffi import requests as cf_requests
        resp = cf_requests.get(
            target_url,
            impersonate="chrome120",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            },
            timeout=20
        )
        if resp.status_code == 200 and "Please login to continue" not in resp.text and "Just a moment" not in resp.text:
            return resp.text
    except Exception as e:
        print(f"  [Engine 1] curl_cffi: {e}")

    # Motore 2: SeleniumBase UC Mode (Undetected Chrome)
    if driver:
        try:
            print("  [Engine 2] Caricamento con SeleniumBase UC Mode...")
            driver.get(target_url)
            driver.sleep(5)  # Attesa per eventuale challenge JS di Cloudflare
            html = driver.get_page_source()
            if "article-row" in html or "h1" in html:
                return html
        except Exception as e:
            print(f"  [Engine 2] Errore SeleniumBase: {e}")

    return None

def check_item(doc_ref, data, driver=None):
    raw_url = (data.get("baseUrl") or data.get("url") or "").strip()
    if not raw_url:
        return "URL mancante"

    title_db = data.get("name") or data.get("title") or "Prodotto Cardmarket"
    target_price = float(data.get("targetPrice") or 0)
    language_val = data.get("langId") or data.get("language") or ""
    
    is_sealed = bool(data.get("isSealed", False)) or any(
        kw in raw_url for kw in ["/Booster", "/Boxes", "/Tins", "/Trainer-Boxes", "/Decks", "/Sets"]
    )
    if "/Singles/" in raw_url:
        is_sealed = False

    target_url = build_filtered_url(raw_url, language_val, is_sealed)
    print(f"\n--> Controllo: {title_db}")
    print(f"    URL: {target_url}")

    html = fetch_html_stealth(target_url, driver)
    if not html:
        return f"ERR {title_db}: Impossibile scaricare la pagina (blocco anti-bot o timeout)"

    soup = BeautifulSoup(html, "html.parser")

    # Estrazione Titolo
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else title_db

    # Estrazione Immagine
    image_url = data.get("imageUrl", "")
    img_tag = soup.select_one("img.is-front, .product-image img, meta[property='og:image']")
    if img_tag:
        if img_tag.name == "meta":
            image_url = img_tag.get("content", image_url)
        else:
            src = img_tag.get("src") or img_tag.get("data-src")
            if src:
                image_url = src if src.startswith("http") else f"https:{src}"

    # Risoluzione lingua attesa
    expected_lang_name = ""
    lang_str = str(language_val).strip().lower()
    if lang_str in NAME_TO_CODE:
        expected_lang_name = LANG_CODE_TO_NAME.get(NAME_TO_CODE[lang_str], "")
    elif lang_str in LANG_CODE_TO_NAME:
        expected_lang_name = LANG_CODE_TO_NAME[lang_str]

    offers = []
    rows = soup.select("div.table-body div.article-row, div.article-row")
    for row in rows:
        price_el = row.select_one(".price-container .color-primary, .col-price .color-primary, .mobile-offer-container .color-primary, span.font-weight-bold.color-primary, .col-price")
        if not price_el:
            continue
        price = parse_price(price_el.get_text(strip=True))
        if price is None:
            continue

        seller_el = row.select_one(".seller-name a, .col-seller a, a.seller-name")
        seller = seller_el.get_text(strip=True) if seller_el else "Venditore"

        cond_el = row.select_one(".article-condition span, .product-attributes a.article-condition span, span.badge")
        condition = cond_el.get("title") or cond_el.get_text(strip=True) if cond_el else ("Sealed" if is_sealed else "NM")

        lang_el = row.select_one(".product-attributes span[data-original-title], .product-attributes span[aria-label], span.icon[data-original-title]")
        lang_name = (lang_el.get("data-original-title") or lang_el.get("aria-label")) if lang_el else ""

        # Filtro lingua
        if expected_lang_name and lang_name:
            if expected_lang_name.lower() not in lang_name.lower():
                continue

        # Filtro condizione (NM/Mint per singoli)
        if not is_sealed and condition:
            cond_upper = condition.upper()
            if not any(valid in cond_upper for valid in ["MT", "NM", "MINT", "NEAR MINT"]):
                continue

        offers.append({
            "price": price,
            "seller": seller,
            "condition": condition,
            "language": lang_name or expected_lang_name or "N/D"
        })

    offers.sort(key=lambda x: x["price"])
    top_10 = offers[:10]
    top10_prices = [round(o["price"], 2) for o in top_10]

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    today_str = now_utc.strftime("%Y-%m-%d")
    formatted_now = now_utc.strftime("%d/%m/%Y %H:%M UTC")

    update_payload = {
        "name": title,
        "title": title,
        "imageUrl": image_url,
        "top10Offers": top10_prices,
        "topOffers": top_10,
        "lastUpdated": formatted_now,
        "lastChecked": now_iso
    }

    if top_10:
        min_price = top_10[0]["price"]
        current_avg = round(sum(top10_prices) / len(top10_prices), 2)
        update_payload["currentMinPrice"] = min_price
        update_payload["currentLowest"] = min_price
        update_payload["currentAvgTop10"] = current_avg

        # Minimo odierno
        existing_daily_min = data.get("dailyMinPrice")
        if data.get("dailyMinDate") == today_str and existing_daily_min is not None:
            daily_min = min(min_price, float(existing_daily_min))
        else:
            daily_min = min_price

        update_payload["dailyMinPrice"] = daily_min
        update_payload["dailyMinDate"] = today_str

        # Storico per grafico Chart.js
        raw_history = data.get("dailyHistory")
        history_map = {}
        if isinstance(raw_history, list):
            for entry in raw_history:
                if isinstance(entry, dict) and "d" in entry and "min" in entry:
                    try:
                        history_map[str(entry["d"])] = float(entry["min"])
                    except (ValueError, TypeError):
                        pass
        elif isinstance(raw_history, dict):
            for d, p in raw_history.items():
                try:
                    history_map[str(d)] = float(p)
                except (ValueError, TypeError):
                    pass

        if today_str in history_map:
            history_map[today_str] = min(history_map[today_str], daily_min)
        else:
            history_map[today_str] = daily_min

        sorted_history = [
            {"d": d, "min": round(history_map[d], 2)}
            for d in sorted(history_map.keys())
        ]
        update_payload["dailyHistory"] = sorted_history

        # Storico Orario ad alta frequenza (per grafici orari dettagliati)
        raw_hourly = data.get("hourlyHistory") or []
        if not isinstance(raw_hourly, list):
            raw_hourly = []
        time_label = now_utc.strftime("%d/%m %H:%M")
        raw_hourly.append({"t": time_label, "p": min_price})
        update_payload["hourlyHistory"] = raw_hourly[-96:]  # Mantiene le ultime 96 rilevazioni (24-48h a intervalli di 15-30m)

        # Notifiche Discord con logica anti-spam
        webhook = data.get("discordWebhook")
        notified_offers = dict(data.get("notifiedOffers") or {})
        notified_changed = False

        if target_price > 0 and webhook:
            for offer in top_10:
                price = offer["price"]
                if price <= target_price:
                    offer_key = f"{offer['seller']}_{offer.get('condition', '')}_{offer.get('language', '')}"
                    last_notified_price = notified_offers.get(offer_key)

                    if last_notified_price is None or price < float(last_notified_price):
                        send_discord_alert(webhook, title, target_url, image_url, offer, target_price)
                        notified_offers[offer_key] = price
                        notified_changed = True

        if notified_changed or "notifiedOffers" not in data:
            update_payload["notifiedOffers"] = notified_offers

        doc_ref.update(update_payload)
        summary = f"OK {title}: {len(top_10)} offerte (Minimo: {min_price:.2f} €, Media Top 10: {current_avg:.2f} €)"
        print(f"    {summary}")
        return summary
    else:
        doc_ref.update(update_payload)
        summary = f"OK {title}: 0 offerte compatibili al momento"
        print(f"    {summary}")
        return summary

def main():
    print("=" * 60)
    print(f"AVVIO CARDMARKET PRICE TRACKER • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    items_to_check = []
    processed_paths = set()

    try:
        for item_doc in db.collection_group("items").stream():
            path = item_doc.reference.path
            if path not in processed_paths:
                processed_paths.add(path)
                items_to_check.append((item_doc.reference, item_doc.to_dict() or {}))
    except Exception as e:
        print(f"Avviso scansione collection_group: {e}")

    try:
        for col in db.collections():
            if col.id != "users":
                for item_doc in col.stream():
                    path = item_doc.reference.path
                    if path not in processed_paths:
                        processed_paths.add(path)
                        items_to_check.append((item_doc.reference, item_doc.to_dict() or {}))
    except Exception as e:
        print(f"Avviso scansione collezioni radice: {e}")

    print(f"Elementi totali trovati nel database: {len(items_to_check)}")
    if not items_to_check:
        print("Nessun prodotto da monitorare. Esco.")
        return

    # Inizializzazione browser Undetected Chrome (SeleniumBase)
    driver = None
    try:
        from seleniumbase import Driver
        print("Inizializzazione browser Undetected Chrome (SeleniumBase)...")
        driver = Driver(uc=True, headless=False)
    except Exception as e:
        print(f"Nota: SeleniumBase non inizializzato ({e}), utilizzo motore curl_cffi.")

    try:
        for idx, (ref, data) in enumerate(items_to_check, 1):
            print(f"\n[{idx}/{len(items_to_check)}]")
            check_item(ref, data, driver)
            time.sleep(3)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    print("\n" + "=" * 60)
    print("CICLO DI CONTROLLO COMPLETATO CON SUCCESSO!")
    print("=" * 60)

if __name__ == "__main__":
    main()
