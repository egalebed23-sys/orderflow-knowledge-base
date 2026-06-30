from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Iterable
from zoneinfo import ZoneInfo

import active_of
from mmt_client import MMTClient


DEFAULT_TZ = "Europe/Moscow"
DEFAULT_SPOT_CVD_EXCHANGE = active_of.DEFAULT_SPOT_CVD_EXCHANGE
DEFAULT_PERP_CVD_EXCHANGE = active_of.DEFAULT_PERP_CVD_EXCHANGE
DEFAULT_PRICE_EXCHANGE = active_of.DEFAULT_PRICE_EXCHANGE
DEFAULT_DERIVATIVE_EXCHANGE = active_of.DEFAULT_DERIVATIVE_EXCHANGE


@dataclass
class Zone:
    low: float
    high: float
    name: str
    side: str

    @property
    def label(self) -> str:
        return f"{self.low:g}-{self.high:g}"


@dataclass
class SeriesPoint:
    t: int
    o: float
    h: float
    l: float
    c: float


def parse_time(value: str, tz_name: str) -> int:
    if value.isdigit():
        return int(value)
    value = value.replace("T", " ")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return int(dt.timestamp())


def parse_zone(raw: str) -> Zone:
    parts = [p.strip() for p in raw.split("|")]
    bounds = parts[0].replace("—", "-").replace("–", "-")
    if "-" not in bounds:
        raise ValueError(f"Bad zone bounds: {raw}")
    left, right = bounds.split("-", 1)
    low = float(left)
    high = float(right)
    if high < low:
        low, high = high, low
    name = parts[1] if len(parts) > 1 and parts[1] else f"{low:g}-{high:g}"
    side = parts[2].lower() if len(parts) > 2 and parts[2] else "observe"
    if side not in {"long", "short", "observe"}:
        side = "observe"
    return Zone(low=low, high=high, name=name, side=side)


def ohlc_points(payload: dict) -> list[SeriesPoint]:
    points = []
    for item in payload.get("data", []):
        points.append(SeriesPoint(t=int(item["t"]), o=float(item["o"]), h=float(item["h"]), l=float(item["l"]), c=float(item["c"])))
    return points


def rows_by_t(payload: dict) -> dict[int, dict]:
    return {int(item["t"]): item for item in payload.get("data", [])}


def touched(candle: SeriesPoint, zone: Zone) -> bool:
    return candle.h >= zone.low and candle.l <= zone.high


def group_touch_indices(candles: list[SeriesPoint], zone: Zone) -> list[tuple[int, int]]:
    groups = []
    start = None
    last = None
    for idx, candle in enumerate(candles):
        if touched(candle, zone):
            if start is None:
                start = idx
            last = idx
        elif start is not None:
            groups.append((start, last if last is not None else start))
            start = None
            last = None
    if start is not None:
        groups.append((start, last if last is not None else start))
    return groups


def fmt_time(ts: int, tz_name: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).strftime("%d.%m %H:%M")


def window_indices(start: int, end: int, count: int, before: int = 2, after: int = 8) -> tuple[int, int]:
    return max(0, start - before), min(count - 1, end + after)


def delta_for(rows: dict[int, dict], timestamps: Iterable[int], field: str = "c") -> float | None:
    values = [float(rows[t][field]) for t in timestamps if t in rows and rows[t].get(field) is not None]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def sum_for(rows: dict[int, dict], timestamps: Iterable[int], field: str) -> float:
    return sum(float(rows[t].get(field) or 0.0) for t in timestamps if t in rows)


def avg_for(rows: dict[int, dict], timestamps: Iterable[int], field: str) -> float | None:
    values = [float(rows[t].get(field) or 0.0) for t in timestamps if t in rows and rows[t].get(field) is not None]
    if not values:
        return None
    return mean(values)


def arrow(delta: float | None, threshold: float) -> str:
    if delta is None:
        return "n/a"
    if delta > threshold:
        return "↑"
    if delta < -threshold:
        return "↓"
    return "~"


def money(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}K"
    return f"{sign}{value:.0f}"


def flow_label(buy_volume: float, sell_volume: float) -> str:
    net = buy_volume - sell_volume
    total = buy_volume + sell_volume
    if total <= 0:
        return "flat"
    share = abs(net) / total
    if share < 0.05:
        return "balanced"
    return "net taker longs" if net > 0 else "net taker shorts"


