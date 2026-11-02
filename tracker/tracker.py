"""Weekly SJU flight price tracker.

Searches next month's Thursday -> Monday nonstop round trips on Google Flights
(via SerpApi), saves the prices to data/history.json, writes the data file the
web page reads, and emails a summary with a link to the page.

Usage:
    python tracker/tracker.py              # real run: search, save, email
    python tracker/tracker.py --no-email   # search and save, skip the email
    python tracker/tracker.py --demo       # fake data for previewing the page (no searches, nothing saved)
    python tracker/tracker.py --month 2027-01   # search a specific month instead of next month

Environment variables:
    SERPAPI_KEY          SerpApi API key (required for real runs)
    GMAIL_ADDRESS        Gmail account that sends the email
    GMAIL_APP_PASSWORD   Gmail app password for that account
    EMAIL_TO             Where to send it (defaults to GMAIL_ADDRESS; comma-separate several)
    PAGE_URL             Link to the web page, included in the email
"""

import argparse
import json
import os
import random
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
HISTORY_FILE = ROOT / "data" / "history.json"
SITE_DATA_FILE = ROOT / "site" / "data.json"

# Puerto Rico is UTC-4 all year (no daylight saving time).
PR_TZ = timezone(timedelta(hours=-4))
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


# ---------------------------------------------------------------- dates

def today_pr() -> date:
    return datetime.now(PR_TZ).date()


def next_month(d: date) -> tuple[int, int]:
    return (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)


def trip_dates(year: int, month: int, weekday: int, nights: int) -> list[tuple[date, date]]:
    """Every <weekday> in the month, paired with its return date."""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    out = []
    while d.month == month:
        out.append((d, d + timedelta(days=nights)))
        d += timedelta(days=7)
    return out


def month_label(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {MONTHS[d.month - 1][:3]} {d.day}"


# ---------------------------------------------------------------- SerpApi

def fallback_flights_url(origin: str, dest: str, out_d: str, ret_d: str) -> str:
    q = f"Nonstop flights from {origin} to {dest.replace(',', ' or ')} on {out_d} returning {ret_d}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def serpapi_search(api_key: str, cfg: dict, dest: str, out_d: date, ret_d: date) -> dict:
    params = {
        "engine": "google_flights",
        "departure_id": cfg["origin"],
        "arrival_id": dest,
        "outbound_date": out_d.isoformat(),
        "return_date": ret_d.isoformat(),
        "type": "1",  # round trip
        "currency": cfg.get("currency", "USD"),
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    if cfg.get("nonstop_only", True):
        params["stops"] = "1"  # 1 = nonstop only
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=90) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"SerpApi HTTP {e.code}: {msg or e.reason}") from None


def parse_options(resp: dict, nonstop_only: bool) -> list[dict]:
    options = []
    for opt in (resp.get("best_flights") or []) + (resp.get("other_flights") or []):
        legs = opt.get("flights") or []
        if not legs or opt.get("price") is None:
            continue
        if nonstop_only and len(legs) > 1:
            continue
        first, last = legs[0], legs[-1]
        options.append({
            "price": opt["price"],
            "airline": ", ".join(dict.fromkeys(l.get("airline", "") for l in legs)),
            "airline_logo": opt.get("airline_logo") or first.get("airline_logo"),
            "flight_number": " / ".join(l.get("flight_number", "") for l in legs),
            "depart_airport": first.get("departure_airport", {}).get("id"),
            "depart_time": first.get("departure_airport", {}).get("time"),
            "arrive_airport": last.get("arrival_airport", {}).get("id"),
            "arrive_time": last.get("arrival_airport", {}).get("time"),
            "duration_min": opt.get("total_duration"),
            "airplane": first.get("airplane"),
            "legroom": first.get("legroom"),
            "travel_class": first.get("travel_class"),
        })
    # Remove duplicates (same flight listed twice) and sort by price.
    seen, unique = set(), []
    for o in sorted(options, key=lambda o: o["price"]):
        key = (o["flight_number"], o["depart_time"], o["price"])
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique


