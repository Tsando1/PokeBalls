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
        "footer": {"text": "Cardmarket Cloud Tracker • Ogni 5 Minuti"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        print(f"  [DISCORD] Notifica inviata per {item_title} a {offer['price']:.2f} € (status: {resp.status_code})")
    except Exception as e:
        print(f"  [DISCORD ERROR] Errore invio Discord: {e}")

def fetch_html_stealth(target_url, sb=None):
    """
    Motori di scaricamento in cascata:
    1. Scrape.do (se configurato)
    2. curl_cffi impersonate chrome120
    3. SeleniumBase CDP Mode (Chrome DevTools Protocol con bypass Turnstile)
    """
    # 0. Scrape.do se disponibile
    scraped_token = os.environ.get("SCRAPEDO_TOKEN", "").strip()
    if scraped_token:
        try:
            api_url = f"https://api.scrape.do/?token={scraped_token}&url={target_url}"
            resp = requests.get(api_url, timeout=25)
            if resp.status_code == 200 and ("article-row" in resp.text or "h1" in resp.text):
                return resp.text, "Scrape.do"
        except Exception:
            pass

    # 1. curl_cffi impersonate chrome120
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
            timeout=15
        )
        if resp.status_code == 200 and "article-row" in resp.text and "Please login to continue" not in resp.text and "Just a moment" not in resp.text:
            return resp.text, "curl_cffi"
    except Exception:
        pass

    # 2. SeleniumBase UC Mode (Bypass Cloudflare Turnstile)
    if sb:
        try:
            print("  [UC Mode] Apertura stealth con reconnect...")
            sb.uc_open_with_reconnect(target_url, reconnect_time=5)
            sb.sleep(3)

            html = sb.get_page_source()
            if "article-row" in html or ("h1" in html and "table" in html):
                return html, "UC Reconnect"

            # Se Cloudflare mostra la schermata Turnstile / Just a moment
            if "Just a moment" in html or "Attention Required" in html or "Turnstile" in html:
                print("  [UC Mode] Rilevata schermata Turnstile, esecuzione solve_captcha()...")
                try:
                    sb.solve_captcha()
                    sb.sleep(3)
                except Exception as ce:
                    print(f"    solve_captcha fallback: {ce}")

                html = sb.get_page_source()
                if "article-row" in html or "h1" in html:
                    return html, "UC solve_captcha"

                print("  [UC Mode] Captcha ancora presente, tentativo con uc_gui_click_captcha()...")
                try:
                    sb.uc_gui_click_captcha()
                    sb.sleep(4)
                except Exception as ge:
                    print(f"    uc_gui_click_captcha fallback: {ge}")

                html = sb.get_page_source()
                if "article-row" in html or "h1" in html:
                    return html, "UC gui_click"

            # Fallback CDP mode se ancora bloccato
            try:
                print("  [UC Mode] Tentativo fallback con activate_cdp_mode...")
                sb.activate_cdp_mode(target_url)
                sb.sleep(3)
                sb.solve_captcha()
                sb.sleep(3)
                html = sb.get_page_source()
                if "article-row" in html or "h1" in html:
                    return html, "CDP Mode"
            except Exception:
                pass

            print(f"  [UC Mode Fine] Titolo pagina: '{sb.get_title()}' - Lunghezza HTML: {len(html)}")
            if "article-row" in html:
                return html, "UC Page"
        except Exception as e:
            print(f"  [UC Mode Errore]: {e}")

    return None, "Blocco Cloudflare"