def positioning_read(price_delta: float, net_taker: float, oi_delta: float | None, buy_volume: float, sell_volume: float) -> str:
    if oi_delta is None:
        return "OI unavailable"
    total = buy_volume + sell_volume
    if total > 0 and abs(net_taker) / total < 0.05:
        if oi_delta > 0:
            return "OI builds, but taker flow is balanced"
        if price_delta > 0:
            return "balanced flow / possible short-cover bounce"
        if price_delta < 0:
            return "balanced flow / possible long unwind"
        return "balanced flow"
    if price_delta > 0 and net_taker > 0 and oi_delta > 0:
        return "fresh longs / initiative bid"
    if price_delta < 0 and net_taker < 0 and oi_delta > 0:
        return "fresh shorts / initiative offer"
    if price_delta > 0 and oi_delta <= 0:
        return "short covering / de-risk bounce"
    if price_delta < 0 and oi_delta <= 0:
        return "long unwind / de-risk selloff"
    if price_delta < 0 and net_taker > 0 and oi_delta > 0:
        return "longs absorbed into selloff"
    if price_delta > 0 and net_taker < 0 and oi_delta > 0:
        return "shorts absorbed into bid"
    return "mixed positioning"


def verdict(zone: Zone, price_delta: float, spot_a: str, perp_a: str, oi_a: str, close_after: float) -> str:
    if zone.side == "short":
        if close_after < zone.low and spot_a == "↓" and perp_a in {"↓", "~"}:
            return "short executable: reject/loss with selling CVD"
        if close_after > zone.high:
            return "short invalidated: acceptance above zone"
        return "short needs discretion: reaction without clean OF confirmation"
    if zone.side == "long":
        if close_after > zone.high and spot_a in {"↑", "~"} and oi_a in {"↓", "~"}:
            return "long executable after reclaim/absorption"
        if spot_a == "↓" and perp_a == "↓" and oi_a == "↑":
            return "no long: continuation selling / forced move"
        if close_after < zone.low:
            return "long invalidated: acceptance below zone"
        return "long needs reclaim/absorption confirmation"
    if spot_a == "↓" and perp_a == "↓" and oi_a == "↑":
        return "continuation selling / forced move"
    if price_delta > 0 and oi_a in {"↓", "~"}:
        return "reaction bounce / possible short-covering"
    return "observe"