def search_trip(api_key: str, cfg: dict, trip: dict, dates: list[tuple[date, date]]) -> list[dict]:
    results = []
    for out_d, ret_d in dates:
        entry = {
            "depart": out_d.isoformat(),
            "return": ret_d.isoformat(),
            "google_flights_url": fallback_flights_url(cfg["origin"], trip["destination"],
                                                       out_d.isoformat(), ret_d.isoformat()),
            "price_insights": None,
            "options": [],
            "error": None,
        }
        print(f"  {trip['name']}: {out_d} -> {ret_d} ... ", end="", flush=True)
        try:
            resp = serpapi_search(api_key, cfg, trip["destination"], out_d, ret_d)
            if resp.get("error") and not (resp.get("best_flights") or resp.get("other_flights")):
                # SerpApi returns an "error" field when there are simply no results.
                entry["error"] = resp["error"]
            entry["options"] = parse_options(resp, cfg.get("nonstop_only", True))[:10]
            url = (resp.get("search_metadata") or {}).get("google_flights_url")
            if url:
                entry["google_flights_url"] = url
            pi = resp.get("price_insights") or {}
            if pi:
                entry["price_insights"] = {
                    "lowest_price": pi.get("lowest_price"),
                    "price_level": pi.get("price_level"),
                    "typical_price_range": pi.get("typical_price_range"),
                }
            print(f"{len(entry['options'])} flights" + (f" ({entry['error']})" if entry["error"] else ""))
        except Exception as e:  # keep going if one date fails
            entry["error"] = str(e)
            print(f"FAILED: {e}")
        results.append(entry)
    return results


# ---------------------------------------------------------------- demo data

def demo_runs(cfg: dict, target: tuple[int, int], weekday: int) -> list[dict]:
    rng = random.Random(42)
    airlines = {
        "washington": [("JetBlue", "B6"), ("United", "UA"), ("Southwest", "WN"), ("Spirit", "NK")],
        "new-york": [("JetBlue", "B6"), ("Delta", "DL"), ("American", "AA"), ("Frontier", "F9")],
    }
    dests = {"washington": ["IAD", "BWI", "DCA"], "new-york": ["JFK"]}
    dates = trip_dates(*target, weekday, cfg["nights"])
    first_day = date(*target, 1)
    mondays = [first_day - timedelta(days=first_day.weekday()) - timedelta(weeks=w) for w in (4, 3, 2, 1)]
    runs = []
    for w, monday in enumerate(mondays):
        trips = {}
        for trip in cfg["trips"]:
            entries = []
            for i, (out_d, ret_d) in enumerate(dates):
                holiday = 90 if out_d.month == 12 and out_d.day >= 17 else 0
                base = 240 + i * 15 + holiday + w * rng.randint(-10, 15)
                opts = []
                for name, code in rng.sample(airlines.get(trip["id"], airlines["new-york"]), 3):
                    hour = rng.choice([6, 9, 13, 16, 20])
                    dur = rng.randint(215, 245)
                    dep = datetime(out_d.year, out_d.month, out_d.day, hour, rng.choice([0, 15, 30, 45]))
                    arr = dep + timedelta(minutes=dur)
                    opts.append({
                        "price": base + rng.randint(0, 90),
                        "airline": name, "airline_logo": None,
                        "flight_number": f"{code} {rng.randint(100, 2999)}",
                        "depart_airport": cfg["origin"],
                        "depart_time": dep.strftime("%Y-%m-%d %H:%M"),
                        "arrive_airport": rng.choice(dests.get(trip["id"], ["JFK"])),
                        "arrive_time": arr.strftime("%Y-%m-%d %H:%M"),
                        "duration_min": dur,
                        "airplane": rng.choice(["Airbus A320", "Airbus A321", "Boeing 737-800"]),
                        "legroom": rng.choice(["30 in", "31 in", "32 in"]),
                        "travel_class": "Economy",
                    })
                opts.sort(key=lambda o: o["price"])
                low = opts[0]["price"]
                entries.append({
                    "depart": out_d.isoformat(), "return": ret_d.isoformat(),
                    "google_flights_url": fallback_flights_url(cfg["origin"], trip["destination"],
                                                               out_d.isoformat(), ret_d.isoformat()),
                    "price_insights": {"lowest_price": low,
                                       "price_level": "low" if low < 280 else "high" if low > 340 else "typical",
                                       "typical_price_range": [270, 340]},
                    "options": opts, "error": None,
                })
            trips[trip["id"]] = {"name": trip["name"], "destination": trip["destination"], "dates": entries}
        runs.append({"run_date": monday.isoformat(), "target_month": f"{target[0]}-{target[1]:02d}",
                     "trips": trips})
    return runs