def check_item(doc_ref, data, sb=None):
    raw_url = (data.get("baseUrl") or data.get("url") or "").strip()
    title_db = data.get("name") or data.get("title") or "Prodotto Cardmarket"
    
    if not raw_url:
        return {"title": title_db, "min": "-", "avg": "-", "status": "URL mancante"}

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

    html, engine_used = fetch_html_stealth(target_url, sb)
    now_utc = datetime.now(timezone.utc)
    formatted_now = now_utc.strftime("%d/%m %H:%M UTC")

    if not html:
        doc_ref.update({"lastUpdated": f"Errore anti-bot ({formatted_now})"})
        err_msg = f"Blocco Cloudflare"
        print(f"    [ERR] {err_msg}")
        return {"title": title_db, "min": "-", "avg": "-", "status": err_msg}

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

    now_iso = now_utc.isoformat()
    today_str = now_utc.strftime("%Y-%m-%d")

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
        update_payload["hourlyHistory"] = raw_hourly[-96:]

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
        summary = f"OK ({len(top_10)} offerte)"
        print(f"    OK {title}: Minimo {min_price:.2f} €, Media Top 10: {current_avg:.2f} €")
        return {"title": title, "min": f"{min_price:.2f} €", "avg": f"{current_avg:.2f} €", "status": summary}
    else:
        doc_ref.update(update_payload)
        print(f"    OK {title}: 0 offerte compatibili trovate.")
        return {"title": title, "min": "N/D", "avg": "N/D", "status": f"0 offerte trovate ({engine_used})"}

def main():
    print("=" * 60)
    print(f"AVVIO CARDMARKET PRICE TRACKER • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    items_to_check = []
    processed_paths = set()

    # Scansione collezione 'items'
    try:
        for item_doc in db.collection("items").stream():
            path = item_doc.reference.path
            if path not in processed_paths:
                processed_paths.add(path)
                items_to_check.append((item_doc.reference, item_doc.to_dict() or {}))
    except Exception as e:
        print(f"Avviso scansione db.collection('items'): {e}")

    try:
        for item_doc in db.collection_group("items").stream():
            path = item_doc.reference.path
            if path not in processed_paths:
                processed_paths.add(path)
                items_to_check.append((item_doc.reference, item_doc.to_dict() or {}))
    except Exception as e:
        print(f"Avviso scansione collection_group: {e}")

    print(f"Elementi totali trovati nel database: {len(items_to_check)}")
    
    summary_rows = []

    if not items_to_check:
        print("Nessun prodotto da monitorare nel database.")
    else:
        # Esecuzione con SeleniumBase UC Mode (Bypass Turnstile)
        try:
            from seleniumbase import SB
            print("Avvio sessione SeleniumBase UC Mode (Bypass Turnstile)...")
            with SB(uc=True, test=True) as sb:
                for idx, (ref, data) in enumerate(items_to_check, 1):
                    print(f"\n[{idx}/{len(items_to_check)}]")
                    res = check_item(ref, data, sb)
                    summary_rows.append(res)
                    time.sleep(2)
        except Exception as e:
            print(f"Errore sessione SeleniumBase: {e}")
            # Fallback senza browser
            for idx, (ref, data) in enumerate(items_to_check, 1):
                res = check_item(ref, data, None)
                summary_rows.append(res)

    # Scrittura file diagnostico report_last_run.json
    try:
        report_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_items": len(items_to_check),
            "results": summary_rows
        }
        with open("report_last_run.json", "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)
        print("File report_last_run.json salvato.")
    except Exception as e:
        print(f"Errore salvataggio report_last_run.json: {e}")

    # Scrittura report riassuntivo in GITHUB_STEP_SUMMARY
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            lines = [
                "## ⚡ Report Esecuzione Cardmarket Tracker (Ogni 5 Min)",
                f"**Data e Ora:** `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`",
                f"**Prodotti Monitorati:** `{len(items_to_check)}`",
                "",
                "| Prodotto | Minimo Attuale | Media Top 10 | Esito |",
                "|---|---|---|---|"
            ]
            for r in summary_rows:
                lines.append(f"| **{r['title']}** | {r['min']} | {r['avg']} | {r['status']} |")
            if not summary_rows:
                lines.append("| *Nessun elemento* | - | - | Database vuoto |")

            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print("Report scritto con successo in GITHUB_STEP_SUMMARY.")
        except Exception as e:
            print(f"Errore scrittura GITHUB_STEP_SUMMARY: {e}")

    print("\n" + "=" * 60)
    print("CICLO DI CONTROLLO COMPLETATO CON SUCCESSO!")
    print("=" * 60)

if __name__ == "__main__":
    main()
