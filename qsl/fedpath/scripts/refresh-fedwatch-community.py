"""Refresh the isolated FedPath community fallback from public sources.

This build-time script is intentionally independent from the formal policy
snapshot and from the official rendered FedWatch capture. It derives only a
small, explicitly exploratory snapshot from public CME ZQ settlements, public
FRED observations, and the checked-in FOMC calendar.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import sys
import zipfile
from pathlib import Path
from typing import Any


CME_SETTLEMENTS_URL = (
    "https://www.cmegroup.com/CmeWS/mvc/Settlements/Futures/Settlements/305/FUT"
)
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MONTH_CODES = {
    "JAN": "F",
    "FEB": "G",
    "MAR": "H",
    "APR": "J",
    "MAY": "K",
    "JUN": "M",
    "JUL": "N",
    "AUG": "Q",
    "SEP": "U",
    "OCT": "V",
    "NOV": "X",
    "DEC": "Z",
}
STEP = 0.25
EPSILON = 1e-9


def requests_client() -> Any:
    try:
        from curl_cffi import requests  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "curl-cffi is required; install scripts/requirements-community.txt"
        ) from exc
    return requests


def fetch_settlements(requested: dt.date, lookback_days: int) -> tuple[dt.date, list[dict[str, Any]]]:
    requests = requests_client()
    last_error = ""
    for offset in range(max(0, lookback_days) + 1):
        trade_date = requested - dt.timedelta(days=offset)
        try:
            response = requests.get(
                CME_SETTLEMENTS_URL,
                params={"tradeDate": trade_date.strftime("%m/%d/%Y")},
                impersonate="chrome",
                timeout=30,
            )
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code} on {trade_date.isoformat()}"
                continue
            payload = response.json()
            rows = payload.get("settlements", []) if isinstance(payload, dict) else []
            if isinstance(rows, list) and rows:
                return trade_date, [row for row in rows if isinstance(row, dict)]
            last_error = f"empty settlement response on {trade_date.isoformat()}"
        except Exception as exc:  # pragma: no cover - provider/network dependent
            last_error = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"CME public settlement source unavailable: {last_error}")


def decode_csv(payload: bytes) -> str:
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                raise RuntimeError("FRED zip response contained no CSV")
            return archive.read(names[0]).decode("utf-8-sig")
    return payload.decode("utf-8-sig")


def fetch_fred(series_id: str, start: dt.date, end: dt.date) -> dict[str, float]:
    requests = requests_client()
    response = requests.get(
        FRED_CSV_URL,
        params={
            "id": series_id,
            "cosd": start.isoformat(),
            "coed": end.isoformat(),
        },
        impersonate="chrome",
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"FRED {series_id} returned HTTP {response.status_code}")
    values: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(decode_csv(response.content))):
        date = (row.get("observation_date") or row.get("DATE") or "").strip()
        raw = (row.get(series_id) or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or raw in {"", ".", "NaN"}:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            values[date] = value
    if not values:
        raise RuntimeError(f"FRED {series_id} returned no numeric observations")
    return values


def load_calendar(path: Path) -> list[dt.date]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise RuntimeError("FOMC calendar must be a JSON array")
    meetings: list[dt.date] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("meetingDate"), str):
            raise RuntimeError("FOMC calendar rows require meetingDate")
        meetings.append(dt.date.fromisoformat(item["meetingDate"]))
    return sorted(set(meetings))


def month_key(value: dt.date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def add_month(value: str) -> str:
    year, month = (int(part) for part in value.split("-"))
    month += 1
    if month == 13:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def month_sequence(start: str, end: str) -> list[str]:
    result: list[str] = []
    current = start
    while current <= end and len(result) < 240:
        result.append(current)
        current = add_month(current)
    return result


def days_in_month(month: str) -> int:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month_number + 1, 1)
    return (next_month - dt.date(year, month_number, 1)).days


def parse_settlements(
    rows: list[dict[str, Any]], trade_date: dt.date
) -> dict[str, dict[str, Any]]:
    futures: dict[str, dict[str, Any]] = {}
    for row in rows:
        month_text = str(row.get("month", "")).strip().upper()
        match = re.fullmatch(r"([A-Z]{3})\s+(\d{2})", month_text)
        if not match or match.group(1) not in MONTH_CODES:
            continue
        raw_price = row.get("settle")
        if raw_price in {None, "", "-"}:
            raw_price = row.get("last")
        try:
            price = float(str(raw_price).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price):
            continue
        year = 2000 + int(match.group(2))
        month_number = dt.datetime.strptime(match.group(1), "%b").month
        month = f"{year:04d}-{month_number:02d}"
        futures[month] = {
            "contract": f"ZQ{MONTH_CODES[match.group(1)]}{match.group(2)[-1]}",
            "month": month,
            "price": price,
            "observation_as_of": trade_date.isoformat(),
        }
    if not futures:
        raise RuntimeError(f"No numeric ZQ settlements found for {trade_date.isoformat()}")
    return futures


def latest_value(values: dict[str, float], as_of: dt.date) -> tuple[str, float]:
    eligible = [(date, value) for date, value in values.items() if date <= as_of.isoformat()]
    if not eligible:
        raise RuntimeError(f"No FRED observation on or before {as_of.isoformat()}")
    return max(eligible, key=lambda item: item[0])


def latest_target_range(
    upper: dict[str, float], lower: dict[str, float], as_of: dt.date
) -> tuple[str, float, float]:
    eligible = sorted(
        date for date in set(upper).intersection(lower) if date <= as_of.isoformat()
    )
    if not eligible:
        raise RuntimeError(f"No target range on or before {as_of.isoformat()}")
    date = eligible[-1]
    return date, lower[date], upper[date]


def implied_effr(price: float) -> float:
    return 100.0 - price


def decompose_meeting(
    futures: dict[str, dict[str, Any]], meetings: list[dt.date], meeting: dt.date
) -> tuple[float, float]:
    """Return start/end EFFR for one meeting month using public ZQ averages."""
    month = month_key(meeting)
    months = sorted(futures)
    if month not in futures:
        raise RuntimeError(f"Missing ZQ settlement for meeting month {month}")
    meetings_by_month: dict[str, list[dt.date]] = {}
    for item in meetings:
        meetings_by_month.setdefault(month_key(item), []).append(item)
    if len(meetings_by_month.get(month, [])) != 1:
        raise RuntimeError(f"Unsupported multiple FOMC meetings in {month}")
    target_index = months.index(month)
    anchor_index = -1
    for index in range(target_index + 1, len(months)):
        candidate = months[index]
        if candidate in meetings_by_month:
            continue
        if all(required in futures for required in month_sequence(month, candidate)):
            anchor_index = index
            break
    if anchor_index < 0:
        raise RuntimeError("No continuous non-FOMC ZQ anchor after target meeting")

    next_start = implied_effr(float(futures[months[anchor_index]]["price"]))
    state: dict[str, tuple[float, float]] = {}
    for index in range(anchor_index - 1, target_index - 1, -1):
        current_month = months[index]
        average = implied_effr(float(futures[current_month]["price"]))
        current_meeting = meetings_by_month.get(current_month, [])
        if not current_meeting:
            next_start = average
            continue
        meeting_date = current_meeting[0]
        before_days = meeting_date.day
        after_days = days_in_month(current_month) - before_days
        if before_days <= 0 or after_days < 0:
            raise RuntimeError(f"Invalid meeting-day weights for {current_month}")
        end_effr = next_start
        start_effr = (
            average * days_in_month(current_month) - after_days * end_effr
        ) / before_days
        state[current_month] = (start_effr, end_effr)
        next_start = start_effr
    if month not in state:
        raise RuntimeError("Meeting decomposition did not produce a state")
    return state[month]


def move_probabilities(
    before_midpoint: float, expected_after_midpoint: float
) -> dict[str, float]:
    expected_steps = (expected_after_midpoint - before_midpoint) / STEP
    floor_step = math.floor(expected_steps + EPSILON)
    ceil_step = math.ceil(expected_steps - EPSILON)
    fraction = expected_steps - floor_step
    states: list[tuple[float, float]] = (
        [(float(floor_step), 1.0)]
        if floor_step == ceil_step
        else [(float(floor_step), 1.0 - fraction), (float(ceil_step), fraction)]
    )
    ease = no_change = hike = 0.0
    for step, probability in states:
        midpoint = max(0.0, before_midpoint + step * STEP)
        if midpoint < before_midpoint - 0.001:
            ease += probability
        elif midpoint > before_midpoint + 0.001:
            hike += probability
        else:
            no_change += probability
    total = ease + no_change + hike
    if not math.isfinite(total) or total <= EPSILON:
        raise RuntimeError("Derived probability mass is empty")
    return {
        "ease": ease / total,
        "no_change": no_change / total,
        "hike": hike / total,
    }


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def mark_existing_stale(output: Path, manifest_path: Path, attempted_at: str, error: str) -> bool:
    if not output.exists():
        return False
    try:
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )
        snapshot["status"] = "stale"
        base_warning = str(snapshot.get("warning", "")).split(
            " Last automatic refresh failed:", 1
        )[0]
        snapshot["warning"] = (
            base_warning
            + " Last automatic refresh failed: "
            + error
        ).strip()
        snapshot["refresh_error"] = error
        serialized = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
        manifest.update(
            {
                "status": "stale",
                "refresh_attempted_at": attempted_at,
                "last_refresh_error": error,
                "fallback": "Last successful community snapshot retained; no replacement data was written.",
                "output_sha256": sha256_text(serialized),
            }
        )
        write_atomic(output, serialized)
        write_atomic(
            manifest_path,
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
        print(f"Community source failed; retained and marked stale: {error}", file=sys.stderr)
        return True
    except Exception as stale_error:
        print(f"Could not mark existing community snapshot stale: {stale_error}", file=sys.stderr)
        return False


def run(args: argparse.Namespace) -> None:
    requested = (
        dt.date.fromisoformat(args.trade_date)
        if args.trade_date
        else dt.datetime.now(dt.timezone.utc).date()
    )
    trade_date, rows = fetch_settlements(requested, args.lookback_days)
    start_date = dt.date.fromisoformat(args.start_date)
    meetings = load_calendar(Path(args.calendar))
    future_meetings = [item for item in meetings if item > trade_date]
    if not future_meetings:
        raise RuntimeError("Calendar has no future FOMC meeting")
    next_meeting = future_meetings[0]
    futures = parse_settlements(rows, trade_date)
    effr = fetch_fred("EFFR", start_date, trade_date)
    upper = fetch_fred("DFEDTARU", start_date, trade_date)
    lower = fetch_fred("DFEDTARL", start_date, trade_date)
    effr_date, current_effr = latest_value(effr, trade_date)
    range_date, current_lower, current_upper = latest_target_range(upper, lower, trade_date)
    current_midpoint = (current_lower + current_upper) / 2.0
    start_effr, end_effr = decompose_meeting(futures, meetings, next_meeting)
    expected_after = current_midpoint + (end_effr - start_effr)
    probabilities = move_probabilities(current_midpoint, expected_after)
    contract = futures[month_key(next_meeting)]
    retrieved_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stale_after = (trade_date + dt.timedelta(days=args.stale_days)).isoformat()
    now_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    status = "stale" if now_date > stale_after else "available"
    snapshot = {
        "schemaVersion": "fedpath.fedwatch-web-current.v2",
        "snapshot_id": f"fedwatch-community-current-{trade_date.isoformat()}",
        "observation_as_of": trade_date.isoformat(),
        "retrieved_at": retrieved_at,
        "source_url": CME_SETTLEMENTS_URL,
        "source_kind": "community_zq",
        "source_label": "Community ZQ settlement approximation",
        "quality_mode": "exploratory_zq_approximation",
        "strict_pit": False,
        "status": status,
        "stale_after": stale_after,
        "warning": (
            "Derived from public CME ZQ settlement data, public FRED observations, "
            "and the checked-in FOMC calendar. Not official CME FedWatch/QuikStrike; "
            "not an API response and not strict PIT."
        ),
        "meeting": {
            "meeting_date": next_meeting.isoformat(),
            "contract": contract["contract"],
            "mid_price": contract["price"],
            "target_range": {
                "lower_bps": int(round(current_lower * 100)),
                "upper_bps": int(round(current_upper * 100)),
            },
            **probabilities,
        },
    }
    serialized = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    manifest = {
        "schemaVersion": "fedpath.fedwatch-current-manifest.v1",
        "snapshot_id": snapshot["snapshot_id"],
        "source": "public CME ZQ settlement + public FRED CSV",
        "source_url": CME_SETTLEMENTS_URL,
        "fred_url": FRED_CSV_URL,
        "observation_as_of": trade_date.isoformat(),
        "retrieved_at": retrieved_at,
        "quality_mode": "exploratory_zq_approximation",
        "strict_pit": False,
        "status": status,
        "stale_after": stale_after,
        "current_effr_observation": effr_date,
        "target_range_observation": range_date,
        "warning": snapshot["warning"],
        "official_fedwatch_api": False,
        "last_good_snapshot": True,
        "output_sha256": sha256_text(serialized),
    }
    write_atomic(Path(args.output), serialized)
    write_atomic(
        Path(args.manifest),
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )
    print(
        f"Wrote community snapshot for {trade_date.isoformat()} "
        f"({next_meeting.isoformat()}, no_change={probabilities['no_change']:.4f}, "
        f"hike={probabilities['hike']:.4f})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--stale-days", type=int, default=3)
    parser.add_argument("--calendar", default="qsl/fedpath/data/import/fomc-calendar.json")
    parser.add_argument("--output", default="qsl/fedpath/data/fedwatch-community-current.json")
    parser.add_argument(
        "--manifest",
        default="qsl/fedpath/data/fedwatch-community-current-manifest.json",
    )
    args = parser.parse_args()
    output = Path(args.output)
    manifest = Path(args.manifest)
    attempted_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        run(args)
        return 0
    except Exception as exc:
        if mark_existing_stale(output, manifest, attempted_at, str(exc)):
            return 0
        print(f"Community refresh failed with no prior snapshot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
