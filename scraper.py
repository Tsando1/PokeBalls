import os
import re
import json
import time
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

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

def send_discord(webhook_url: str, name: str, min_price: float, target: float, url: str, is_sealed: bool, image_url: str = ""):
    if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
        return
    spec = "Sealed" if is_sealed else "Near Mint"
    embed = {
        "title": f"🚨 Offerta sotto soglia: {name}",
        "url": url,
        "color": 3447003,
        "fields": [
            {"name": "1ª Offerta Trovata", "value": f"**{min_price:.2f} €**", "inline": True},
            {"name": "Tua Soglia", "value": f"{target:.2f} €", "inline": True},
            {"name": "Specifica", "value": spec, "inline": True}
        ],
        "footer": {"text": "Cardmarket Tracker"}
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"Errore invio Discord: {e}")

def scrape_item(doc_snap):
    data = doc_snap.to_dict()
    base_url = data["baseUrl"]
    is_sealed = "/Singles/" not in base_url
    url = build_filtered_url(base_url, data["langId"], is_sealed)

    resp = cf_requests.get(
        url,
        impersonate="chrome",
        headers={"Accept-Language": "en-US,en;q=0.9,it;q=0.8"},
        timeout=25
    )
    if resp.status_code != 200:
        print(f"Errore HTTP {resp.status_code} su {url}")
        return

    soup = BeautifulSoup(resp.text, "html.parser")
    updates = {"isSealed": is_sealed}

    card_name = data.get("name", "Caricamento...")
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
                top10.append(val)
        if len(top10) == 10:
            break

    if not top10:
        doc_snap.reference.update(updates)
        return

    now_it = datetime.now(TZ_ITALY)
    today_str = now_it.strftime("%Y-%m-%d")
    now_label = now_it.strftime("%d/%m %H:%M")

    min_price = top10[0]
    avg_top10 = round(sum(top10) / len(top10), 2)

    updates["top10Offers"] = top10
    updates["currentMinPrice"] = min_price
    updates["currentAvgTop10"] = avg_top10
    updates["lastUpdated"] = now_label
    updates["history"] = firestore.ArrayUnion([{"t": now_label, "min": min_price, "avg": avg_top10}])

    saved_day = data.get("dailyMinDate")
    saved_daily_min = data.get("dailyMinPrice")

    if saved_day != today_str or saved_daily_min is None:
        updates["dailyMinDate"] = today_str
        updates["dailyMinPrice"] = min_price
    elif min_price < float(saved_daily_min):
        updates["dailyMinPrice"] = min_price

    target = float(data["targetPrice"])
    if min_price <= target:
        last_alert = data.get("lastAlertPrice")
        if last_alert is None or min_price < float(last_alert):
            send_discord(data.get("discordWebhook", ""), card_name, min_price, target, url, is_sealed, img_url)
            updates["lastAlertPrice"] = min_price
    else:
        updates["lastAlertPrice"] = None

    doc_snap.reference.update(updates)
    print(f"OK {card_name} | Ora: {min_price}€ | Min Oggi: {updates.get('dailyMinPrice', saved_daily_min)}€")

def main():
    docs = db.collection("items").stream()
    for doc in docs:
        try:
            scrape_item(doc)
        except Exception as e:
            print(f"Errore su {doc.id}: {e}")
        time.sleep(random.uniform(4.0, 8.0))

if __name__ == "__main__":
    main()