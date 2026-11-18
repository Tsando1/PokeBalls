import os
import re
import json
import time
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify
from bs4 import BeautifulSoup
import requests

try:
    from curl_cffi import requests as cf_requests
    http_client = cf_requests
except Exception:
    http_client = requests

import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# Inizializzazione automatica Firebase cercando qualsiasi variabile env con le credenziali
if not firebase_admin._apps:
    cred_dict = None
    for val in os.environ.values():
        val_s = str(val).strip()
        if val_s.startswith("{") and "private_key" in val_s and "project_id" in val_s:
            try:
                cred_dict = json.loads(val_s)
                break
            except Exception:
                pass

    if cred_dict:
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    elif os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    else:
        raise RuntimeError("Nessuna variabile d'ambiente JSON Firebase trovata su Render!")

db = firestore.client()

# URL del Worker Cloudflare con Browser Rendering attivo
CF_WORKER_URL = "https://cm-browser.alexandrodellis1102.workers.dev"

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

    # Risoluzione ID lingua Cardmarket
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
        "footer": {"text": "Cardmarket Cloud Tracker"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"Errore invio Discord: {e}")

def check_single_item(doc_ref, data, fallback_webhook=""):
    raw_url = (data.get("baseUrl") or data.get("url") or "").strip()
    if not raw_url:
        return "URL mancante"

    target_price = float(data.get("targetPrice") or 0)
    language_val = data.get("langId") or data.get("language") or ""
    
    # Riconoscimento sealed / carte singole
    is_sealed = bool(data.get("isSealed", False)) or any(
        kw in raw_url for kw in ["/Booster", "/Boxes", "/Tins", "/Trainer-Boxes", "/Decks", "/Sets"]
    )
    if "/Singles/" in raw_url:
        is_sealed = False

    target_url = build_filtered_url(raw_url, language_val, is_sealed)

    # Chiamata al Cloudflare Worker
    try:
        if http_client is cf_requests:
            resp = http_client.get(
                CF_WORKER_URL,
                params={"url": target_url},
                impersonate="chrome120",
                timeout=45
            )
        else:
            resp = requests.get(
                CF_WORKER_URL,
                params={"url": target_url},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=45
            )
    except Exception as e:
        return f"Errore connessione Worker su {target_url}: {e}"

    if resp.status_code == 500 and "Rate limit exceeded" in resp.text:
        return f"Cloudflare Worker rate limit exceeded su {target_url}"

    if resp.status_code != 200:
        return f"HTTP {resp.status_code} dal Worker su {target_url}: {resp.text[:150]}"

    if "Please login to continue" in resp.text:
        return f"Cardmarket blocco/login su {target_url}"

    soup = BeautifulSoup(resp.text, "html.parser")

    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if "Just a moment" in page_title or "Attention Required" in page_title:
        return f"Challenge Cloudflare non superato su {target_url}"

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (data.get("name") or data.get("title") or "Prodotto Cardmarket")

    image_url = data.get("imageUrl", "")
    img_tag = soup.select_one("img.is-front, .product-image img, meta[property='og:image']")
    if img_tag:
        if img_tag.name == "meta":
            image_url = img_tag.get("content", image_url)
        else:
            src = img_tag.get("src") or img_tag.get("data-src")
            if src:
                image_url = src if src.startswith("http") else f"https:{src}"

    # Risoluzione nome lingua attesa
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

        # Filtro condizione (Near Mint / Mint per carte singole)
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
        current_avg = round(sum(p for p in top10_prices) / len(top10_prices), 2)
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

        # Storico giornaliero per Chart.js (array di oggetti [{"d": "YYYY-MM-DD", "min": X.XX}])
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

        # Logica Notifiche Discord Anti-Spam
        webhook = data.get("discordWebhook") or fallback_webhook
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
    if top_10:
        return f"OK {title}: {len(top_10)} offerte (min: {top_10[0]['price']:.2f} €, media top10: {update_payload.get('currentAvgTop10', 0):.2f} €)"
    return f"OK {title}: 0 offerte compatibili trovate"

# Lock per evitare esecuzioni sovrapposte di controlli che bruciano la quota Cloudflare
check_lock = threading.Lock()

def run_all_checks():
    if not check_lock.acquire(blocking=False):
        return ["Controllo già in corso in background, attendi qualche istante prima di riprovare."]
    try:
        results = []
        processed_paths = set()

        def process_doc(doc_ref, doc_data, webhook=""):
            url = (doc_data.get("baseUrl") or doc_data.get("url") or "").strip()
            if not url or "cardmarket.com" not in url:
                return None
            wh = doc_data.get("discordWebhook") or webhook
            return check_single_item(doc_ref, doc_data, wh)

        # 1. Collection Group su 'items' (cattura sia collezione radice che qualsiasi sottocollezione)
        try:
            for item_doc in db.collection_group("items").stream():
                doc_path = item_doc.reference.path
                if doc_path in processed_paths:
                    continue
                processed_paths.add(doc_path)
                res = process_doc(item_doc.reference, item_doc.to_dict() or {})
                if res:
                    results.append(res)
                    time.sleep(20)  # Pausa di 20s: Cloudflare Free permette 1 browser ogni 20 secondi
        except Exception as e:
            print(f"Errore scansione collection_group: {e}")

        # 2. Controllo collezioni radice ed eventuale users/{uid}/items per copertura totale
        try:
            for col in db.collections():
                if col.id == "users":
                    for user_doc in col.stream():
                        u_data = user_doc.to_dict() or {}
                        u_webhook = u_data.get("discordWebhook", "")
                        for subcol in user_doc.reference.collections():
                            for item_doc in subcol.stream():
                                doc_path = item_doc.reference.path
                                if doc_path in processed_paths:
                                    continue
                                processed_paths.add(doc_path)
                                res = process_doc(item_doc.reference, item_doc.to_dict() or {}, u_webhook)
                                if res:
                                    results.append(res)
                                    time.sleep(20)
                else:
                    for item_doc in col.stream():
                        doc_path = item_doc.reference.path
                        if doc_path in processed_paths:
                            continue
                        processed_paths.add(doc_path)
                        res = process_doc(item_doc.reference, item_doc.to_dict() or {})
                        if res:
                            results.append(res)
                            time.sleep(20)
        except Exception as e:
            print(f"Errore scansione collezioni radice: {e}")

        return results
    finally:
        check_lock.release()

def background_worker():
    time.sleep(15)
    while True:
        try:
            print(f"[{datetime.now()}] Avvio ciclo periodico controllo Cardmarket...")
            res = run_all_checks()
            print(f"[{datetime.now()}] Esito ciclo: {res}")
        except Exception as e:
            print(f"Errore nel worker in background: {e}")
        time.sleep(900)

if not os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()

@app.route("/")
def health():
    return jsonify({
        "status": "running",
        "service": "Cardmarket Cloud Price Tracker",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route("/test")
def test_now():
    esito = run_all_checks()
    return jsonify({"trovati_nel_db": len(esito), "esito": esito})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)