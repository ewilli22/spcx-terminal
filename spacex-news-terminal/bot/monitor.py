#!/usr/bin/env python3
"""
SPCX Terminal: free news + price monitor for SpaceX (SPCX) and the broad market.

Runs on a schedule (GitHub Actions), pulls free RSS / SEC / Fed feeds and Yahoo
quotes, scores each headline for likely market impact, emails you the important
ones, and writes docs/data.json for the dashboard.

Standard library only. No API keys required.

Usage:
  python bot/monitor.py               normal run
  python bot/monitor.py --dry-run     print emails instead of sending
  python bot/monitor.py --fixtures DIR  read feeds/quotes from local files (testing)
"""

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"
DATA_PATH = ROOT / "docs" / "data.json"

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


# --------------------------------------------------------------------------- utils

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def http_get(url, fixtures=None, fixture_name=None, sec=False, timeout=20):
    """GET a URL (or read a fixture file when testing). Returns text or None."""
    if fixtures:
        p = Path(fixtures) / fixture_name
        return p.read_text(encoding="utf-8") if p.exists() else None
    headers = {"User-Agent": UA_BROWSER, "Accept": "*/*"}
    if sec:
        # SEC requires a descriptive User-Agent with contact info.
        contact = os.environ.get("SEC_CONTACT") or os.environ.get("GMAIL_ADDRESS") or "spcx-terminal@example.com"
        headers["User-Agent"] = f"SPCX-Terminal personal-research {contact}"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            log(f"  fetch failed ({attempt + 1}/2): {url[:80]}... {e}")
            time.sleep(1.5)
    return None


def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t)          # strip " - Source" (Google News)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def item_id(title):
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)          # RFC 822 (RSS)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# --------------------------------------------------------------------------- feeds

def parse_feed(text, feed):
    """Parse RSS 2.0 or Atom into a list of dicts."""
    items = []
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as e:
        log(f"  parse error in {feed['id']}: {e}")
        return items

    def tag(el):
        return el.tag.split("}", 1)[-1]

    entries = [el for el in root.iter() if tag(el) in ("item", "entry")]
    for el in entries:
        rec = {"title": "", "link": "", "summary": "", "published": None, "source": feed["name"]}
        for c in el:
            t = tag(c)
            if t == "title":
                rec["title"] = strip_html(c.text or "")
            elif t == "link":
                rec["link"] = (c.get("href") or c.text or "").strip() or rec["link"]
            elif t in ("description", "summary", "content"):
                rec["summary"] = strip_html(c.text or "")[:400]
            elif t in ("pubDate", "published", "updated", "date"):
                rec["published"] = rec["published"] or parse_date(c.text)
            elif t == "source" and c.text:
                rec["source"] = c.text.strip()
        if not rec["title"]:
            continue
        if "sec.gov" in feed["url"]:
            rec["title"] = f"SEC filing: {rec['title']}"
        # Google News puts the outlet after " - "
        if "news.google.com" in feed["url"]:
            m = re.search(r"\s+-\s+([^-]{2,60})$", rec["title"])
            if m:
                rec["source"] = m.group(1).strip()
                rec["title"] = rec["title"][: m.start()].strip()
        items.append(rec)
    return items


def build_matchers(words):
    """Short keywords match whole words; longer ones match as prefixes (explod -> exploded)."""
    out = []
    for w, weight in words.items():
        w = w.strip().lower()
        if not w:
            continue
        esc = re.escape(w)
        pat = rf"(?<![a-z0-9]){esc}(?![a-z0-9])" if len(w) <= 4 else rf"(?<![a-z0-9]){esc}"
        out.append((w, weight, re.compile(pat)))
    return out


def score_item(rec, feed, cfg, matchers):
    text = f"{rec['title']} {rec['summary']}".lower()
    spacex_hit = any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text)
                     for t in cfg["spacex_terms"])
    category = "spacex" if (feed["category"] == "spacex" or spacex_hit) else "market"

    hits, score = [], 0
    for cat in ("spacex", "market"):
        for w, weight, rx in matchers[cat]:
            if rx.search(text) and w not in hits:
                hits.append(w)
                score += weight
    # SpaceX-specific words only count fully when the story is about SpaceX
    if category == "market":
        score = sum(wt for w, wt, _ in matchers["market"] if w in hits) + \
                0.5 * sum(wt for w, wt, _ in matchers["spacex"] if w in hits)
    if category == "spacex" and spacex_hit:
        score += 1
    score += feed.get("source_bonus", 0)
    # Headlines carry more signal than summaries
    title_l = rec["title"].lower()
    score += sum(1 for w in hits if w in title_l) * 0.5
    return category, round(min(score, 25), 1), hits


