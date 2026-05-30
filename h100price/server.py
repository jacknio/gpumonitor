#!/usr/bin/env python3
"""H100 rental price tracker.

Run a small local web app that collects NVIDIA H100 rental prices.
The implementation intentionally sticks to the Python standard library so the
first run works on a clean machine.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
STATIC_DATA_DIR = STATIC_DIR / "data"
STATIC_EXPORTS_DIR = STATIC_DIR / "exports"
DB_PATH = DATA_DIR / "h100_prices.sqlite3"
ALL_CSV_PATH = DATA_DIR / "gpu_rental_prices.csv"
ALL_XLSX_PATH = DATA_DIR / "gpu_rental_prices.xlsx"
VAST_API_KEY_PATH = APP_DIR.parent / "gpumonitor_private" / "vast_api_key.txt"
LEGACY_VAST_API_KEY_PATH = DATA_DIR / "vast_api_key.txt"
CSV_DOWNLOAD_NAME = "gpu_rental_prices.csv"
XLSX_DOWNLOAD_NAME = "gpu_rental_prices.xlsx"

EXPORT_COLUMNS = [
    "run_id",
    "observed_at",
    "source",
    "model",
    "market",
    "title",
    "price",
    "unit",
    "currency",
    "gpu_count",
    "price_per_gpu_hour",
    "normalized_price",
    "condition",
    "availability",
    "link",
    "metadata_json",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) H100PriceDesk/1.0"
)

H100_QUERY = os.getenv("H100_QUERY", "NVIDIA H100 80GB GPU")
DEFAULT_TIMEOUT = int(os.getenv("H100_TIMEOUT", "12"))

GPU_MODELS = ["H100", "H200", "A100", "B200", "B300"]
DEFAULT_GPU_MODEL = "H100"

# Vast.ai bundle filters per model. We over-fetch with numeric brackets, then
# confirm each offer by gpu_name via classify_gpu_model so look-alikes
# (GB200/GB300 superchips, neighbouring models) get dropped.
# compute_cap encodes the SM version x100 (H100/H200 = 900, B200 = 1000,
# B300 = 1030; note Vast tags RTX 50-series / RTX PRO as 1200, so Blackwell
# data-center parts must be isolated by gpu_ram, not compute_cap alone).
VAST_MODEL_FILTERS: dict[str, dict[str, Any]] = {
    "H100": {"gpu_ram": {"gte": 79000, "lt": 100000}, "compute_cap": {"gte": 900, "lt": 1000}},
    "H200": {"gpu_ram": {"gte": 100000, "lt": 160000}, "compute_cap": {"gte": 900, "lt": 1000}},
    "A100": {"gpu_ram": {"gte": 39000, "lt": 90000}, "compute_cap": {"gte": 800, "lt": 900}},
    "B200": {"gpu_ram": {"gte": 150000, "lt": 250000}, "compute_cap": {"gte": 1000, "lt": 1100}},
    "B300": {"gpu_ram": {"gte": 250000}, "compute_cap": {"gte": 1000, "lt": 1100}},
}


def classify_gpu_model(gpu_name: str) -> str | None:
    name = (gpu_name or "").upper()
    if "GB200" in name or "GB300" in name:
        return None
    if "B300" in name:
        return "B300"
    if "B200" in name:
        return "B200"
    if "H200" in name:
        return "H200"
    if "H100" in name:
        return "H100"
    if "A100" in name:
        return "A100"
    return None


def normalize_model(value: str | None) -> str:
    if not value:
        return DEFAULT_GPU_MODEL
    upper = value.strip().upper()
    return upper if upper in GPU_MODELS else DEFAULT_GPU_MODEL
DEFAULT_DAILY_AT = os.getenv("H100_DAILY_AT", "09:00")
DEFAULT_TRACK_EVERY_HOURS = float(os.getenv("H100_TRACK_EVERY_HOURS", "1"))

TRACKER_LOCK = threading.Lock()
TRACKER_STATE: dict[str, Any] = {
    "enabled": False,
    "mode": "off",
    "runAt": DEFAULT_DAILY_AT,
    "intervalHours": None,
    "lastRunId": None,
    "lastStartedAt": None,
    "lastFinishedAt": None,
    "lastError": "",
    "nextRunAt": None,
}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def unix_ms() -> int:
    return int(time.time() * 1000)


def parse_daily_at(value: str) -> tuple[int, int]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        raise ValueError("daily time must be HH:MM, for example 09:00")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("daily time must be a valid local HH:MM")
    return hour, minute


def next_daily_run_at(value: str) -> dt.datetime:
    hour, minute = parse_daily_at(value)
    now_local = dt.datetime.now().astimezone()
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += dt.timedelta(days=1)
    return candidate


def parse_interval_hours(value: float) -> float:
    interval = float(value)
    if interval <= 0 or interval > 24:
        raise ValueError("track interval must be greater than 0 and at most 24 hours")
    return interval


def next_interval_run_at(interval_hours: float) -> dt.datetime:
    interval = parse_interval_hours(interval_hours)
    now_local = dt.datetime.now().astimezone()
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    interval_seconds = int(interval * 3600)
    elapsed_seconds = int((now_local - midnight).total_seconds())
    slots_elapsed = elapsed_seconds // interval_seconds
    candidate = midnight + dt.timedelta(seconds=(slots_elapsed + 1) * interval_seconds)
    if candidate <= now_local:
        candidate += dt.timedelta(seconds=interval_seconds)
    return candidate


def update_tracker_state(**updates: Any) -> None:
    with TRACKER_LOCK:
        TRACKER_STATE.update(updates)


def tracker_snapshot() -> dict[str, Any]:
    with TRACKER_LOCK:
        return dict(TRACKER_STATE)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in {"", ".", "-", "-."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html(markup: str) -> str:
    markup = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", markup)
    markup = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    return compact_text(html.unescape(markup))


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_text(url: str, **kwargs: Any) -> str:
    raw = http_request(url, **kwargs)
    return raw.decode("utf-8", errors="replace")


def http_json(url: str, **kwargs: Any) -> Any:
    return json.loads(http_text(url, **kwargs))


def http_text_retry(url: str, *, tries: int = 4, backoff: float = 2.0, **kwargs: Any) -> str:
    last_error: Exception | None = None
    for attempt in range(tries):
        try:
            return http_text(url, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt == tries - 1:
                break
            time.sleep(backoff * (attempt + 1))
    assert last_error is not None
    raise last_error


def http_json_retry(url: str, *, tries: int = 4, backoff: float = 2.0, **kwargs: Any) -> Any:
    return json.loads(http_text_retry(url, tries=tries, backoff=backoff, **kwargs))


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def infer_gpu_count(title: str) -> int:
    title_u = title.upper()
    patterns = [
        r"(\d+)\s*[Xx]\s*(?:NVIDIA\s*)?H100",
        r"(\d+)\s*(?:GPU|GPUS)\b",
        r"(?:H100|H-100).{0,16}(\d+)\s*[Xx]",
    ]
    for pattern in patterns:
        match = re.search(pattern, title_u)
        if match:
            count = int(match.group(1))
            if 1 <= count <= 32:
                return count
    return 1


def looks_like_h100_rental(title: str, price: float | None = None) -> bool:
    title_u = title.upper()
    if "H100" not in title_u:
        return False
    bad_terms = [
        "A100",
        "H200",
        "CONTACT SALES",
        "CALL",
        "COMING SOON",
    ]
    if any(term in title_u for term in bad_terms):
        return False
    if price is not None and not (0.25 <= price <= 50):
        return False
    return True


def observation(
    *,
    source: str,
    market: str,
    title: str,
    price: float,
    unit: str,
    model: str = DEFAULT_GPU_MODEL,
    currency: str = "USD",
    link: str = "",
    gpu_count: int = 1,
    price_per_gpu_hour: float | None = None,
    condition: str = "",
    availability: str = "",
    observed_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if gpu_count < 1:
        gpu_count = 1
    if market != "rental":
        raise ValueError("only rental observations are supported")
    if price_per_gpu_hour is None:
        price_per_gpu_hour = price if unit == "gpu_hour" else price / gpu_count
    normalized = price_per_gpu_hour
    return {
        "source": source,
        "model": normalize_model(model),
        "market": market,
        "title": compact_text(title),
        "price": round(float(price), 6),
        "unit": unit,
        "currency": currency,
        "gpuCount": int(gpu_count),
        "pricePerGpuHour": round(float(price_per_gpu_hour), 6) if price_per_gpu_hour is not None else None,
        "normalizedPrice": round(float(normalized), 6),
        "condition": condition,
        "availability": availability,
        "link": link,
        "observedAt": observed_at or now_utc(),
        "metadata": metadata or {},
    }


@dataclass
class SourceSpec:
    name: str
    market: str
    link: str
    collector: Callable[[], list[dict[str, Any]]]
    requires: str = ""


def parse_lambda_pricing(text: str, *, observed_at: str | None = None, link: str = "https://lambda.ai/pricing", basis: str = "official pricing page") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    pattern = re.compile(r"(NVIDIA H100\s+(?:SXM|PCIe)[^$]{0,150}?)\$\s*([0-9]+(?:\.[0-9]+)?)", re.I)
    for match in pattern.finditer(text):
        title = compact_text(match.group(1))
        price = parse_float(match.group(2))
        if price is None or not looks_like_h100_rental(title, price):
            continue
        key = (title, price)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            observation(
                source="Lambda",
                market="rental",
                title=title,
                price=price,
                unit="gpu_hour",
                condition="on-demand instance",
                availability="first-come",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis},
            )
        )
    return results


def collect_lambda() -> list[dict[str, Any]]:
    url = "https://lambda.ai/pricing"
    return parse_lambda_pricing(strip_html(http_text(url)), link=url)


def parse_crusoe_pricing(text: str, *, observed_at: str | None = None, link: str = "https://www.crusoe.ai/cloud/pricing", basis: str = "official pricing page") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    pattern = re.compile(r"(NVIDIA H100\s+80GB\s+HGX)\s+\$([0-9]+(?:\.[0-9]+)?)/GPU-hr", re.I)
    for match in pattern.finditer(text):
        price = parse_float(match.group(2))
        if price is None:
            continue
        results.append(
            observation(
                source="Crusoe",
                market="rental",
                title=match.group(1),
                price=price,
                unit="gpu_hour",
                condition="on-demand",
                availability="public price",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis},
            )
        )
    return results


def collect_crusoe() -> list[dict[str, Any]]:
    url = "https://www.crusoe.ai/cloud/pricing"
    return parse_crusoe_pricing(strip_html(http_text(url)), link=url)


def parse_coreweave_pricing(text: str, *, observed_at: str | None = None, link: str = "https://www.coreweave.com/pricing", basis: str = "official pricing page") -> list[dict[str, Any]]:
    start = text.find("NVIDIA HGX H100")
    end = text.find("NVIDIA HGX H200", start + 1)
    if start < 0:
        return []
    section = text[start : end if end > start else start + 600]
    price_match = re.search(r"On-Demand Price:\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*Hour", section, re.I)
    spot_match = re.search(r"Spot Price:\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*Hour", section, re.I)
    count_match = re.search(r"(\d+)\s+GPU Count", section, re.I)
    price = parse_float(price_match.group(1)) if price_match else None
    gpu_count = int(count_match.group(1)) if count_match else 8
    results: list[dict[str, Any]] = []
    if price is not None:
        results.append(
            observation(
                source="CoreWeave",
                market="rental",
                title="NVIDIA HGX H100",
                price=price,
                unit="instance_hour",
                gpu_count=gpu_count,
                condition="on-demand cluster",
                availability="public price",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis, "instancePrice": price},
            )
        )
    spot = parse_float(spot_match.group(1)) if spot_match else None
    if spot is not None:
        results.append(
            observation(
                source="CoreWeave",
                market="rental",
                title="NVIDIA HGX H100 spot",
                price=spot,
                unit="instance_hour",
                gpu_count=gpu_count,
                condition="spot",
                availability="variable",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis, "instancePrice": spot},
            )
        )
    return results


def collect_coreweave() -> list[dict[str, Any]]:
    url = "https://www.coreweave.com/pricing"
    return parse_coreweave_pricing(strip_html(http_text(url)), link=url)


def parse_aws_capacity_blocks(text: str, *, observed_at: str | None = None, link: str = "https://aws.amazon.com/ec2/capacityblocks/pricing/", basis: str = "official capacity block pricing") -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(p5(?:\.4|\.48)xlarge)\s+(.{0,130}?)\s+\$?\s*([0-9]+(?:\.[0-9]+)?)\s+USD\s*"
        r"\(\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s+USD\s*\)\s+(\d+)\s*x\s*H100",
        re.I,
    )
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for match in pattern.finditer(text):
        instance_type = match.group(1)
        region = compact_text(match.group(2))[-80:]
        instance_price = parse_float(match.group(3))
        per_gpu = parse_float(match.group(4))
        gpu_count = int(match.group(5))
        if instance_price is None or per_gpu is None:
            continue
        key = (instance_type, region, per_gpu)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            observation(
                source="AWS Capacity Blocks",
                market="rental",
                title=f"{instance_type} {region}",
                price=instance_price,
                unit="instance_hour",
                gpu_count=gpu_count,
                price_per_gpu_hour=per_gpu,
                condition="capacity block",
                availability="reservation",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis, "region": region},
            )
        )
    return results


def collect_aws_capacity_blocks() -> list[dict[str, Any]]:
    url = "https://aws.amazon.com/ec2/capacityblocks/pricing/"
    return parse_aws_capacity_blocks(strip_html(http_text(url)), link=url)


def parse_runpod_pricing(text: str, *, observed_at: str | None = None, link: str = "https://www.runpod.io/gpu-instance/pricing", basis: str = "official pricing page scrape") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    pattern = re.compile(r"(H100\s+(?:PCIe|SXM|NVL)[^$]{0,90})\$\s*([0-9]+(?:\.[0-9]+)?)\s*/?\s*(?:hr|hour|h)", re.I)
    for match in pattern.finditer(text):
        price = parse_float(match.group(2))
        if price is None or price <= 0:
            continue
        results.append(
            observation(
                source="RunPod",
                market="rental",
                title=match.group(1),
                price=price,
                unit="gpu_hour",
                condition="pod",
                availability="public page",
                link=link,
                observed_at=observed_at,
                metadata={"basis": basis},
            )
        )
    return results


def collect_runpod_page() -> list[dict[str, Any]]:
    url = "https://www.runpod.io/gpu-instance/pricing"
    return parse_runpod_pricing(strip_html(http_text(url)), link=url)


def configured_vast_api_key_path() -> Path:
    raw_path = os.getenv("VAST_API_KEY_FILE")
    if not raw_path:
        if VAST_API_KEY_PATH.exists():
            return VAST_API_KEY_PATH
        return LEGACY_VAST_API_KEY_PATH
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def vast_api_token() -> str:
    token = os.getenv("VAST_API_KEY") or os.getenv("VAST_TOKEN")
    if token:
        return token.strip()
    try:
        return configured_vast_api_key_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def vast_search(token: str, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    url = "https://console.vast.ai/api/v0/bundles/"
    payload = {
        "limit": limit,
        "type": "ondemand",
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "order": [["dph_total", "asc"]],
        **filters,
    }
    raw = http_request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    parsed = json.loads(raw.decode("utf-8", errors="replace"))
    return parsed.get("offers", parsed if isinstance(parsed, list) else [])


def collect_vast() -> list[dict[str, Any]]:
    token = vast_api_token()
    if not token:
        raise RuntimeError("set VAST_API_KEY or ../gpumonitor_private/vast_api_key.txt for live Vast.ai offers")
    limit = int(os.getenv("VAST_LIMIT", "80"))
    results: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    query_cache: dict[str, list[dict[str, Any]]] = {}
    for model in GPU_MODELS:
        filters = VAST_MODEL_FILTERS[model]
        cache_key = json.dumps(filters, sort_keys=True)
        if cache_key in query_cache:
            offers = query_cache[cache_key]
        else:
            try:
                offers = vast_search(token, filters, limit)
            except Exception:
                if os.getenv("H100_DEBUG") == "1":
                    traceback.print_exc()
                offers = []
            query_cache[cache_key] = offers
        for offer in offers:
            name = str(offer.get("gpu_name") or "")
            if classify_gpu_model(name) != model:
                continue
            gpu_count = int(offer.get("num_gpus") or 1)
            total_hour = (
                parse_float((offer.get("search") or {}).get("totalHour"))
                or parse_float(offer.get("dph_total"))
                or parse_float((offer.get("instance") or {}).get("totalHour"))
            )
            if total_hour is None:
                continue
            offer_id = offer.get("id") or offer.get("ask_contract_id") or offer.get("bundle_id")
            key = (offer_id, model)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                observation(
                    source="Vast.ai",
                    market="rental",
                    model=model,
                    title=f"{name} x{gpu_count}",
                    price=total_hour,
                    unit="instance_hour",
                    gpu_count=gpu_count,
                    condition="on-demand marketplace",
                    availability="rentable",
                    link="https://cloud.vast.ai/create/",
                    metadata={
                        "basis": "official bundles API",
                        "offerId": offer_id,
                        "reliability": offer.get("reliability"),
                        "location": offer.get("geolocation"),
                    },
                )
            )
    return results


def aws_credentials() -> tuple[str, str, str | None]:
    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    if not (access_key and secret_key):
        raise RuntimeError("set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY for AWS live APIs")
    return access_key, secret_key, session_token


def aws_sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def aws_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    date_key = aws_sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    region_key = aws_sign(date_key, region)
    service_key = aws_sign(region_key, service)
    return aws_sign(service_key, "aws4_request")


def aws_ec2_query(region: str, params: dict[str, str]) -> ET.Element:
    access_key, secret_key, session_token = aws_credentials()
    service = "ec2"
    host = f"ec2.{region}.amazonaws.com"
    endpoint = f"https://{host}/"
    query_params = {"Version": "2016-11-15", **params}
    canonical_query = urllib.parse.urlencode(
        sorted(query_params.items()),
        quote_via=urllib.parse.quote,
        safe="-_.~",
    )
    amz_date = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    signed_headers = ";".join(sorted(headers))
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(["GET", "/", canonical_query, canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        aws_signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request_headers = {
        "Authorization": authorization,
        "Host": host,
        "X-Amz-Date": amz_date,
        "User-Agent": USER_AGENT,
    }
    if session_token:
        request_headers["X-Amz-Security-Token"] = session_token
    raw = http_request(f"{endpoint}?{canonical_query}", headers=request_headers, timeout=int(os.getenv("AWS_TIMEOUT", "20")))
    return ET.fromstring(raw)


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_child_text(element: ET.Element, name: str) -> str:
    for child in list(element):
        if xml_local_name(child.tag) == name:
            return child.text or ""
    return ""


def xml_items(root: ET.Element, parent_name: str) -> list[ET.Element]:
    for element in root.iter():
        if xml_local_name(element.tag) == parent_name:
            return [child for child in list(element) if xml_local_name(child.tag) == "item"]
    return []


def aws_regions(default: str = "us-east-1,us-east-2,us-west-2,us-west-1,ap-northeast-1,ap-south-1,ap-southeast-2,ap-southeast-3,eu-west-2,eu-north-1,sa-east-1") -> list[str]:
    value = os.getenv("AWS_H100_REGIONS") or os.getenv("AWS_REGIONS") or default
    return [region.strip() for region in value.split(",") if region.strip()]


def aws_instance_types(default: str = "p5.48xlarge,p5.4xlarge") -> list[str]:
    value = os.getenv("AWS_H100_INSTANCE_TYPES", default)
    return [instance.strip() for instance in value.split(",") if instance.strip()]


def aws_capacity_durations(default: str = "24,168,336") -> list[int]:
    value = os.getenv("AWS_CAPACITY_DURATION_HOURS", default)
    durations: list[int] = []
    for item in value.split(","):
        parsed = parse_float(item.strip())
        if parsed:
            durations.append(int(parsed))
    return durations or [24]


def aws_gpu_count(instance_type: str) -> int:
    if instance_type == "p5.48xlarge":
        return 8
    if instance_type == "p5.4xlarge":
        return 1
    return infer_gpu_count(instance_type)


def aws_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_aws_capacity_block_offers() -> list[dict[str, Any]]:
    aws_credentials()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(days=int(os.getenv("AWS_CAPACITY_LOOKAHEAD_DAYS", "14")))
    instance_count = int(os.getenv("AWS_CAPACITY_INSTANCE_COUNT", "1"))
    for region in aws_regions():
        for instance_type in aws_instance_types():
            for duration_hours in aws_capacity_durations():
                try:
                    root = aws_ec2_query(
                        region,
                        {
                            "Action": "DescribeCapacityBlockOfferings",
                            "InstanceType": instance_type,
                            "InstanceCount": str(instance_count),
                            "CapacityDurationHours": str(duration_hours),
                            "StartDateRange": aws_iso(now),
                            "EndDateRange": aws_iso(end),
                            "AllAvailabilityZones": "true",
                            "MaxResults": "1000",
                        },
                    )
                except Exception as exc:
                    errors.append(f"{region}/{instance_type}/{duration_hours}h: {exc}")
                    if os.getenv("H100_DEBUG") == "1":
                        traceback.print_exc()
                    continue
                for item in xml_items(root, "capacityBlockOfferingSet"):
                    upfront_fee = parse_float(xml_child_text(item, "upfrontFee"))
                    if upfront_fee is None:
                        continue
                    offering_id = xml_child_text(item, "capacityBlockOfferingId")
                    offered_type = xml_child_text(item, "instanceType") or instance_type
                    offered_count = int(parse_float(xml_child_text(item, "instanceCount")) or instance_count)
                    hours = int(parse_float(xml_child_text(item, "capacityBlockDurationHours")) or duration_hours)
                    minutes = int(parse_float(xml_child_text(item, "capacityBlockDurationMinutes")) or 0)
                    duration = hours + minutes / 60
                    if duration <= 0:
                        continue
                    gpu_count = aws_gpu_count(offered_type) * max(1, offered_count)
                    hourly_total = upfront_fee / duration
                    az = xml_child_text(item, "availabilityZone")
                    start_date = xml_child_text(item, "startDate")
                    end_date = xml_child_text(item, "endDate")
                    results.append(
                        observation(
                            source="AWS Capacity Block Offers",
                            market="rental",
                            title=f"{offered_type} x{offered_count} {az} {start_date}",
                            price=hourly_total,
                            unit="instance_hour",
                            currency=xml_child_text(item, "currencyCode") or "USD",
                            gpu_count=gpu_count,
                            condition="capacity block offering",
                            availability="available to reserve",
                            link="https://console.aws.amazon.com/ec2/home#CapacityReservations:",
                            metadata={
                                "basis": "DescribeCapacityBlockOfferings API",
                                "region": region,
                                "availabilityZone": az,
                                "offeringId": offering_id,
                                "startDate": start_date,
                                "endDate": end_date,
                                "upfrontFee": upfront_fee,
                                "durationHours": duration,
                                "instanceCount": offered_count,
                            },
                        )
                    )
    if not results and errors:
        raise RuntimeError("; ".join(errors[:3]))
    return results


def collect_aws_spot_history() -> list[dict[str, Any]]:
    aws_credentials()
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=int(os.getenv("AWS_SPOT_LOOKBACK_HOURS", "24")))
    for region in aws_regions():
        for instance_type in aws_instance_types():
            try:
                root = aws_ec2_query(
                    region,
                    {
                        "Action": "DescribeSpotPriceHistory",
                        "InstanceType.1": instance_type,
                        "ProductDescription.1": os.getenv("AWS_SPOT_PRODUCT_DESCRIPTION", "Linux/UNIX"),
                        "StartTime": aws_iso(start),
                        "MaxResults": os.getenv("AWS_SPOT_MAX_RESULTS", "100"),
                    },
                )
            except Exception as exc:
                errors.append(f"{region}/{instance_type}: {exc}")
                if os.getenv("H100_DEBUG") == "1":
                    traceback.print_exc()
                continue
            seen: set[tuple[str, str, float]] = set()
            for item in xml_items(root, "spotPriceHistorySet"):
                price = parse_float(xml_child_text(item, "spotPrice"))
                if price is None:
                    continue
                offered_type = xml_child_text(item, "instanceType") or instance_type
                az = xml_child_text(item, "availabilityZone")
                timestamp = xml_child_text(item, "timestamp")
                key = (offered_type, az, price)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    observation(
                        source="AWS Spot History",
                        market="rental",
                        title=f"{offered_type} {az}",
                        price=price,
                        unit="instance_hour",
                        gpu_count=aws_gpu_count(offered_type),
                        condition="spot",
                        availability="price history, not capacity guarantee",
                        link="https://console.aws.amazon.com/ec2/home#SpotInstances:",
                        observed_at=timestamp or None,
                        metadata={
                            "basis": "DescribeSpotPriceHistory API",
                            "region": region,
                            "availabilityZone": az,
                            "timestamp": timestamp,
                        },
                    )
                )
    if not results and errors:
        raise RuntimeError("; ".join(errors[:3]))
    return results


def collect_manual() -> list[dict[str, Any]]:
    manual_path = Path(os.getenv("MANUAL_PRICE_FILE", DATA_DIR / "manual_prices.json"))
    if not manual_path.exists():
        return []
    with manual_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = raw.get("prices", raw if isinstance(raw, list) else [])
    results: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source", "Manual"))
        market = str(row.get("market", "rental"))
        if market != "rental":
            continue
        title = str(row.get("title", "H100"))
        price = parse_float(row.get("price"))
        if price is None:
            continue
        gpu_count = int(row.get("gpuCount") or row.get("gpu_count") or infer_gpu_count(title))
        unit = str(row.get("unit") or "gpu_hour")
        results.append(
            observation(
                source=source,
                market=market,
                title=title,
                price=price,
                unit=unit,
                currency=str(row.get("currency", "USD")),
                gpu_count=gpu_count,
                price_per_gpu_hour=parse_float(row.get("pricePerGpuHour") or row.get("price_per_gpu_hour")),
                condition=str(row.get("condition", "")),
                availability=str(row.get("availability", "manual")),
                link=str(row.get("link", "")),
                metadata={"basis": "manual file", **dict(row.get("metadata", {}))},
            )
        )
    return results


STATIC_SOURCE_SPECS: list[SourceSpec] = [
    SourceSpec("Lambda", "rental", "https://lambda.ai/pricing", collect_lambda),
    SourceSpec("Crusoe", "rental", "https://www.crusoe.ai/cloud/pricing", collect_crusoe),
    SourceSpec("CoreWeave", "rental", "https://www.coreweave.com/pricing", collect_coreweave),
    SourceSpec("AWS Capacity Blocks", "rental", "https://aws.amazon.com/ec2/capacityblocks/pricing/", collect_aws_capacity_blocks),
    SourceSpec("RunPod", "rental", "https://www.runpod.io/gpu-instance/pricing", collect_runpod_page),
]

OTHER_LIVE_SOURCE_SPECS: list[SourceSpec] = [
    SourceSpec("AWS Capacity Block Offers", "rental", "https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeCapacityBlockOfferings.html", collect_aws_capacity_block_offers, "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"),
    SourceSpec("AWS Spot History", "rental", "https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-spot-price-history.html", collect_aws_spot_history, "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"),
    SourceSpec("Manual", "rental", str(DATA_DIR / "manual_prices.json"), collect_manual),
]

VAST_SOURCE_SPECS: list[SourceSpec] = [
    SourceSpec("Vast.ai", "rental", "https://docs.vast.ai/api-reference/search/search-offers", collect_vast, "VAST_API_KEY"),
]

SOURCE_SPECS: list[SourceSpec] = (
    VAST_SOURCE_SPECS
    + (OTHER_LIVE_SOURCE_SPECS if os.getenv("H100_INCLUDE_OTHER_LIVE_SOURCES") == "1" else [])
    + (STATIC_SOURCE_SPECS if os.getenv("H100_INCLUDE_STATIC_SOURCES") == "1" else [])
)

WAYBACK_PARSERS: dict[str, Callable[[str, str, str], list[dict[str, Any]]]] = {
    "Lambda": lambda text, observed_at, link: parse_lambda_pricing(
        text,
        observed_at=observed_at,
        link=link,
        basis="Wayback pricing page snapshot",
    ),
    "Crusoe": lambda text, observed_at, link: parse_crusoe_pricing(
        text,
        observed_at=observed_at,
        link=link,
        basis="Wayback pricing page snapshot",
    ),
    "CoreWeave": lambda text, observed_at, link: parse_coreweave_pricing(
        text,
        observed_at=observed_at,
        link=link,
        basis="Wayback pricing page snapshot",
    ),
    "AWS Capacity Blocks": lambda text, observed_at, link: parse_aws_capacity_blocks(
        text,
        observed_at=observed_at,
        link=link,
        basis="Wayback pricing page snapshot",
    ),
    "RunPod": lambda text, observed_at, link: parse_runpod_pricing(
        text,
        observed_at=observed_at,
        link=link,
        basis="Wayback pricing page snapshot",
    ),
}


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'H100',
                market TEXT NOT NULL,
                title TEXT NOT NULL,
                price REAL NOT NULL,
                unit TEXT NOT NULL,
                currency TEXT NOT NULL,
                gpu_count INTEGER NOT NULL,
                price_per_gpu_hour REAL,
                normalized_price REAL NOT NULL,
                condition TEXT,
                availability TEXT,
                link TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                market TEXT NOT NULL,
                ok INTEGER NOT NULL,
                count INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                error TEXT,
                requires TEXT,
                link TEXT
            )
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
        if "model" not in existing_columns:
            conn.execute("ALTER TABLE observations ADD COLUMN model TEXT NOT NULL DEFAULT 'H100'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_time ON observations(observed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_model ON observations(model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_runs_run ON source_runs(run_id)")


def load_export_rows() -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_id, observed_at, source, model, market, title, price, unit,
                   currency, gpu_count, price_per_gpu_hour, normalized_price,
                   condition, availability, link, metadata_json
            FROM observations
            WHERE market = 'rental'
            ORDER BY observed_at ASC, id ASC
            """
        ).fetchall()
    return [{column: row[column] for column in EXPORT_COLUMNS} for row in rows]