# ---------------------------------------------------------------- email

def top_flights(run_trip: dict, n: int) -> list[dict]:
    all_opts = []
    for entry in run_trip["dates"]:
        for o in entry["options"]:
            all_opts.append({**o, "depart": entry["depart"], "return": entry["return"],
                             "url": entry["google_flights_url"]})
    return sorted(all_opts, key=lambda o: o["price"])[:n]


def fmt_time(t: str | None) -> str:
    if not t:
        return "?"
    try:
        return datetime.strptime(t, "%Y-%m-%d %H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return t


def build_email(cfg: dict, run: dict, page_url: str) -> tuple[str, str, str]:
    month = month_label(run["target_month"])
    names = " & ".join(t["name"] for t in cfg["trips"])
    subject = f"✈ Cheapest nonstop flights for {month}: {cfg['origin']} → {names}"
    cur = "$" if cfg.get("currency", "USD") == "USD" else cfg["currency"] + " "
    out_day = cfg["departure_weekday"].capitalize()
    ret_day = WEEKDAYS[(WEEKDAYS.index(out_day.lower()) + cfg["nights"]) % 7].capitalize()

    text = [f"Cheapest nonstop round trips for {month} "
            f"({out_day} → {ret_day}, {cfg['nights']} nights). Prices are round-trip per person.\n"]
    html = [f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:620px;color:#1a1a1a">
<h2 style="margin:0 0 4px">✈ Cheapest flights for {escape(month)}</h2>
<p style="margin:0 0 20px;color:#555">Nonstop, {escape(out_day)} → {escape(ret_day)} ({cfg['nights']} nights).
Prices are round-trip per person.</p>"""]

    for trip in cfg["trips"]:
        rt = run["trips"].get(trip["id"])
        if not rt:
            continue
        flights = top_flights(rt, cfg.get("top_n", 3))
        text.append(f"{cfg['origin']} → {trip['name'].upper()}")
        html.append(f'<h3 style="margin:24px 0 8px">{escape(cfg["origin"])} → {escape(trip["name"])}</h3>')
        if not flights:
            text.append("  No nonstop flights found this week.\n")
            html.append('<p style="color:#555">No nonstop flights found this week.</p>')
            continue
        html.append('<table style="border-collapse:collapse;width:100%;font-size:14px">')
        for f in flights:
            dates = f"{short_date(f['depart'])} → {short_date(f['return'])}"
            times = f"{fmt_time(f['depart_time'])} → {fmt_time(f['arrive_time'])} {f['arrive_airport'] or ''}"
            text.append(f"  {dates}  {f['airline']} {f['flight_number']}  {times}  "
                        f"{cur}{f['price']}\n    {f['url']}")
            html.append(f"""<tr style="border-bottom:1px solid #e5e5e5">
<td style="padding:10px 8px 10px 0"><b>{escape(dates)}</b><br>
<span style="color:#555">{escape(f['airline'])} {escape(f['flight_number'])} · {escape(times)}</span></td>
<td style="padding:10px 8px;text-align:right;white-space:nowrap"><b style="font-size:17px">{cur}{f['price']}</b><br>
<a href="{escape(f['url'])}" style="color:#1a66d9">Google Flights</a></td></tr>""")
        html.append("</table>")
        text.append("")

    errors = [f"{t['name']} {short_date(e['depart'])}: {e['error']}"
              for t in cfg["trips"] if t["id"] in run["trips"]
              for e in run["trips"][t["id"]]["dates"] if e["error"] and not e["options"]]
    if errors:
        text.append("Notes: " + "; ".join(errors))
        html.append('<p style="color:#888;font-size:12px;margin-top:16px">Notes: '
                    + escape("; ".join(errors)) + "</p>")
    if page_url:
        text.append(f"\nFull details and price trends: {page_url}")
        html.append(f"""<p style="margin-top:24px"><a href="{escape(page_url)}"
style="background:#1a66d9;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;display:inline-block">
See full details &amp; price trends →</a></p>""")
    html.append("</div>")
    return subject, "\n".join(text), "\n".join(html)


def send_email(subject: str, text: str, html: str) -> None:
    sender = "".join(os.environ.get("GMAIL_ADDRESS", "").split())
    # App passwords are shown as "abcd efgh ijkl mnop"; drop all spaces/newlines from pasting.
    password = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    to = os.environ.get("EMAIL_TO", "").strip() or sender
    if not sender or not password:
        print("Email skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    print(f"Logging in to Gmail as {sender[:2]}***@{sender.partition('@')[2]} "
          f"(app password length: {len(password)}, should be 16)")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(sender, password)
            s.send_message(msg)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
        raise SystemExit(f"Gmail login failed ({e}). Check that GMAIL_ADDRESS is the full Gmail "
                         "address and GMAIL_APP_PASSWORD is a current 16-character app password.")
    print(f"Email sent to {to}.")


# ---------------------------------------------------------------- main

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def site_payload(cfg: dict, runs: list[dict], demo: bool) -> dict:
    return {
        "generated_at": datetime.now(PR_TZ).isoformat(timespec="minutes"),
        "demo": demo,
        "config": {k: cfg[k] for k in ("origin", "departure_weekday", "nights", "nonstop_only", "currency")}
                  | {"trips": [{"id": t["id"], "name": t["name"], "destination": t["destination"]}
                               for t in cfg["trips"]]},
        "runs": runs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-email", action="store_true", help="don't send the email")
    ap.add_argument("--demo", action="store_true", help="write fake data for previewing the page")
    ap.add_argument("--month", help="search this month (YYYY-MM) instead of next month")
    ap.add_argument("--email-only", action="store_true",
                    help="don't search; email the most recent saved results")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)

    if args.email_only:
        runs = load_json(HISTORY_FILE, {"runs": []})["runs"]
        if not runs:
            print("No saved results to email.", file=sys.stderr)
            return 1
        latest = max(runs, key=lambda r: (r["run_date"], r["target_month"]))
        send_email(*build_email(cfg, latest, os.environ.get("PAGE_URL", "")))
        return 0

    weekday = WEEKDAYS.index(cfg["departure_weekday"].lower())
    today = today_pr()
    target = tuple(int(x) for x in args.month.split("-")) if args.month else next_month(today)
    dates = trip_dates(*target, weekday, cfg["nights"])
    target_str = f"{target[0]}-{target[1]:02d}"

    if args.demo:
        runs = demo_runs(cfg, target, weekday)
        write_json(SITE_DATA_FILE, site_payload(cfg, runs, demo=True))
        print(f"Demo data written to {SITE_DATA_FILE} (nothing searched, history untouched).")
        if not args.no_email:
            subject, text, _ = build_email(cfg, runs[-1], os.environ.get("PAGE_URL", ""))
            print("\n--- email preview ---\n" + subject + "\n\n" + text)
        return 0

    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key:
        print("SERPAPI_KEY is not set.", file=sys.stderr)
        return 1

    print(f"Searching {month_label(target_str)}: {len(dates)} dates × {len(cfg['trips'])} trips "
          f"= {len(dates) * len(cfg['trips'])} searches")
    run = {"run_date": today.isoformat(), "target_month": target_str, "trips": {}}
    for trip in cfg["trips"]:
        run["trips"][trip["id"]] = {"name": trip["name"], "destination": trip["destination"],
                                    "dates": search_trip(api_key, cfg, trip, dates)}

    history = load_json(HISTORY_FILE, {"runs": []})
    # A re-run on the same day for the same month replaces the earlier one.
    history["runs"] = [r for r in history["runs"]
                       if not (r["run_date"] == run["run_date"] and r["target_month"] == target_str)]
    history["runs"].append(run)
    history["runs"].sort(key=lambda r: (r["target_month"], r["run_date"]))
    write_json(HISTORY_FILE, history)
    write_json(SITE_DATA_FILE, site_payload(cfg, history["runs"], demo=False))
    print(f"Saved to {HISTORY_FILE}")

    all_failed = all(e["error"] and not e["options"]
                     for t in run["trips"].values() for e in t["dates"])
    if not args.no_email:
        send_email(*build_email(cfg, run, os.environ.get("PAGE_URL", "")))
    if all_failed:
        print("Every search failed - check the SerpApi key and quota.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