# --------------------------------------------------------------------------- quotes

def fetch_quote(symbol, fixtures=None):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=1d&interval=5m&includePrePost=false")
    txt = http_get(url, fixtures, f"quote_{symbol.replace('^', '_')}.json")
    if not txt:
        return None
    try:
        res = json.loads(txt)["chart"]["result"][0]
        meta = res["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c]
        if price is None or not prev:
            return None
        return {
            "price": round(price, 4),
            "prev_close": round(prev, 4),
            "change": round(price - prev, 4),
            "pct": round((price - prev) / prev * 100, 2),
            "time": meta.get("regularMarketTime"),
            "spark": [round(c, 3) for c in closes[::2]][-80:],
        }
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log(f"  quote parse failed for {symbol}: {e}")
        return None


def market_open(now_local):
    if now_local.weekday() >= 5:
        return False
    hm = now_local.hour * 60 + now_local.minute
    return 9 * 60 + 30 <= hm <= 16 * 60


def check_price_alerts(quotes, cfg, state, now_local):
    alerts = []
    if not market_open(now_local):
        return alerts
    day = now_local.strftime("%Y-%m-%d")
    fired = state.setdefault("price_alerts", {})
    for sym, rule in cfg.get("price_alerts", {}).items():
        q = quotes.get(sym)
        if not q:
            continue
        label = next((x["label"] for x in cfg["quotes"] if x["symbol"] == sym), sym)
        for band in sorted(rule.get("pct_bands", []), reverse=True):
            if abs(q["pct"]) >= band:
                direction = "up" if q["pct"] > 0 else "down"
                key = f"{day}:{sym}:pct:{band}:{direction}"
                if key not in fired:
                    fired[key] = True
                    alerts.append({
                        "title": f"{label} ({sym}) {direction} {abs(q['pct']):.2f}% today, now {q['price']:,.2f}",
                        "symbol": sym, "kind": "price", "score": 10 + min(band, 8),
                    })
                break  # only the biggest band crossed
        for lvl in sorted(rule.get("level_above", []), reverse=True):
            if q["price"] >= lvl:
                key = f"{day}:{sym}:level:{lvl}"
                if key not in fired:
                    fired[key] = True
                    alerts.append({
                        "title": f"{label} above {lvl} (now {q['price']:.2f}), risk-off signal",
                        "symbol": sym, "kind": "price", "score": 12,
                    })
                break
    # prune old keys
    cutoff = (now_local - timedelta(days=5)).strftime("%Y-%m-%d")
    state["price_alerts"] = {k: v for k, v in fired.items() if k[:10] >= cutoff}
    return alerts


# --------------------------------------------------------------------------- email

def send_email(subject, html_body, text_body, dry_run=False):
    user = os.environ.get("GMAIL_ADDRESS")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("ALERT_TO") or user
    if dry_run or not (user and pwd):
        if not dry_run:
            log("  (email skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)")
        print("\n" + "=" * 70 + f"\nSUBJECT: {subject}\n" + "-" * 70 + "\n" + text_body + "\n" + "=" * 70)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"SPCX Terminal <{user}>"
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"  email sent: {subject}")
    return True


def fmt_quote_line(sym, label, q):
    if not q:
        return f"{label:<16} n/a"
    sign = "+" if q["pct"] >= 0 else ""
    return f"{label:<16} {q['price']:>12,.2f}  {sign}{q['pct']:.2f}%"


