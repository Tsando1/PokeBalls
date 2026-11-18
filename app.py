import os
import re
import json
import time
import random
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from flask import Flask
import requests
from curl_cffi import requests as cf_requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

cred_dict = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
cred = credentials.Certificate(cred_dict)
firebase_admin.initialize_app(cred)
db = firestore.client()

TZ_ITALY = ZoneInfo("Europe/Rome")
lock = threading.Lock()
web_app = Flask(__name__)

def log(msg: str):
    print(msg, flush=True)

@web_app.route("/")
def health():
    return "Cardmarket Tracker Active", 200

@web_app.route("/test")
def force_run():
    docs = list(db.collection("items").stream())
    results = []
    for doc in docs:
        res = check_single_item(doc.id, doc.to_dict(), force_refresh_top10=True)
        results.append(res)
    return {"trovati_nel_db": len(docs), "esito": results}, 200

def build_filtered_url(base_url: str, lang_id: str, is_sealed: bool) -> str:
    parsed = urlparse(base_url.strip())
    query = parse_qs(parsed.query)
    query["language"] = [lang_id]
    if not is_sealed:
        query["minCondition"] = ["2"]
    else:
        query.pop("minCondition", None)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment))

def parse_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d,\.]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def is_row_sealed(row) -> bool:
    info_col = row.select_one(".product-attributes, .col-product") or row
    html_snippet = str(info_col).lower()
    return any(k in html_snippet for k in ["sealed", "sigillato", "scellé", "versiegelt", "precintado"])