def build_report(args: argparse.Namespace) -> str:
    start = parse_time(args.start, args.timezone)
    end = parse_time(args.end, args.timezone)
    if end <= start:
        raise SystemExit("--end must be after --start")

    zones = [parse_zone(raw) for raw in args.zone]
    client = MMTClient(min_interval_s=args.min_interval, reserve_weight=args.reserve_weight)

    candles = ohlc_points(client.candles(args.price_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))
    perp_cvd_exchange = args.perp_cvd_exchange or args.perp_exchange
    spot_vd = active_of.fetch_vd_rows_by_t(client, args.spot_exchange, args.symbol, args.tf, start, end, args.bucket, use_cache=not args.no_cache)
    perp_vd = active_of.fetch_vd_rows_by_t(client, perp_cvd_exchange, args.symbol, args.tf, start, end, args.bucket, use_cache=not args.no_cache)
    oi = rows_by_t(client.oi(args.perp_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))
    stats = rows_by_t(client.stats(args.perp_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))

    if not candles:
        raise SystemExit("No candle data returned for the requested window.")

    lines = [
        "### MMT order-flow replay",
        f"- Window: `{fmt_time(start, args.timezone)} → {fmt_time(end, args.timezone)}` `{args.tf}`",
        f"- Price: `{args.price_exchange}`; Spot CVD: `{args.spot_exchange}`; Perp CVD: `{perp_cvd_exchange}`; OI/Stats: `{args.perp_exchange}`",
        "",
    ]

    for zone in zones:
        groups = group_touch_indices(candles, zone)
        lines.append(f"#### {zone.label} — {zone.name} ({zone.side})")
        if not groups:
            lines.append("- Touch: not reached.")
            lines.append("")
            continue

        for n, (touch_start, touch_end) in enumerate(groups, start=1):
            left, right = window_indices(touch_start, touch_end, len(candles), before=args.before, after=args.after)
            window = candles[left : right + 1]
            ts = [c.t for c in window]
            price_delta = window[-1].c - window[0].o
            close_after = window[-1].c
            min_low = min(c.l for c in window)
            max_high = max(c.h for c in window)
            total_volume = sum_for(stats, ts, "vb") + sum_for(stats, ts, "vs")
            buy_volume = sum_for(stats, ts, "vb")
            sell_volume = sum_for(stats, ts, "vs")
            buy_trades = sum_for(stats, ts, "tb")
            sell_trades = sum_for(stats, ts, "ts")
            net_taker = buy_volume - sell_volume
            cvd_threshold = max(total_volume * args.cvd_threshold, 1.0)

            spot_delta = delta_for(spot_vd, ts)
            perp_delta = delta_for(perp_vd, ts)
            oi_delta = delta_for(oi, ts)
            oi_mean = avg_for(oi, ts, "c") or 0.0
            oi_threshold = max(abs(oi_mean) * args.oi_threshold, 1.0)
            fr_avg = avg_for(stats, ts, "fr")
            liq_buy = sum_for(stats, ts, "lb")
            liq_sell = sum_for(stats, ts, "ls")
            liq_buy_count = sum_for(stats, ts, "tlb")
            liq_sell_count = sum_for(stats, ts, "tls")

            spot_a = arrow(spot_delta, cvd_threshold)
            perp_a = arrow(perp_delta, cvd_threshold)
            oi_a = arrow(oi_delta, oi_threshold)
            pos_read = positioning_read(price_delta, net_taker, oi_delta, buy_volume, sell_volume)
            result = verdict(zone, price_delta, spot_a, perp_a, oi_a, close_after)

            lines.extend(
                [
                    f"- Touch {n}: `{fmt_time(candles[touch_start].t, args.timezone)} → {fmt_time(candles[touch_end].t, args.timezone)}`; analysis window `{fmt_time(window[0].t, args.timezone)} → {fmt_time(window[-1].t, args.timezone)}`.",
                    f"- Price: `{money(price_delta)}` close-delta; range `{min_low:g}-{max_high:g}`; close after `{close_after:g}`.",
                    f"- Spot CVD: `{spot_a}` `{money(spot_delta)}`; Perp CVD: `{perp_a}` `{money(perp_delta)}`; OI: `{oi_a}` `{money(oi_delta)}`.",
                    f"- Net taker flow: `{flow_label(buy_volume, sell_volume)}` `{money(net_taker)}`; buy/sell vol `{money(buy_volume)}` / `{money(sell_volume)}`; trades `{buy_trades:.0f}` / `{sell_trades:.0f}`.",
                    f"- Positioning: `{pos_read}`.",
                    f"- Funding avg: `{fr_avg:.6f}`; liquidations buy/sell: `{money(liq_buy)}` / `{money(liq_sell)}`; liq counts `{liq_buy_count:.0f}` / `{liq_sell_count:.0f}`.",
                    f"- Verdict: **{result}**.",
                ]
            )
        lines.append("")

    rate = client.rate
    if rate.remaining is not None:
        lines.append(f"_Rate limit after run: remaining `{rate.remaining:g}` / limit `{rate.limit:g}`._")
    return "\n".join(lines)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Replay MMT order-flow around planned zones.")
    parser.add_argument("--start", required=True, help="Start time, e.g. '2026-06-24 00:00' or Unix seconds.")
    parser.add_argument("--end", required=True, help="End time, e.g. '2026-06-25 00:00' or Unix seconds.")
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--symbol", default="btc/usd")
    parser.add_argument("--tf", default="15m")
    parser.add_argument("--price-exchange", default=DEFAULT_PRICE_EXCHANGE)
    parser.add_argument("--spot-exchange", default=DEFAULT_SPOT_CVD_EXCHANGE)
    parser.add_argument("--perp-cvd-exchange", default=DEFAULT_PERP_CVD_EXCHANGE, help="Exchange aggregate for Perp CVD. OI/Stats still use --perp-exchange.")
    parser.add_argument("--perp-exchange", default=DEFAULT_DERIVATIVE_EXCHANGE)
    parser.add_argument("--bucket", type=int, default=1, help="VD bucket. Basic tier usually supports bucket 1 only.")
    parser.add_argument("--zone", action="append", required=True, help='Repeatable. Format: "low-high|name|short|long|observe".')
    parser.add_argument("--before", type=int, default=2, help="Candles before touch for OF window.")
    parser.add_argument("--after", type=int, default=8, help="Candles after touch for OF window.")
    parser.add_argument("--cvd-threshold", type=float, default=0.005, help="CVD arrow threshold as fraction of window volume.")
    parser.add_argument("--oi-threshold", type=float, default=0.002, help="OI arrow threshold as fraction of mean OI.")
    parser.add_argument("--min-interval", type=float, default=0.35, help="Minimum seconds between uncached API requests.")
    parser.add_argument("--reserve-weight", type=float, default=5.0, help="Sleep near rate limit when remaining weight is <= this value.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass local cache.")
    args = parser.parse_args()
    print(build_report(args))


if __name__ == "__main__":
    main()
