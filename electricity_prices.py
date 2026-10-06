"""Send today's and tomorrow's Finnish spot electricity min/max prices to Telegram or ntfy.

Prices are the Nord Pool day-ahead prices for Finland (what Helen Exchange Electricity
follows), VAT included, in c/kWh. Uses only the Python standard library.

Configuration via environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  -> send via Telegram bot
  NTFY_TOPIC                            -> send via ntfy.sh push notification
  HELEN_MARGIN                          -> optional margin in c/kWh added to every price (e.g. 0.49)

Designed to be run frequently (e.g. every 10 minutes). Each run:
  1. reads new Telegram messages to the bot (only from TELEGRAM_CHAT_ID):
       "HH:MM" (e.g. 07:30) -> change the daily notification time
       /time                -> show the current notification time
       /now                 -> send prices right away
  2. if the notification time has passed today and nothing was sent yet today,
     fetches prices and sends them.
State (notification time, last sent date, Telegram offset) lives in state.json.

Run with --dry-run to just print the message.
"""

import json
import os
import re
import sys
from pathlib import Path
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class HelsinkiFallback(tzinfo):
    """EET/EEST per EU rules, for systems without tz data (e.g. Windows without tzdata)."""

    @staticmethod
    def _last_sunday_utc(year, month):
        d = datetime(year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def utcoffset(self, dt):
        utc = dt.replace(tzinfo=None) - timedelta(hours=2)  # approx is fine away from switch
        start, end = self._last_sunday_utc(dt.year, 3), self._last_sunday_utc(dt.year, 10)
        return timedelta(hours=3 if start.replace(tzinfo=None) <= utc < end.replace(tzinfo=None) else 2)

    def fromutc(self, dt):
        start, end = self._last_sunday_utc(dt.year, 3), self._last_sunday_utc(dt.year, 10)
        naive = dt.replace(tzinfo=None)
        dst = start.replace(tzinfo=None) <= naive < end.replace(tzinfo=None)
        return (naive + timedelta(hours=3 if dst else 2)).replace(tzinfo=self)

    def dst(self, dt):
        return self.utcoffset(dt) - timedelta(hours=2)


try:
    TZ = ZoneInfo("Europe/Helsinki")
except ZoneInfoNotFoundError:
    TZ = HelsinkiFallback()
PORSSISAHKO_URL = "https://api.porssisahko.net/v2/latest-prices.json"
SPOT_HINTA_URL = "https://api.spot-hinta.fi/TodayAndDayForward"


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "helen-price-notifier/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_prices():
    """Return a list of (start datetime in Helsinki time, price c/kWh incl. VAT)."""
    try:
        data = http_get_json(PORSSISAHKO_URL)
        return [
            (datetime.fromisoformat(p["startDate"].replace("Z", "+00:00")).astimezone(TZ), p["price"])
            for p in data["prices"]
        ]
    except Exception as e:
        print(f"porssisahko.net failed ({e}), trying spot-hinta.fi", file=sys.stderr)
    data = http_get_json(SPOT_HINTA_URL)
    return [
        (datetime.fromisoformat(p["DateTime"]).astimezone(TZ), p["PriceWithTax"] * 100)
        for p in data
    ]


def summarize_day(prices, day, margin):
    slots = sorted((t, p + margin) for t, p in prices if t.date() == day)
    if not slots:
        return None
    low = min(slots, key=lambda s: s[1])
    high = max(slots, key=lambda s: s[1])
    avg = sum(p for _, p in slots) / len(slots)
    return low, high, avg


def format_day(label, day, summary):
    head = f"<b>{label} {day.strftime('%a %d.%m.')}</b>"
    if summary is None:
        return f"{head}\nNot published yet (usually around 14:00)."
    (lt, lp), (ht, hp), avg = summary
    lp, hp, avg = (round(x, 2) + 0.0 for x in (lp, hp, avg))  # avoid "-0.00"
    return (
        f"{head}\n"
        f"🟢 Lowest:  {lp:.2f} c/kWh at {lt:%H:%M}\n"
        f"🔴 Highest: {hp:.2f} c/kWh at {ht:%H:%M}\n"
        f"⚪ Average: {avg:.2f} c/kWh"
    )


def build_message(margin):
    prices = fetch_prices()
    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    parts = [
        "⚡ Electricity prices (incl. VAT" + (f" + {margin:g} c margin" if margin else "") + ")",
        format_day("Today", today, summarize_day(prices, today, margin)),
        format_day("Tomorrow", tomorrow, summarize_day(prices, tomorrow, margin)),
    ]
    return "\n\n".join(parts)


def telegram_api(token, method, params):
    body = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=body, timeout=30) as resp:
        return json.load(resp)["result"]


def send_telegram(token, chat_id, text):
    telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def send_ntfy(topic, text):
    plain = text.replace("<b>", "").replace("</b>", "")
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=plain.encode("utf-8"),
        headers={"Title": "Electricity prices", "Tags": "zap"},
    )
    urllib.request.urlopen(req, timeout=30)


STATE_FILE = Path(__file__).with_name("state.json")
DEFAULT_STATE = {"notify_time": "08:00", "last_sent_date": None, "telegram_offset": 0}
TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


def load_state():
    try:
        return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        return dict(DEFAULT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def deliver(message):
    sent = False
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        send_telegram(token, chat_id, message)
        sent = True
    if os.environ.get("NTFY_TOPIC"):
        send_ntfy(os.environ["NTFY_TOPIC"], message)
        sent = True
    if not sent:
        sys.exit("No delivery configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or NTFY_TOPIC")


def process_commands(state):
    """Handle messages sent to the bot since the last run. Returns True if /now was requested."""
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    updates = telegram_api(token, "getUpdates", {"offset": state["telegram_offset"], "timeout": 0})
    send_now = False
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # ignore strangers who find the bot
        text = (msg.get("text") or "").strip()
        match = TIME_RE.match(text)
        if match:
            state["notify_time"] = f"{int(match[1]):02d}:{match[2]}"
            reply = f"✅ Daily prices will now be sent at {state['notify_time']} (Helsinki time)."
        elif text.startswith("/time"):
            reply = f"⏰ Daily prices are sent at {state['notify_time']} (Helsinki time)."
        elif text.startswith("/now"):
            send_now = True
            continue
        else:
            reply = (
                "Send a time like <b>07:30</b> to change when daily prices arrive.\n"
                "/time - show the current time\n/now - send prices right away"
            )
        send_telegram(token, chat_id, reply)
    return send_now


def main():
    margin = float(os.environ.get("HELEN_MARGIN") or 0)
    if "--dry-run" in sys.argv:
        print(build_message(margin))
        return

    state = load_state()
    send_now = process_commands(state)

    now = datetime.now(TZ)
    today = now.date().isoformat()
    due = now.strftime("%H:%M") >= state["notify_time"] and state["last_sent_date"] != today
    if due or send_now:
        message = build_message(margin)
        deliver(message)
        if due:
            state["last_sent_date"] = today
        print("Sent:\n" + message)
    else:
        print(f"Nothing to send (time {state['notify_time']}, last sent {state['last_sent_date']}).")
    save_state(state)


if __name__ == "__main__":
    main()