def send_discord(webhook_url: str, name: str, min_price: float, seller: str, target: float, url: str, is_sealed: bool, image_url: str = ""):
    if not webhook_url or ("discord.com/api/webhooks/" not in webhook_url and "discordapp.com/api/webhooks/" not in webhook_url):
        return
    spec = "Sealed" if is_sealed else "Near Mint"
    embed = {
        "title": f"🚨 Offerta sotto soglia: {name}",
        "url": url,
        "color": 3447003,
        "fields": [
            {"name": "1ª Offerta Trovata", "value": f"**{min_price:.2f} €**", "inline": True},
            {"name": "Venditore", "value": seller or "N/D", "inline": True},
            {"name": "Tua Soglia", "value": f"{target:.2f} €", "inline": True},
            {"name": "Specifica", "value": spec, "inline": True}
        ],
        "footer": {"text": "Cardmarket Cloud Tracker"}
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log(f"Errore invio Discord: {e}")

def check_single_item(doc_id: str, data: dict, force_refresh_top10: bool = False) -> str:
    with lock:
        base_url = data["baseUrl"]
        is_sealed = "/Singles/" not in base_url
        url = build_filtered_url(base_url, data["langId"], is_sealed)

        try:
            proxy_url = "https://pokeballs.alexandrodellis1102.workers.dev"
resp = cf_requests.get(
    proxy_url,
    params={"url": url},
    impersonate="chrome",
    timeout=25
)
            if resp.status_code != 200:
                msg = f"HTTP {resp.status_code} su {url}"
                log(msg)
                return msg
        except Exception as e:
            msg = f"Errore connessione: {e}"
            log(msg)
            return msg

        soup = BeautifulSoup(resp.text, "html.parser")
        updates = {"isSealed": is_sealed}

        card_name = data.get("name", "")
        if not card_name or card_name.startswith("Caricamento"):
            h1 = soup.select_one("h1")
            if h1:
                card_name = h1.get_text(" ", strip=True)
                updates["name"] = card_name

        img_url = data.get("imageUrl", "")
        if not img_url:
            img = soup.select_one(".image-container img, .info-list-container img, meta[property='og:image']")
            if img:
                src = img.get("src") or img.get("content") or ""
                img_url = ("https:" + src) if src.startswith("//") else src
                updates["imageUrl"] = img_url

        rows = soup.select("#table .article-row")
        top10 = []
        best_seller = ""
        best_offer_id = ""

        for row in rows:
            if is_sealed and not is_row_sealed(row):
                continue
            if not is_sealed:
                comm = row.select_one(".product-comments")
                txt = comm.get_text(" ", strip=True).lower() if comm else ""
                if any(k in txt for k in ["psa", "bgs", "cgc", "graad", "proxy"]):
                    continue

            price_el = row.select_one(".price-container .font-weight-bold, .price-container span")
            if price_el:
                val = parse_price(price_el.get_text(strip=True))
                if val and val > 0:
                    if len(top10) == 0:
                        seller_el = row.select_one(".seller-name a, .seller-info a")
                        best_seller = seller_el.get_text(strip=True) if seller_el else "unknown"
                        best_offer_id = row.get("id", f"{best_seller}_{val}")
                    top10.append(val)
            if len(top10) == 10:
                break

        if not top10:
            db.collection("items").document(doc_id).update(updates)
            msg = f"Nessuna offerta valida trovata per {card_name} (righe HTML trovate: {len(rows)})"
            log(msg)
            return msg

        now_it = datetime.now(TZ_ITALY)
        today_str = now_it.strftime("%Y-%m-%d")
        today_label = now_it.strftime("%d/%m")
        current_hour_str = now_it.strftime("%Y-%m-%d %H")
        now_time_str = now_it.strftime("%H:%M:%S")

        min_price = top10[0]
        avg_top10 = round(sum(top10) / len(top10), 2)

        updates["currentMinPrice"] = min_price
        updates["lastUpdated"] = f"{today_label} {now_time_str}"

        saved_day = data.get("dailyMinDate")
        saved_hour = data.get("lastTop10Hour")

        if force_refresh_top10 or saved_day != today_str or saved_hour != current_hour_str or not data.get("top10Offers"):
            updates["top10Offers"] = top10
            updates["currentAvgTop10"] = avg_top10
            updates["lastTop10Hour"] = current_hour_str

        saved_daily_min = data.get("dailyMinPrice")
        daily_history = list(data.get("dailyHistory", []))

        if saved_day != today_str or saved_daily_min is None:
            new_daily_min = min_price
            updates["dailyMinDate"] = today_str
            updates["dailyMinPrice"] = new_daily_min
            daily_history.append({"d": today_label, "min": new_daily_min})
            updates["dailyHistory"] = daily_history
        elif min_price < float(saved_daily_min):
            new_daily_min = min_price
            updates["dailyMinPrice"] = new_daily_min
            if daily_history and daily_history[-1]["d"] == today_label:
                daily_history[-1]["min"] = new_daily_min
            else:
                daily_history.append({"d": today_label, "min": new_daily_min})
            updates["dailyHistory"] = daily_history

        target = float(data["targetPrice"])
        if min_price <= target:
            last_sig = data.get("lastAlertSignature")
            last_alert_price = data.get("lastAlertPrice")
            current_sig = f"{best_offer_id}_{min_price}"

            if current_sig != last_sig and (last_alert_price is None or min_price <= float(last_alert_price)):
                send_discord(data.get("discordWebhook", ""), card_name, min_price, best_seller, target, url, is_sealed, img_url)
                updates["lastAlertSignature"] = current_sig
                updates["lastAlertPrice"] = min_price
        else:
            updates["lastAlertSignature"] = None
            updates["lastAlertPrice"] = None

        db.collection("items").document(doc_id).update(updates)
        msg = f"OK {card_name} -> 1ª Offerta: {min_price:.2f}€ | Min Oggi: {updates.get('dailyMinPrice', saved_daily_min):.2f}€"
        log(msg)
        return msg

def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == "ADDED":
            data = change.document.to_dict()
            if not data.get("top10Offers"):
                log("Nuova carta rilevata dal sito! Avvio controllo immediato...")
                threading.Thread(target=check_single_item, args=(change.document.id, data, True)).start()

def background_worker():
    log("Worker avviato, connessione a Firebase...")
    db.collection("items").on_snapshot(on_snapshot)
    while True:
        try:
            docs = list(db.collection("items").stream())
            log(f"Controllo periodico su {len(docs)} prodotti...")
            for doc in docs:
                check_single_item(doc.id, doc.to_dict(), force_refresh_top10=False)
                time.sleep(random.uniform(3.0, 6.0))
        except Exception as e:
            log(f"Errore ciclo: {e}")
        time.sleep(60)

threading.Thread(target=background_worker, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)
