from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from mmt_client import MMTClient


DEFAULT_TZ = "Europe/Moscow"
DEFAULT_SPOT_CVD_EXCHANGE = "binance:bitfinex:bybit:coinbase:kraken:okx"
DEFAULT_PERP_CVD_EXCHANGE = "binancef:bitfinexf:bitmexf:bybitf:deribitf:extendedf:hyperliquid:lighterf:okxf"
DEFAULT_PRICE_EXCHANGE = "binancef"
DEFAULT_DERIVATIVE_EXCHANGE = "binancef"


def rows(payload: dict) -> list[dict]:
    return list(payload.get("data") or [])


def parse_time(value: str, tz_name: str) -> int:
    if value.isdigit():
        return int(value)
    value = value.replace("T", " ")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return int(dt.timestamp())


def rows_by_t(payload: dict) -> dict[int, dict]:
    return {int(item["t"]): item for item in rows(payload)}


def exchange_ids(exchange_spec: str) -> list[str]:
    return [item.strip() for item in exchange_spec.split(":") if item.strip()]


def aggregate_rows_by_t(payloads: list[dict]) -> dict[int, dict]:
    grouped: dict[int, dict] = {}
    fields = ("o", "h", "l", "c", "n")
    for payload in payloads:
        for item in rows(payload):
            ts = int(item["t"])
            target = grouped.setdefault(ts, {"t": ts, "o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0, "n": 0.0})
            for field in fields:
                target[field] += float(item.get(field) or 0.0)
    return grouped


def fetch_vd_rows_by_t(
    client: MMTClient,
    exchange_spec: str,
    symbol: str,
    tf: str,
    start: int,
    end: int,
    bucket: int,
    *,
    use_cache: bool,
) -> dict[int, dict]:
    exchanges = exchange_ids(exchange_spec)
    if len(exchanges) <= 1:
        return rows_by_t(client.vd(exchange_spec, symbol, tf, start, end, bucket=bucket, use_cache=use_cache))
    payloads = [client.vd(exchange, symbol, tf, start, end, bucket=bucket, use_cache=use_cache) for exchange in exchanges]
    return aggregate_rows_by_t(payloads)


def fmt_time(ts: int, tz_name: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).strftime("%d.%m %H:%M")


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


def delta_for(series: dict[int, dict], timestamps: list[int], field: str = "c") -> float | None:
    values = [float(series[t][field]) for t in timestamps if t in series and series[t].get(field) is not None]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def sum_for(series: dict[int, dict], timestamps: list[int], field: str) -> float:
    return sum(float(series[t].get(field) or 0.0) for t in timestamps if t in series)


def avg_for(series: dict[int, dict], timestamps: list[int], field: str) -> float | None:
    values = [float(series[t].get(field) or 0.0) for t in timestamps if t in series and series[t].get(field) is not None]
    if not values:
        return None
    return mean(values)


def arrow(delta: float | None, threshold: float) -> str:
    if delta is None:
        return "n/a"
    if delta > threshold:
        return "up"
    if delta < -threshold:
        return "down"
    return "flat"


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


def parse_windows(raw: str, total_count: int, tf_minutes: int) -> list[tuple[str, int]]:
    windows: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.lower() in {"all", "full", "day", "session"}:
            windows.append((item, total_count))
            continue
        if item.endswith("m"):
            minutes = float(item[:-1])
            label = item
        elif item.endswith("h"):
            minutes = float(item[:-1]) * 60
            label = item
        else:
            minutes = float(item)
            label = f"{item}m"
        count = max(2, int(round(minutes / tf_minutes)) + 1)
        windows.append((label, min(count, total_count)))
    return windows


def timeframe_minutes(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    raise SystemExit(f"Unsupported timeframe for active OF: {tf}")


def build_report(args: argparse.Namespace) -> str:
    tz = ZoneInfo(args.timezone)
    end_dt = datetime.fromtimestamp(parse_time(args.end, args.timezone), tz) if args.end else datetime.now(tz)
    start_dt = datetime.fromtimestamp(parse_time(args.start, args.timezone), tz) if args.start else end_dt - timedelta(hours=args.hours)
    if end_dt <= start_dt:
        raise SystemExit("--end must be after --start")
    start = int(start_dt.timestamp())
    end = int(end_dt.timestamp())
    tf_min = timeframe_minutes(args.tf)

    client = MMTClient(min_interval_s=args.min_interval, reserve_weight=args.reserve_weight)
    candles = rows(client.candles(args.price_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))
    perp_cvd_exchange = args.perp_cvd_exchange or args.perp_exchange
    spot_vd = fetch_vd_rows_by_t(client, args.spot_exchange, args.symbol, args.tf, start, end, args.bucket, use_cache=not args.no_cache)
    perp_vd = fetch_vd_rows_by_t(client, perp_cvd_exchange, args.symbol, args.tf, start, end, args.bucket, use_cache=not args.no_cache)
    oi = rows_by_t(client.oi(args.perp_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))
    stats = rows_by_t(client.stats(args.perp_exchange, args.symbol, args.tf, start, end, use_cache=not args.no_cache))

    if len(candles) < 2:
        raise SystemExit("No candle data returned for the requested window.")

    all_ts = [int(candle["t"]) for candle in candles]
    last = candles[-1]
    lines = [
        "### Active order-flow snapshot",
        f"- Now: `{end_dt.strftime('%d.%m %H:%M')}`; TF `{args.tf}`; window `{args.hours:g}h`.",
        f"- Price: `{args.price_exchange}`; Spot CVD: `{args.spot_exchange}`; Perp CVD: `{perp_cvd_exchange}`; OI/Stats: `{args.perp_exchange}`.",
        f"- Last candle `{fmt_time(int(last['t']), args.timezone)}` O `{float(last['o']):g}` H `{float(last['h']):g}` L `{float(last['l']):g}` C `{float(last['c']):g}`.",
        "",
    ]

    for label, count in parse_windows(args.windows, len(all_ts), tf_min):
        timestamps = all_ts[-count:]
        subset = [candle for candle in candles if int(candle["t"]) in timestamps]
        if len(subset) < 2:
            continue
        price_delta = float(subset[-1]["c"]) - float(subset[0]["o"])
        low = min(float(candle["l"]) for candle in subset)
        high = max(float(candle["h"]) for candle in subset)
        close = float(subset[-1]["c"])

        buy_volume = sum_for(stats, timestamps, "vb")
        sell_volume = sum_for(stats, timestamps, "vs")
        net_taker = buy_volume - sell_volume
        total_volume = buy_volume + sell_volume
        cvd_threshold = max(total_volume * args.cvd_threshold, 1.0)

        spot_delta = delta_for(spot_vd, timestamps)
        perp_delta = delta_for(perp_vd, timestamps)
        oi_delta = delta_for(oi, timestamps)
        oi_mean = avg_for(oi, timestamps, "c") or 0.0
        oi_threshold = max(abs(oi_mean) * args.oi_threshold, 1.0)
        funding = avg_for(stats, timestamps, "fr")
        liq_buy = sum_for(stats, timestamps, "lb")
        liq_sell = sum_for(stats, timestamps, "ls")
        liq_buy_count = sum_for(stats, timestamps, "tlb")
        liq_sell_count = sum_for(stats, timestamps, "tls")

        lines.extend(
            [
                f"#### {label}: `{fmt_time(timestamps[0], args.timezone)} -> {fmt_time(timestamps[-1], args.timezone)}`",
                f"- Price `{money(price_delta)}`; range `{low:g}-{high:g}`; close `{close:g}`.",
                f"- Spot CVD `{arrow(spot_delta, cvd_threshold)}` `{money(spot_delta)}`; Perp CVD `{arrow(perp_delta, cvd_threshold)}` `{money(perp_delta)}`; OI `{arrow(oi_delta, oi_threshold)}` `{money(oi_delta)}`.",
                f"- Net taker flow `{flow_label(buy_volume, sell_volume)}` `{money(net_taker)}`; buy/sell vol `{money(buy_volume)}` / `{money(sell_volume)}`.",
                f"- Positioning `{positioning_read(price_delta, net_taker, oi_delta, buy_volume, sell_volume)}`.",
                f"- Funding avg `{funding:.6f}`; liquidations buy/sell `{money(liq_buy)}` / `{money(liq_sell)}`; liq counts `{liq_buy_count:.0f}` / `{liq_sell_count:.0f}`.",
                "",
            ]
        )

    rate = client.rate
    if rate.remaining is not None:
        lines.append(f"_Rate limit after run: remaining `{rate.remaining:g}` / limit `{rate.limit:g}`._")
    return "\n".join(lines)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Current MMT order-flow snapshot with net taker flow and liquidations.")
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--symbol", default="btc/usd")
    parser.add_argument("--tf", default="15m")
    parser.add_argument("--start", help="Override start time, e.g. '2026-06-25 10:00'.")
    parser.add_argument("--end", help="Override end time. Defaults to now.")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--windows", default="24h,8h,3h,90m", help="Comma-separated windows, e.g. 24h,8h,3h,90m. Use day/all for the full requested window.")
    parser.add_argument("--price-exchange", default=DEFAULT_PRICE_EXCHANGE)
    parser.add_argument("--spot-exchange", default=DEFAULT_SPOT_CVD_EXCHANGE)
    parser.add_argument("--perp-cvd-exchange", default=DEFAULT_PERP_CVD_EXCHANGE, help="Exchange aggregate for Perp CVD. OI/Stats still use --perp-exchange.")
    parser.add_argument("--perp-exchange", default=DEFAULT_DERIVATIVE_EXCHANGE)
    parser.add_argument("--bucket", type=int, default=1)
    parser.add_argument("--cvd-threshold", type=float, default=0.005)
    parser.add_argument("--oi-threshold", type=float, default=0.002)
    parser.add_argument("--min-interval", type=float, default=0.7)
    parser.add_argument("--reserve-weight", type=float, default=15.0)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    print(build_report(args))


if __name__ == "__main__":
    main()
