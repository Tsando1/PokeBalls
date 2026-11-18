import os
import re
import json
import time
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, jsonify
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

# Inizializzazione automatica Firebase cercando qualsiasi variabile env con le credenziali
if not firebase_admin._apps:
    cred_dict = None
    for val in os.environ.values():
        val_s = val.strip()
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

LANG_MAP = {
    "1": "English",
    "2": "French",
    "3": "German",
    "4": "Spanish",
    "5": "Italian",
    "7": "Japanese"
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

def build_filtered_url(base_url, language_code, is_sealed):
    sep = "&" if "?" in base_url else "?"
    params = []
    if language_code and "language=" not in base_url:
        params.append(f"language={language_code}")
    if not is_sealed and "minCondition=" not in base_url:
        params.append("minCondition=2")
    if not params:
        return base_url
    return f"{base_url}{sep}{'&'.join(params)}"

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

def check_single_item(doc_ref, data, user_webhook):
    raw_url = data.get("url", "").strip()
    if not raw_url:
        return "URL mancante"

    target_price = float(data.get("targetPrice", 0))
    language_code = str(data.get("language", ""))
    is_sealed = bool(data.get("isSealed", False)) or ("/Products/Booster" in raw_url or "/Products/Boxes" in raw_url or "/Products/Tins" in raw_url or "/Products/Trainer-Boxes" in raw_url)

    target_url = build_filtered_url(raw_url, language_code, is_sealed)

    try:
        resp = cf_requests.get(
            CF_WORKER_URL,
            params={"url": target_url},
            impersonate="chrome",
            timeout=40
        )
    except Exception as e:
        return f"Errore connessione Worker su {target_url}: {e}"

    if resp.status_code != 200:
        return f"HTTP {resp.status_code} dal Worker su {target_url}: {resp.text[:150]}"

    soup = BeautifulSoup(resp.text, "html.parser")

    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if "Just a moment" in page_title or "Attention Required" in page_title:
        return f"Challenge Cloudflare non superato su {target_url}"

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else data.get("title") or "Prodotto Cardmarket"

    image_url = data.get("imageUrl", "")
    img_tag = soup.select_one("img.is-front, .product-image img, meta[property='og:image']")
    if img_tag:
        if img_tag.name == "meta":
            image_url = img_tag.get("content", image_url)
        else:
            src = img_tag.get("src") or img_tag.get("data-src")
            if src:
                image_url = src if src.startswith("http") else f"https:{src}"

    offers = []
    rows = soup.select("div.table-body div.article-row")
    for row in rows:
        price_el = row.select_one(".price-container .color-primary, .col-price .color-primary, .mobile-offer-container .color-primary")
        if not price_el:
            continue
        price = parse_price(price_el.get_text(strip=True))
        if price is None:
            continue

        seller_el = row.select_one(".seller-name a, .col-seller a")
        seller = seller_el.get_text(strip=True) if seller_el else "Venditore"

        cond_el = row.select_one(".article-condition span, .product-attributes a.article-condition span")
        condition = cond_el.get("title") or cond_el.get_text(strip=True) if cond_el else ("Sealed" if is_sealed else "NM")

        lang_el = row.select_one(".product-attributes span[data-original-title], .product-attributes span[aria-label]")
        lang_name = (lang_el.get("data-original-title") or lang_el.get("aria-label")) if lang_el else LANG_MAP.get(language_code, "")

        if language_code and lang_name:
            expected_lang = LANG_MAP.get(language_code, "").lower()
            if expected_lang and expected_lang not in lang_name.lower():
                continue

        if not is_sealed and condition:
            if condition.upper() not in ["MT", "NM", "MINT", "NEAR MINT"]:
                continue

        offers.append({
            "price": price,
            "seller": seller,
            "condition": condition,
            "language": lang_name or LANG_MAP.get(language_code, "")
        })

    offers.sort(key=lambda x: x["price"])
    top_10 = offers[:10]

    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    update_payload = {
        "title": title,
        "imageUrl": image_url,
        "topOffers": top_10,
        "lastChecked": now_iso
    }

    if top_10:
        best = top_10[0]
        min_price = best["price"]
        update_payload["currentLowest"] = min_price

        history = data.get("dailyHistory", {}) or {}
        if today_str not in history or min_price < float(history[today_str]):
            history[today_str] = min_price
            update_payload["dailyHistory"] = history

        notified_offers = data.get("notifiedOffers", {}) or {}
        offer_key = f"{best['seller']}_{best.get('condition', '')}_{best.get('language', '')}"
        last_notified_price = notified_offers.get(offer_key)

        if target_price > 0 and min_price <= target_price:
            if last_notified_price is None or min_price < float(last_notified_price):
                send_discord_alert(user_webhook, title, target_url, image_url, best, target_price)
                notified_offers[offer_key] = min_price
                update_payload["notifiedOffers"] = notified_offers

    doc_ref.update(update_payload)
    return f"OK {title}: {len(top_10)} offerte trovate (min: {top_10[0]['price']} €)" if top_10 else f"OK {title}: 0 offerte compatibili trovate"

def run_all_checks():
    results = []
    users = db.collection("users").stream()
    for user_doc in users:
        user_data = user_doc.to_dict() or {}
        webhook = user_data.get("discordWebhook", "")
        items = user_doc.reference.collection("tracked_items").stream()
        for item_doc in items:
            res = check_single_item(item_doc.reference, item_doc.to_dict() or {}, webhook)
            results.append(res)
            time.sleep(2)
    return results

def background_worker():
    time.sleep(15)
    while True:
        try:
            print(f"[{datetime.now()}] Avvio ciclo controllo Cardmarket...")
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
    return jsonify({"status": "running", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/test")
def test_now():
    esito = run_all_checks()
    return jsonify({"trovati_nel_db": len(esito), "esito": esito})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