def model_csv_path(model: str) -> Path:
    return DATA_DIR / f"{normalize_model(model).lower()}_rental_prices.csv"


def model_xlsx_path(model: str) -> Path:
    return DATA_DIR / f"{normalize_model(model).lower()}_rental_prices.xlsx"


def export_observations_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def excel_column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xlsx_cell(value: Any, column_index: int, row_index: int) -> str:
    ref = f"{excel_column_name(column_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = html.escape(str(value), quote=False)
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def xlsx_worksheet_xml(rows: list[dict[str, Any]]) -> str:
    sheet_rows = [EXPORT_COLUMNS] + [[row.get(column) for column in EXPORT_COLUMNS] for row in rows]
    worksheet_rows = []
    for row_index, row in enumerate(sheet_rows, start=1):
        cells = "".join(xlsx_cell(value, column_index, row_index) for column_index, value in enumerate(row))
        worksheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(worksheet_rows)}</sheetData>'
        "</worksheet>"
    )


def xlsx_sheet_name(value: str) -> str:
    name = re.sub(r"[\[\]\*\?/\\:]", " ", value).strip() or "Sheet"
    return name[:31]


def xlsx_sheet_sets(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    sheets: list[tuple[str, list[dict[str, Any]]]] = [("All", rows)]
    for model in GPU_MODELS:
        model_rows = [row for row in rows if normalize_model(str(row.get("model", ""))) == model]
        sheets.append((model, model_rows))
    return sheets


def export_observations_xlsx(path: Path, sheets: list[tuple[str, list[dict[str, Any]]]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index, _ in enumerate(sheets, start=1)
    )
    workbook_sheets = "".join(
        f'<sheet name="{html.escape(xlsx_sheet_name(name), quote=True)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, start=1)
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index, _ in enumerate(sheets, start=1)
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{sheet_overrides}"
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{workbook_sheets}</sheets>"
            "</workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}"
            "</Relationships>",
        )
        for index, (_, sheet_rows) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", xlsx_worksheet_xml(sheet_rows))


def export_observations_files() -> None:
    rows = load_export_rows()
    export_observations_csv(ALL_CSV_PATH, rows)
    export_observations_xlsx(ALL_XLSX_PATH, xlsx_sheet_sets(rows))
    for model in GPU_MODELS:
        model_rows = [row for row in rows if normalize_model(str(row.get("model", ""))) == model]
        export_observations_csv(model_csv_path(model), model_rows)
        export_observations_xlsx(model_xlsx_path(model), [(model, model_rows)])


def save_run(run_id: str, observations: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        for item in observations:
            conn.execute(
                """
                INSERT INTO observations (
                    run_id, observed_at, source, model, market, title, price, unit, currency,
                    gpu_count, price_per_gpu_hour, normalized_price, condition,
                    availability, link, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item["observedAt"],
                    item["source"],
                    item.get("model", DEFAULT_GPU_MODEL),
                    item["market"],
                    item["title"],
                    item["price"],
                    item["unit"],
                    item["currency"],
                    item["gpuCount"],
                    item.get("pricePerGpuHour"),
                    item["normalizedPrice"],
                    item.get("condition", ""),
                    item.get("availability", ""),
                    item.get("link", ""),
                    json.dumps(item.get("metadata", {}), separators=(",", ":")),
                ),
            )
        observed_at = now_utc()
        for status in statuses:
            conn.execute(
                """
                INSERT INTO source_runs (
                    run_id, observed_at, source, market, ok, count, latency_ms,
                    error, requires, link
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    observed_at,
                    status["source"],
                    status["market"],
                    1 if status["ok"] else 0,
                    status["count"],
                    status["latencyMs"],
                    status.get("error", ""),
                    status.get("requires", ""),
                    status.get("link", ""),
                ),
            )
    if os.getenv("H100_EXPORT_EXCEL", "1") != "0":
        try:
            export_observations_files()
        except Exception as exc:
            print(f"[export] failed: {exc}", file=sys.stderr)
    if not run_id.startswith("backfill-") and os.getenv("H100_EXPORT_STATIC", "1") != "0":
        try:
            export_static_site_assets(refresh_exports=False)
        except Exception as exc:
            print(f"[static-export] failed: {exc}", file=sys.stderr)


def run_id_exists(run_id: str) -> bool:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT 1 FROM source_runs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
    return row is not None


def row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
    metadata = {}
    if row["metadata_json"]:
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            metadata = {}
    return {
        "source": row["source"],
        "model": row["model"] if "model" in row.keys() else DEFAULT_GPU_MODEL,
        "market": row["market"],
        "title": row["title"],
        "price": row["price"],
        "unit": row["unit"],
        "currency": row["currency"],
        "gpuCount": row["gpu_count"],
        "pricePerGpuHour": row["price_per_gpu_hour"],
        "normalizedPrice": row["normalized_price"],
        "condition": row["condition"] or "",
        "availability": row["availability"] or "",
        "link": row["link"] or "",
        "observedAt": row["observed_at"],
        "metadata": metadata,
    }


def load_latest_run() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        run_row = conn.execute(
            """
            SELECT sr.run_id
            FROM source_runs sr
            WHERE sr.run_id NOT LIKE 'backfill-%'
              AND EXISTS (SELECT 1 FROM observations o WHERE o.run_id = sr.run_id)
            ORDER BY sr.id DESC
            LIMIT 1
            """
        ).fetchone()
        if not run_row:
            run_row = conn.execute("SELECT run_id FROM source_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not run_row:
            return [], [], None
        run_id = run_row["run_id"]
        obs_rows = conn.execute(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY normalized_price ASC",
            (run_id,),
        ).fetchall()
        status_rows = conn.execute(
            "SELECT * FROM source_runs WHERE run_id = ? ORDER BY source ASC",
            (run_id,),
        ).fetchall()
    observations = [row_to_observation(row) for row in obs_rows]
    statuses = [
        {
            "source": row["source"],
            "market": row["market"],
            "ok": bool(row["ok"]),
            "count": row["count"],
            "latencyMs": row["latency_ms"],
            "error": row["error"] or "",
            "requires": row["requires"] or "",
            "link": row["link"] or "",
        }
        for row in status_rows
    ]
    return observations, statuses, run_id


def latest_payload_for_model(model: str) -> dict[str, Any]:
    observations, statuses, run_id = load_latest_run()
    model_counts: dict[str, int] = {name: 0 for name in GPU_MODELS}
    for obs in observations:
        key = obs.get("model", DEFAULT_GPU_MODEL)
        model_counts[key] = model_counts.get(key, 0) + 1
    selected_model = normalize_model(model)
    filtered = [obs for obs in observations if obs.get("model", DEFAULT_GPU_MODEL) == selected_model]
    return payload_for(
        filtered,
        statuses,
        run_id=run_id,
        demo=False,
        history=load_history(limit=int(os.getenv("H100_STATIC_HISTORY_LIMIT", "100000")), model=selected_model),
        model=selected_model,
        model_counts=model_counts,
    )


def static_payload_for_model(model: str, exported_at: str) -> dict[str, Any]:
    payload = latest_payload_for_model(model)
    payload["staticSnapshot"] = True
    payload["staticExportedAt"] = exported_at
    return payload


def export_static_site_assets(refresh_exports: bool = True) -> dict[str, Any]:
    if refresh_exports and os.getenv("H100_EXPORT_EXCEL", "1") != "0":
        export_observations_files()
    STATIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    exported_at = now_utc()
    data_files = []
    for model in GPU_MODELS:
        path = STATIC_DATA_DIR / f"monitor_{model.lower()}.json"
        payload = static_payload_for_model(model, exported_at)
        path.write_text(json.dumps(payload, indent=2, separators=(",", ": ")), encoding="utf-8")
        data_files.append(str(path))

    default_payload = static_payload_for_model(DEFAULT_GPU_MODEL, exported_at)
    default_path = STATIC_DATA_DIR / "monitor.json"
    default_path.write_text(json.dumps(default_payload, indent=2, separators=(",", ": ")), encoding="utf-8")
    data_files.append(str(default_path))

    export_sources = [
        ALL_CSV_PATH,
        ALL_XLSX_PATH,
        *[model_csv_path(model) for model in GPU_MODELS],
        *[model_xlsx_path(model) for model in GPU_MODELS],
    ]
    export_files = []
    for source_path in export_sources:
        if source_path.exists():
            target_path = STATIC_EXPORTS_DIR / source_path.name
            shutil.copy2(source_path, target_path)
            export_files.append(str(target_path))

    return {
        "staticPublishPath": str(STATIC_DIR),
        "exportedAt": exported_at,
        "dataFiles": data_files,
        "exportFiles": export_files,
    }


def load_history(limit: int = 500, model: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if model:
            rows = conn.execute(
                """
                SELECT observed_at, source, model, market, title, normalized_price,
                       price_per_gpu_hour, price, unit, currency, gpu_count
                FROM observations
                WHERE model = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (normalize_model(model), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT observed_at, source, model, market, title, normalized_price,
                       price_per_gpu_hour, price, unit, currency, gpu_count
                FROM observations
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [
        {
            "observedAt": row["observed_at"],
            "source": row["source"],
            "model": row["model"],
            "market": row["market"],
            "title": row["title"],
            "normalizedPrice": row["normalized_price"],
            "pricePerGpuHour": row["price_per_gpu_hour"],
            "price": row["price"],
            "unit": row["unit"],
            "currency": row["currency"],
            "gpuCount": row["gpu_count"],
        }
        for row in rows
    ]


def run_collectors(selected: set[str] | None = None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    all_observations: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for spec in SOURCE_SPECS:
        if selected and spec.name not in selected:
            continue
        start = unix_ms()
        error = ""
        source_observations: list[dict[str, Any]] = []
        ok = False
        try:
            source_observations = spec.collector()
            ok = len(source_observations) > 0
            if not source_observations:
                error = "optional manual file missing or empty" if spec.name == "Manual" else "no H100 prices found"
        except Exception as exc:
            ok = False
            error = str(exc)
            if os.getenv("H100_DEBUG") == "1":
                error = f"{error}\n{traceback.format_exc()}"
        latency = unix_ms() - start
        all_observations.extend(source_observations)
        statuses.append(
            {
                "source": spec.name,
                "market": spec.market,
                "ok": ok,
                "count": len(source_observations),
                "latencyMs": latency,
                "error": error,
                "requires": spec.requires if (error and spec.requires) else "",
                "link": spec.link,
            }
        )
    save_run(run_id, all_observations, statuses)
    return run_id, all_observations, statuses


def timestamp_to_iso(timestamp: str) -> str:
    parsed = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def source_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def wayback_cdx_snapshots(url: str, *, from_date: dt.date, to_date: dt.date, limit: int = 400) -> list[dict[str, str]]:
    params = {
        "url": url,
        "from": from_date.strftime("%Y%m%d"),
        "to": to_date.strftime("%Y%m%d"),
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype",
        "filter": "statuscode:200",
        "collapse": "timestamp:8",
        "limit": str(limit),
    }
    endpoint = f"https://web.archive.org/cdx?{urllib.parse.urlencode(params)}"
    rows = http_json_retry(endpoint, timeout=int(os.getenv("H100_WAYBACK_TIMEOUT", "60")))
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    snapshots: list[dict[str, str]] = []
    for row in rows[1:]:
        item = {header[idx]: value for idx, value in enumerate(row)}
        timestamp = item.get("timestamp", "")
        if re.fullmatch(r"\d{14}", timestamp):
            snapshots.append(item)
    return snapshots


def wayback_snapshot_url(timestamp: str, original_url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


def backfill_wayback(days: int = 365, selected: set[str] | None = None) -> dict[str, Any]:
    today = dt.datetime.now(dt.timezone.utc).date()
    from_date = today - dt.timedelta(days=days)
    sleep_seconds = float(os.getenv("H100_BACKFILL_SLEEP", "0.05"))
    limit = int(os.getenv("H100_BACKFILL_LIMIT", "400"))
    summary = {
        "from": from_date.isoformat(),
        "to": today.isoformat(),
        "sources": [],
        "runsSaved": 0,
        "observationsSaved": 0,
        "skippedExisting": 0,
        "snapshotsChecked": 0,
    }
    specs = [spec for spec in SOURCE_SPECS if spec.name in WAYBACK_PARSERS]
    if selected:
        specs = [spec for spec in specs if spec.name in selected]

    for spec in specs:
        parser = WAYBACK_PARSERS[spec.name]
        source_summary = {
            "source": spec.name,
            "snapshots": 0,
            "parsed": 0,
            "observations": 0,
            "skippedExisting": 0,
            "errors": [],
        }
        try:
            snapshots = wayback_cdx_snapshots(spec.link, from_date=from_date, to_date=today, limit=limit)
        except Exception as exc:
            source_summary["errors"].append(f"CDX lookup failed: {exc}")
            summary["sources"].append(source_summary)
            continue

        source_summary["snapshots"] = len(snapshots)
        summary["snapshotsChecked"] += len(snapshots)
        for snapshot in snapshots:
            timestamp = snapshot["timestamp"]
            day = timestamp[:8]
            run_id = f"backfill-{source_slug(spec.name)}-{day}"
            if run_id_exists(run_id):
                source_summary["skippedExisting"] += 1
                summary["skippedExisting"] += 1
                continue
            snapshot_url = wayback_snapshot_url(timestamp, spec.link)
            start = unix_ms()
            error = ""
            observations: list[dict[str, Any]] = []
            try:
                text = strip_html(http_text_retry(snapshot_url, timeout=int(os.getenv("H100_WAYBACK_TIMEOUT", "60"))))
                observations = parser(text, timestamp_to_iso(timestamp), snapshot_url)
            except Exception as exc:
                error = str(exc)
            latency = unix_ms() - start
            if observations:
                status = {
                    "source": spec.name,
                    "market": spec.market,
                    "ok": True,
                    "count": len(observations),
                    "latencyMs": latency,
                    "error": "",
                    "requires": "",
                    "link": snapshot_url,
                }
                save_run(run_id, observations, [status])
                source_summary["parsed"] += 1
                source_summary["observations"] += len(observations)
                summary["runsSaved"] += 1
                summary["observationsSaved"] += len(observations)
            elif error:
                source_summary["errors"].append(f"{timestamp}: {error}")
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        if len(source_summary["errors"]) > 5:
            source_summary["errors"] = source_summary["errors"][:5] + ["..."]
        summary["sources"].append(source_summary)
    return summary


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    values_sorted = sorted(values)
    idx = (len(values_sorted) - 1) * pct
    low = int(idx)
    high = min(low + 1, len(values_sorted) - 1)
    fraction = idx - low
    return values_sorted[low] * (1 - fraction) + values_sorted[high] * fraction


def build_metrics(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for idx, item in enumerate(observations):
        normalized = item["normalizedPrice"]
        label_price = item.get("pricePerGpuHour")
        short = f"{item['source']} {money(label_price)}"
        short += "/GPU-hr"
        alert_score = normalized
        metrics.append(
            {
                "id": f"{item['source'].lower().replace(' ', '-')}-{idx}",
                "ok": True,
                "source": item["source"],
                "model": item.get("model", DEFAULT_GPU_MODEL),
                "market": item["market"],
                "short": short,
                "title": item["title"],
                "price": item["price"],
                "unit": item["unit"],
                "currency": item["currency"],
                "gpuCount": item["gpuCount"],
                "pricePerGpuHour": item.get("pricePerGpuHour"),
                "normalizedPrice": normalized,
                "condition": item.get("condition", ""),
                "availability": item.get("availability", ""),
                "link": item.get("link", ""),
                "observedAt": item["observedAt"],
                "latest": item["observedAt"],
                "metadata": item.get("metadata", {}),
                "severity": "low" if alert_score < 3 else "mid" if alert_score < 5 else "high",
                "alertScore": round(alert_score, 4),
            }
        )
    return metrics


def summarize(observations: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> dict[str, Any]:
    rental = [o["normalizedPrice"] for o in observations if o["market"] == "rental"]
    ok_sources = sum(1 for s in statuses if s["ok"])
    total_sources = len(statuses)
    rental_median = statistics.median(rental) if rental else None
    rental_average = statistics.mean(rental) if rental else None
    source_counts: dict[str, Any] = {
        "ok": ok_sources,
        "error": total_sources - ok_sources,
        "total": total_sources,
        "observations": len(observations),
        "byMarket": {
            "rental": len(rental),
        },
    }
    scenarios = [
        {
            "name": "Lowest rental",
            "score": min(rental) if rental else None,
            "unit": "USD/GPU-hr",
        },
        {
            "name": "Median rental",
            "score": rental_median,
            "unit": "USD/GPU-hr",
        },
        {
            "name": "Average rental",
            "score": rental_average,
            "unit": "USD/GPU-hr",
        },
    ]
    summary = {
        "generatedAt": now_utc(),
        "coverage": f"{ok_sources}/{total_sources} sources live",
        "sourceCounts": source_counts,
        "summary": {
            "rentalMin": min(rental) if rental else None,
            "rentalMedian": rental_median,
            "rentalAverage": rental_average,
            "rentalP25": percentile(rental, 0.25),
            "rentalP75": percentile(rental, 0.75),
        },
        "scenarios": scenarios,
    }
    return summary


def demo_observations() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    observed_at = now_utc()
    demo = [
        observation(
            source="Lambda",
            market="rental",
            title="NVIDIA H100 SXM 80 GB",
            price=3.99,
            unit="gpu_hour",
            condition="on-demand instance",
            availability="demo",
            link="https://lambda.ai/pricing",
            observed_at=observed_at,
            metadata={"basis": "demo"},
        ),
        observation(
            source="Crusoe",
            market="rental",
            title="NVIDIA H100 80GB HGX",
            price=3.90,
            unit="gpu_hour",
            condition="on-demand",
            availability="demo",
            link="https://www.crusoe.ai/cloud/pricing",
            observed_at=observed_at,
            metadata={"basis": "demo"},
        ),
        observation(
            source="CoreWeave",
            market="rental",
            title="NVIDIA HGX H100",
            price=49.24,
            unit="instance_hour",
            gpu_count=8,
            condition="on-demand cluster",
            availability="demo",
            link="https://www.coreweave.com/pricing",
            observed_at=observed_at,
            metadata={"basis": "demo"},
        ),
    ]
    statuses = []
    for spec in SOURCE_SPECS:
        count = len([o for o in demo if o["source"] == spec.name])
        statuses.append(
            {
                "source": spec.name,
                "market": spec.market,
                "ok": count > 0,
                "count": count,
                "latencyMs": 0,
                "error": "" if count > 0 else "demo has no live sample",
                "requires": spec.requires if count == 0 else "",
                "link": spec.link,
            }
        )
    history = []
    base = dt.datetime.now(dt.timezone.utc)
    for days_ago in range(14, -1, -1):
        stamp = (base - dt.timedelta(days=days_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")
        drift = (14 - days_ago) * 0.035
        history.append({"observedAt": stamp, "source": "Demo median rental", "market": "rental", "title": "H100 rental basket", "normalizedPrice": 3.75 + drift, "pricePerGpuHour": 3.75 + drift, "price": 3.75 + drift, "unit": "gpu_hour", "currency": "USD", "gpuCount": 1})
    return demo, statuses, history


def payload_for(observations: list[dict[str, Any]], statuses: list[dict[str, Any]], *, run_id: str | None, demo: bool, history: list[dict[str, Any]] | None = None, model: str = DEFAULT_GPU_MODEL, model_counts: dict[str, int] | None = None) -> dict[str, Any]:
    metrics = build_metrics(observations)
    rental_extremes = sorted([m for m in metrics if m["market"] == "rental"], key=lambda x: x["normalizedPrice"])
    summary = summarize(observations, statuses)
    return {
        "app": "GPU Rental Track",
        "runId": run_id,
        "demo": demo,
        "model": model,
        "models": GPU_MODELS,
        "modelCounts": model_counts or {},
        **summary,
        "metrics": metrics,
        "extremes": rental_extremes[:10],
        "observations": observations,
        "statuses": statuses,
        "history": history if history is not None else load_history(model=model),
        "config": public_config(),
    }


def public_config() -> dict[str, Any]:
    env_presence = {
        "VAST_API_KEY": bool(vast_api_token()),
        "AWS_ACCESS_KEY_ID": bool(os.getenv("AWS_ACCESS_KEY_ID")),
        "AWS_SECRET_ACCESS_KEY": bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
        "AWS_SESSION_TOKEN": bool(os.getenv("AWS_SESSION_TOKEN")),
    }
    return {
        "query": H100_QUERY,
        "models": GPU_MODELS,
        "dbPath": str(DB_PATH),
        "sources": [
            {"name": spec.name, "market": spec.market, "link": spec.link, "requires": spec.requires}
            for spec in SOURCE_SPECS
        ],
        "env": env_presence,
        "exports": {
            "csvPath": str(ALL_CSV_PATH),
            "xlsxPath": str(ALL_XLSX_PATH),
            "byModel": {
                model: {
                    "csvPath": str(model_csv_path(model)),
                    "xlsxPath": str(model_xlsx_path(model)),
                }
                for model in GPU_MODELS
            },
        },
        "tracker": tracker_snapshot(),
    }


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "H100PriceDesk/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            sys.stderr.write("[%s] %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))
        except OSError:
            pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, separators=(",", ": ")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str, download_name: str | None = None) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        static_files = {
            "/": (STATIC_DIR / "index.html", "text/html; charset=utf-8", None),
            "/index.html": (STATIC_DIR / "index.html", "text/html; charset=utf-8", None),
            "/app.js": (STATIC_DIR / "app.js", "application/javascript; charset=utf-8", None),
            "/styles.css": (STATIC_DIR / "styles.css", "text/css; charset=utf-8", None),
        }
        if parsed.path in static_files:
            path, content_type, download_name = static_files[parsed.path]
            self.send_file(path, content_type, download_name)
            return
        if parsed.path == "/api/monitor":
            query = urllib.parse.parse_qs(parsed.query)
            demo = query.get("demo", ["0"])[0] == "1"
            refresh = query.get("refresh", ["0"])[0] == "1"
            model = normalize_model(query.get("model", [DEFAULT_GPU_MODEL])[0])
            sources_param = query.get("sources", [""])[0]
            selected = {s.strip() for s in sources_param.split(",") if s.strip()} or None
            if demo:
                observations, statuses, history = demo_observations()
                self.send_json(payload_for(observations, statuses, run_id="demo", demo=True, history=history, model=model))
                return
            if refresh:
                run_id, observations, statuses = run_collectors(selected)
            else:
                observations, statuses, run_id = load_latest_run()
                if not statuses:
                    run_id, observations, statuses = run_collectors(selected)
            model_counts: dict[str, int] = {name: 0 for name in GPU_MODELS}
            for obs in observations:
                key = obs.get("model", DEFAULT_GPU_MODEL)
                model_counts[key] = model_counts.get(key, 0) + 1
            filtered = [obs for obs in observations if obs.get("model", DEFAULT_GPU_MODEL) == model]
            self.send_json(
                payload_for(
                    filtered,
                    statuses,
                    run_id=run_id,
                    demo=False,
                    history=load_history(limit=100000, model=model),
                    model=model,
                    model_counts=model_counts,
                )
            )
            return
        if parsed.path == "/api/history":
            query = urllib.parse.parse_qs(parsed.query)
            limit = int(query.get("limit", ["500"])[0])
            model_param = query.get("model", [""])[0]
            model = normalize_model(model_param) if model_param else None
            self.send_json({"history": load_history(limit=limit, model=model)})
            return
        if parsed.path == "/api/config":
            self.send_json(public_config())
            return
        if parsed.path == "/exports/gpu_rental_prices.xlsx":
            self.send_file(
                ALL_XLSX_PATH,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                XLSX_DOWNLOAD_NAME,
            )
            return
        if parsed.path == "/exports/gpu_rental_prices.csv":
            self.send_file(ALL_CSV_PATH, "text/csv; charset=utf-8", CSV_DOWNLOAD_NAME)
            return
        for model in GPU_MODELS:
            slug = model.lower()
            if parsed.path == f"/exports/{slug}_rental_prices.xlsx":
                self.send_file(
                    model_xlsx_path(model),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    f"{slug}_rental_prices.xlsx",
                )
                return
            if parsed.path == f"/exports/{slug}_rental_prices.csv":
                self.send_file(model_csv_path(model), "text/csv; charset=utf-8", f"{slug}_rental_prices.csv")
                return
        if parsed.path == "/healthz":
            self.send_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def run_once(demo: bool = False) -> dict[str, Any]:
    if demo:
        observations, statuses, history = demo_observations()
        return payload_for(observations, statuses, run_id="demo", demo=True, history=history)
    run_id, observations, statuses = run_collectors()
    return payload_for(observations, statuses, run_id=run_id, demo=False)


def run_scheduled_collection() -> None:
    started_at = now_utc()
    update_tracker_state(lastStartedAt=started_at, lastError="")
    try:
        run_id, observations, statuses = run_collectors()
        ok_count = sum(1 for status in statuses if status["ok"])
        update_tracker_state(
            lastRunId=run_id,
            lastFinishedAt=now_utc(),
            lastError="" if observations else f"collected 0 observations from {ok_count} live sources",
        )
        print(f"[tracker] saved {len(observations)} observations from {ok_count}/{len(statuses)} sources")
    except Exception as exc:
        update_tracker_state(lastFinishedAt=now_utc(), lastError=str(exc))
        print(f"[tracker] collection failed: {exc}", file=sys.stderr)
        if os.getenv("H100_DEBUG") == "1":
            traceback.print_exc()


def daily_tracker_loop(stop_event: threading.Event, daily_at: str, run_now: bool = True) -> None:
    update_tracker_state(enabled=True, mode="daily", runAt=daily_at, intervalHours=None, lastError="")
    if run_now:
        run_scheduled_collection()
    while not stop_event.is_set():
        next_run = next_daily_run_at(daily_at)
        update_tracker_state(nextRunAt=iso_utc(next_run))
        wait_seconds = max(1, int((next_run - dt.datetime.now().astimezone()).total_seconds()))
        if stop_event.wait(wait_seconds):
            break
        run_scheduled_collection()
    update_tracker_state(enabled=False, mode="off", nextRunAt=None)


def start_daily_tracker(daily_at: str, run_now: bool = True) -> threading.Event:
    parse_daily_at(daily_at)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=daily_tracker_loop,
        args=(stop_event, daily_at, run_now),
        name="h100-daily-tracker",
        daemon=True,
    )
    thread.start()
    return stop_event


def interval_tracker_loop(stop_event: threading.Event, interval_hours: float, run_now: bool = True) -> None:
    interval = parse_interval_hours(interval_hours)
    update_tracker_state(enabled=True, mode="interval", runAt=None, intervalHours=interval, lastError="")
    if run_now:
        run_scheduled_collection()
    while not stop_event.is_set():
        next_run = next_interval_run_at(interval)
        update_tracker_state(nextRunAt=iso_utc(next_run))
        wait_seconds = max(1, int((next_run - dt.datetime.now().astimezone()).total_seconds()))
        if stop_event.wait(wait_seconds):
            break
        run_scheduled_collection()
    update_tracker_state(enabled=False, mode="off", nextRunAt=None)


def start_interval_tracker(interval_hours: float, run_now: bool = True) -> threading.Event:
    interval = parse_interval_hours(interval_hours)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=interval_tracker_loop,
        args=(stop_event, interval, run_now),
        name="h100-interval-tracker",
        daemon=True,
    )
    thread.start()
    return stop_event


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor NVIDIA H100 rental prices.")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8787")))
    parser.add_argument("--once", action="store_true", help="collect once and print JSON")
    parser.add_argument("--demo", action="store_true", help="use demo data")
    parser.add_argument("--export-static", action="store_true", help="write static JSON and exports for Render Static Sites")
    parser.add_argument("--backfill-year", action="store_true", help="best-effort one-year history backfill from Wayback Machine")
    parser.add_argument("--backfill-days", type=int, default=365, help="number of days to backfill with --backfill-year")
    parser.add_argument("--backfill-sources", default="", help="comma-separated source names for Wayback backfill")
    parser.add_argument(
        "--track-daily",
        action="store_true",
        default=os.getenv("H100_TRACK_DAILY") == "1",
        help="collect once on startup, then collect every day",
    )
    parser.add_argument(
        "--track-interval",
        action="store_true",
        default=os.getenv("H100_TRACK_INTERVAL", "1") == "1",
        help="collect every N hours for all GPU models (on by default)",
    )
    parser.add_argument(
        "--track-every-hours",
        type=float,
        default=DEFAULT_TRACK_EVERY_HOURS,
        help="with --track-interval, collect every N hours; 1 means hourly (24 samples/day)",
    )
    parser.add_argument("--daily-at", default=DEFAULT_DAILY_AT, help="local time for daily collection, HH:MM")
    parser.add_argument("--no-track-now", action="store_true", help="with tracking, wait until the next scheduled time before first collection")
    parser.add_argument("--no-track", action="store_true", help="disable all automatic collection")
    args = parser.parse_args()

    init_db()
    if args.export_static:
        print(json.dumps(export_static_site_assets(), indent=2))
        return 0
    if args.backfill_year:
        selected = {s.strip() for s in args.backfill_sources.split(",") if s.strip()} or None
        print(json.dumps(backfill_wayback(days=args.backfill_days, selected=selected), indent=2))
        return 0
    if args.once:
        print(json.dumps(run_once(demo=args.demo), indent=2))
        return 0

    STATIC_DIR.mkdir(exist_ok=True)
    stop_tracker: threading.Event | None = None
    tracking_mode = "off"
    if args.no_track:
        tracking_mode = "off"
    elif args.track_daily:
        stop_tracker = start_daily_tracker(args.daily_at, run_now=not args.no_track_now)
        tracking_mode = "daily"
    elif args.track_interval:
        stop_tracker = start_interval_tracker(args.track_every_hours, run_now=not args.no_track_now)
        tracking_mode = "interval"
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"GPU Rental Track running at http://{args.host}:{args.port}")
    print(f"Tracking models: {', '.join(GPU_MODELS)}")
    if tracking_mode == "interval":
        samples_per_day = 24 / parse_interval_hours(args.track_every_hours)
        print(f"Interval tracking enabled every {args.track_every_hours:g} hours ({samples_per_day:g} samples/day).")
    elif tracking_mode == "daily":
        print(f"Daily tracking enabled at {args.daily_at} local time.")
    else:
        print("Automatic tracking disabled (--no-track).")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        if stop_tracker:
            stop_tracker.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