def render_email(title, items, quotes, cfg, dashboard_url, intro=""):
    """Return (html, text) for a list of alert items."""
    q_order = [(x["symbol"], x["label"]) for x in cfg["quotes"] if x["group"] in ("focus", "index", "risk")][:7]
    text_lines = [title, ""]
    if intro:
        text_lines += [intro, ""]
    for it in items:
        tag = "PRICE" if it.get("kind") == "price" else it.get("category", "").upper()
        text_lines.append(f"[{tag} {it.get('score', '')}] {it['title']}")
        if it.get("source"):
            text_lines.append(f"    {it['source']}  {it.get('link', '')}")
        if it.get("hits"):
            text_lines.append(f"    why: {', '.join(it['hits'][:6])}")
        text_lines.append("")
    text_lines.append("Market snapshot:")
    text_lines += ["  " + fmt_quote_line(s, l, quotes.get(s)) for s, l in q_order]
    if dashboard_url:
        text_lines += ["", f"Dashboard: {dashboard_url}"]
    text_lines += ["", "Automated headline alert, not investment advice."]

    rows = []
    for it in items:
        is_price = it.get("kind") == "price"
        color = "#f5a623" if is_price else ("#5ad1ff" if it.get("category") == "spacex" else "#9be38a")
        tag = "PRICE" if is_price else it.get("category", "").upper()
        link = html.escape(it.get("link", "") or dashboard_url or "#")
        why = f"<div style='color:#888;font-size:12px'>why: {html.escape(', '.join(it.get('hits', [])[:6]))}</div>" if it.get("hits") else ""
        src = f"<div style='color:#888;font-size:12px'>{html.escape(it.get('source', ''))}</div>" if it.get("source") else ""
        rows.append(
            f"<tr><td style='padding:10px 12px;border-bottom:1px solid #222;vertical-align:top'>"
            f"<span style='display:inline-block;font:600 11px monospace;color:#000;background:{color};"
            f"padding:2px 6px;border-radius:3px'>{tag} {it.get('score', '')}</span></td>"
            f"<td style='padding:10px 12px;border-bottom:1px solid #222'>"
            f"<a href='{link}' style='color:#eee;text-decoration:none;font-weight:600'>{html.escape(it['title'])}</a>"
            f"{src}{why}</td></tr>")
    qrows = []
    for s, l in q_order:
        q = quotes.get(s)
        if not q:
            continue
        c = "#4cd97b" if q["pct"] >= 0 else "#ff5c5c"
        sign = "+" if q["pct"] >= 0 else ""
        qrows.append(f"<td style='padding:6px 10px;font:12px monospace;color:#aaa'>{html.escape(l)}<br>"
                     f"<b style='color:#eee'>{q['price']:,.2f}</b> <span style='color:{c}'>{sign}{q['pct']:.2f}%</span></td>")
    dash = (f"<p><a href='{html.escape(dashboard_url)}' style='color:#f5a623'>Open dashboard</a></p>"
            if dashboard_url else "")
    html_body = (
        "<div style='background:#0b0b0c;color:#ddd;font-family:-apple-system,Segoe UI,Arial,sans-serif;padding:20px'>"
        f"<div style='font:700 13px monospace;color:#f5a623;letter-spacing:1px'>SPCX TERMINAL</div>"
        f"<h2 style='margin:6px 0 4px;color:#fff;font-size:18px'>{html.escape(title)}</h2>"
        + (f"<p style='color:#aaa'>{html.escape(intro)}</p>" if intro else "")
        + f"<table style='width:100%;border-collapse:collapse;background:#121214'>{''.join(rows)}</table>"
        f"<table style='margin-top:14px;background:#121214'><tr>{''.join(qrows)}</tr></table>"
        f"{dash}<p style='color:#555;font-size:11px'>Automated headline alert, not investment advice.</p></div>")
    return html_body, "\n".join(text_lines)


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print emails instead of sending")
    ap.add_argument("--fixtures", help="read feeds/quotes from this folder (testing)")
    ap.add_argument("--now", help="override current time, ISO format (testing)")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json missing or invalid")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    now_utc = datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    dashboard_url = os.environ.get("DASHBOARD_URL", "")

    state = load_json(STATE_PATH, {})
    first_run = not state.get("seen")
    seen = state.setdefault("seen", {})            # id -> unix ts first seen
    data = load_json(DATA_PATH, {})
    history = {it["id"]: it for it in data.get("items", [])}

    matchers = {cat: build_matchers(words) for cat, words in cfg["keywords"].items()}
    max_age = timedelta(hours=cfg.get("max_item_age_hours", 24))

    # ---- news
    new_items, feed_status = [], []
    for feed in cfg["feeds"]:
        log(f"feed {feed['id']}")
        txt = http_get(feed["url"], args.fixtures, f"{feed['id']}.xml", sec="sec.gov" in feed["url"])
        if txt is None:
            feed_status.append({"id": feed["id"], "name": feed["name"], "ok": False, "count": 0})
            continue
        recs = parse_feed(txt, feed)
        feed_status.append({"id": feed["id"], "name": feed["name"], "ok": True, "count": len(recs)})
        for rec in recs:
            if rec["published"] and now_utc - rec["published"] > max_age:
                continue
            iid = item_id(rec["title"])
            if iid in seen:
                continue
            seen[iid] = int(now_utc.timestamp())
            category, score, hits = score_item(rec, feed, cfg, matchers)
            item = {
                "id": iid, "title": rec["title"], "link": rec["link"], "source": rec["source"],
                "feed": feed["id"], "category": category, "score": score, "hits": hits,
                "summary": rec["summary"][:240],
                "published": (rec["published"] or now_utc).isoformat(),
                "seen_at": now_utc.isoformat(),
                "alert": score >= cfg["thresholds"].get(category, 99),
            }
            new_items.append(item)
            history[iid] = item
    log(f"{len(new_items)} new items, {sum(i['alert'] for i in new_items)} above threshold")

    # ---- quotes
    quotes = {}
    for q in cfg["quotes"]:
        res = fetch_quote(q["symbol"], args.fixtures)
        if res:
            quotes[q["symbol"]] = res
        elif q["symbol"] in data.get("quotes", {}):          # keep last known value, mark stale
            quotes[q["symbol"]] = {**data["quotes"][q["symbol"]], "stale": True}

    price_alerts = [] if first_run else check_price_alerts(quotes, cfg, state, now_local)
    for pa in price_alerts:
        pid = "px" + hashlib.sha1(pa["title"].encode()).hexdigest()[:14]
        history[pid] = {"id": pid, "title": pa["title"], "link": "", "source": "Price alert",
                        "feed": "price", "category": "spacex" if pa["symbol"] == "SPCX" else "market",
                        "score": round(pa["score"], 1), "hits": [], "summary": "", "kind": "price",
                        "published": now_utc.isoformat(), "seen_at": now_utc.isoformat(), "alert": True}

    # ---- email
    email_cfg = cfg.get("email", {})
    prefix = email_cfg.get("subject_prefix", "[SPCX Terminal]")
    if email_cfg.get("enabled", True):
        if first_run:
            n = len(new_items)
            send_email(f"{prefix} Monitor is live",
                       *render_email("Your SPCX news monitor is live",
                                     sorted(new_items, key=lambda x: -x["score"])[:5], quotes, cfg, dashboard_url,
                                     intro=f"First run indexed {n} recent headlines without alerting. "
                                           f"From now on you'll get an email when something scores above your thresholds. "
                                           f"Here are today's top 5 so far."),
                       dry_run=args.dry_run)
        else:
            alerts = [{**p, "score": round(p["score"], 1),
                       "category": "spacex" if p["symbol"] == "SPCX" else "market"} for p in price_alerts]
            alerts += [i for i in new_items if i["alert"]]
            # SpaceX first, then by severity
            alerts.sort(key=lambda x: (x.get("category") != "spacex", -x["score"]))
            alerts = alerts[: email_cfg.get("max_items_per_email", 15)]
            if alerts:
                top = alerts[0]["title"]
                more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
                has_sx = any(a.get("category") == "spacex" for a in alerts)
                tag = "SPCX" if has_sx else "MARKET"
                subj = f"{prefix} {tag}: {top[:90]}{more}"
                send_email(subj, *render_email(f"{len(alerts)} new alert{'s' if len(alerts) > 1 else ''}",
                                               alerts, quotes, cfg, dashboard_url), dry_run=args.dry_run)

        # daily digest after the close
        if email_cfg.get("daily_digest") and now_local.weekday() < 5:
            hh, mm = map(int, email_cfg.get("digest_after_local_time", "16:15").split(":"))
            today = now_local.strftime("%Y-%m-%d")
            if (now_local.hour, now_local.minute) >= (hh, mm) and state.get("last_digest") != today:
                state["last_digest"] = today
                start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                todays = [i for i in history.values()
                          if datetime.fromisoformat(i["seen_at"]) >= start]
                top = sorted(todays, key=lambda x: -x["score"])[:12]
                if top or quotes:
                    spcx = quotes.get("SPCX")
                    intro = (f"SPCX closed {spcx['price']:,.2f} ({spcx['pct']:+.2f}%). " if spcx else "") + \
                            f"{len(todays)} headlines tracked today; top {len(top)} by impact score below."
                    send_email(f"{prefix} Daily close digest {now_local:%a %b %d}",
                               *render_email("Daily close digest", top, quotes, cfg, dashboard_url, intro=intro),
                               dry_run=args.dry_run)

    # ---- persist
    cutoff = now_utc.timestamp() - 7 * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_run"] = now_utc.isoformat()
    save_json(STATE_PATH, state)

    items = sorted(history.values(), key=lambda x: x["published"], reverse=True)[: cfg.get("dashboard_history", 400)]
    save_json(DATA_PATH, {
        "generated_at": now_utc.isoformat(),
        "market_open": market_open(now_local),
        "thresholds": cfg["thresholds"],
        "quotes": quotes,
        "quote_meta": cfg["quotes"],
        "items": items,
        "catalysts": cfg.get("catalysts", []),
        "feeds": feed_status,
    })
    log(f"wrote {DATA_PATH.relative_to(ROOT)} ({len(items)} items, {len(quotes)} quotes)")


if __name__ == "__main__":
    main()
