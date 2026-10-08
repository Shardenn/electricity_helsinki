"""Send a chart of tomorrow's Finnish spot electricity prices (with min/max/average) to Telegram or ntfy.

Prices are the Nord Pool day-ahead prices for Finland (what Helen Exchange Electricity
follows), VAT included, in c/kWh. Needs matplotlib (see requirements.txt) for the chart.

Configuration via environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  -> send via Telegram bot
  NTFY_TOPIC                            -> send via ntfy.sh push notification
  HELEN_MARGIN                          -> optional margin in c/kWh added to every price (e.g. 0.49)
  HOURS_COUNT                           -> how many cheapest / most expensive hours to highlight (default 3)

Designed to be run frequently (e.g. every 10 minutes). Each run:
  1. reads new Telegram messages to the bot (only from TELEGRAM_CHAT_ID):
       "HH:MM" (e.g. 07:30) -> change the daily notification time
       /time                -> show the current notification time
       /now                 -> send tomorrow's chart right away
  2. if the notification time has passed today and nothing was sent yet today, fetches
     tomorrow's prices and sends the chart (if they aren't published yet, retries next run).
State (notification time, last sent date, Telegram offset) lives in state.json.

Run with --dry-run to print the caption and save the chart to chart.png instead of sending.
"""

import base64
import json
import os
import re
import sys
import uuid
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


def hourly_averages(prices, day, margin):
    """Return [(hour start, average price of its 15-min slots)] for the given day, in time order."""
    hours = {}
    for t, p in prices:
        if t.date() == day:
            hours.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(p + margin)
    return sorted((h, sum(ps) / len(ps)) for h, ps in hours.items())


def fmt_price(p):
    return f"{round(p, 2) + 0.0:.2f}"  # + 0.0 avoids "-0.00"


def render_chart(day, slots, hours, count, avg):
    """Bar chart of the day's 15-min prices as PNG bytes; cheapest/priciest hours highlighted."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    by_price = sorted(hours, key=lambda h: h[1])
    cheap = {h for h, _ in by_price[:count]}
    pricey = {h for h, _ in by_price[-count:]}
    green, red, grey = "#2e9d5b", "#d64545", "#8a9bb0"

    def color(t):
        hour = t.replace(minute=0, second=0, microsecond=0)
        return green if hour in cheap else red if hour in pricey else grey

    x = [t.hour + t.minute / 60 for t, _ in slots]
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=120)
    ax.bar(x, [p for _, p in slots], width=0.25, align="edge", color=[color(t) for t, _ in slots])
    ax.axhline(avg, color="#444", linestyle="--", linewidth=1)
    ax.axhline(0, color="#444", linewidth=0.6)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2), [f"{h:02d}" for h in range(0, 25, 2)])
    ax.set_ylabel("c/kWh")
    ax.set_title(f"Electricity price {day:%a %d.%m.%Y}", loc="left", fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(
        handles=[
            Patch(color=green, label=f"Cheapest {count} h"),
            Patch(color=red, label=f"Most expensive {count} h"),
            plt.Line2D([], [], color="#444", linestyle="--", label=f"Average {fmt_price(avg)}"),
        ],
        loc="upper left",
        bbox_to_anchor=(0, -0.1),
        ncols=3,
        frameon=False,
    )
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def build_report(margin, count):
    """Return (caption, PNG bytes) for tomorrow, or None if tomorrow's prices aren't published yet."""
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    prices = fetch_prices()
    slots = sorted((t, p + margin) for t, p in prices if t.date() == tomorrow)
    if not slots:
        return None
    hours = hourly_averages(prices, tomorrow, margin)
    count = min(count, len(hours) // 2)
    low = min(slots, key=lambda s: s[1])
    high = max(slots, key=lambda s: s[1])
    avg = sum(p for _, p in slots) / len(slots)
    caption = (
        f"⚡ <b>Tomorrow {tomorrow:%a %d.%m.}</b>\n"
        f"🟢 Lowest:  {fmt_price(low[1])} c/kWh at {low[0]:%H:%M}\n"
        f"🔴 Highest: {fmt_price(high[1])} c/kWh at {high[0]:%H:%M}\n"
        f"⚪ Average: {fmt_price(avg)} c/kWh\n"
        f"<i>incl. VAT" + (f" + {margin:g} c margin" if margin else "") + "</i>"
    )
    return caption, render_chart(tomorrow, slots, hours, count, avg)


NOT_PUBLISHED = "Tomorrow's prices aren't published yet (usually around 14:00)."


def telegram_api(token, method, params, files=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in params.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, (filename, content) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: image/png\r\n\r\n".encode() + content + b"\r\n"
            )
        req = urllib.request.Request(
            url,
            data=b"".join(parts) + f"--{boundary}--\r\n".encode(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["result"]


def send_telegram(token, chat_id, text):
    telegram_api(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def send_telegram_photo(token, chat_id, caption, png):
    telegram_api(
        token,
        "sendPhoto",
        {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"photo": ("prices.png", png)},
    )


def send_ntfy(topic, caption, png):
    plain = re.sub(r"</?[bi]>", "", caption)
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=png,
        method="PUT",
        headers={
            "Title": "Electricity prices",
            "Tags": "zap",
            "Filename": "prices.png",
            # RFC 2047 so the emoji/umlauts survive in an HTTP header
            "Message": "=?UTF-8?B?" + base64.b64encode(plain.encode()).decode() + "?=",
        },
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


def deliver(caption, png):
    sent = False
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        send_telegram_photo(token, chat_id, caption, png)
        sent = True
    if os.environ.get("NTFY_TOPIC"):
        send_ntfy(os.environ["NTFY_TOPIC"], caption, png)
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
            reply = f"✅ Tomorrow's prices will now be sent daily at {state['notify_time']} (Helsinki time)."
        elif text.startswith("/time"):
            reply = f"⏰ Tomorrow's prices are sent daily at {state['notify_time']} (Helsinki time)."
        elif text.startswith("/now"):
            send_now = True
            continue
        else:
            reply = (
                "Send a time like <b>15:00</b> to change when tomorrow's prices arrive.\n"
                "/time - show the current time\n/now - send tomorrow's chart right away"
            )
        send_telegram(token, chat_id, reply)
    return send_now


def main():
    margin = float(os.environ.get("HELEN_MARGIN") or 0)
    count = int(os.environ.get("HOURS_COUNT") or 3)
    if "--dry-run" in sys.argv:
        report = build_report(margin, count)
        if report is None:
            print(NOT_PUBLISHED)
            return
        Path("chart.png").write_bytes(report[1])
        print(report[0] + "\n\n(chart saved to chart.png)")
        return

    state = load_state()
    send_now = process_commands(state)

    now = datetime.now(TZ)
    today = now.date().isoformat()
    due = now.strftime("%H:%M") >= state["notify_time"] and state["last_sent_date"] != today
    if due or send_now:
        report = build_report(margin, count)
        if report is None:
            # Not out yet: keep it due so a later run sends it once published.
            print(NOT_PUBLISHED)
            if send_now:
                send_telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], NOT_PUBLISHED)
        else:
            deliver(*report)
            if due:
                state["last_sent_date"] = today
            print("Sent:\n" + report[0])
    else:
        print(f"Nothing to send (time {state['notify_time']}, last sent {state['last_sent_date']}).")
    save_state(state)


if __name__ == "__main__":
    main()
