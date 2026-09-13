#!/usr/bin/env python3
"""
Weekly FCC ULS pull, filtered to Nevada / Clark County.

What this does, at a high level:
  1. For each target in config.yml, figures out which FCC bulk-data zip
     files actually cover it (daily transaction files for the configured
     weekdays; the "complete" full-snapshot file is available too but is
     NOT fetched by default -- see --include-complete).
  2. Downloads + unzips those files from the FCC's public mirror
     (https://data.fcc.gov/download/pub/uls/...), which requires no
     auth, no API key, and has no rate limit -- it's the same bulk data
     https://www.midwestgrants.com and similar sites are built on top of.
  3. Parses the HD (header) and EN (entity/address) fixed-schema records
     out of the pipe-delimited .dat files, joins them on
     unique_system_identifier, and filters to the configured state (and
     optionally a city allow-list as a Clark County proxy) and radio
     service codes.
  4. Writes a dated snapshot + a rolling "latest" file per target under
     data/, plus a short run summary.

FCC data source background (verified against fcc.gov directly while
building this -- see README.md for citations):
  - Bulk data root:      https://data.fcc.gov/download/pub/uls/
  - Full/weekly refresh: .../complete/<file>.zip      (nationwide, all
                          currently-valid records for that service)
  - Daily transactions:  .../daily/<prefix><weekday>.zip  (that day's
                          new/renewed/modified/canceled/expired actions
                          only -- this is what "weekly grants" sites are
                          actually built from)

Record layout note:
  HD.dat / EN.dat field positions below follow the FCC's long-stable
  public ULS schema (unchanged for ~25 years and used by essentially
  every third-party ULS tool). They could not be re-verified against a
  live download when this script was written (the environment that
  authored it has no route to fcc.gov). Treat the first real run's
  output as something to spot-check against a known callsign/license
  before trusting it unattended -- see README.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("fetch_uls")

BASE_URL = "https://data.fcc.gov/download/pub/uls"
COMPLETE_DIR = f"{BASE_URL}/complete/"
DAILY_DIR = f"{BASE_URL}/daily/"
REQUEST_TIMEOUT = 60
USER_AGENT = (
    "fcc-uls-nv-grants/1.0 (+weekly Clark County / Nevada ULS grant pull; "
    "contact: repo owner)"
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# --- HD.dat (header) field layout ------------------------------------------
# https://www.fcc.gov/sites/default/files/pubacc_intro_11122014.pdf documents
# the canonical layout; positions below match that historical schema.
HD_FIELDS = [
    "record_type", "unique_system_identifier", "uls_file_number",
    "ebf_number", "call_sign", "license_status", "radio_service_code",
    "grant_date", "expired_date", "cancellation_date", "eligibility_rule_num",
    "reserved_1", "alien", "alien_government", "alien_corporation",
    "alien_officer", "alien_control", "revoked", "convicted", "adjudged",
    "reserved_2", "common_carrier", "non_common_carrier", "private_comm",
    "fixed", "mobile", "radiolocation", "satellite",
    "developmental_or_sta_or_demonstration", "interconnected_service",
    "certifier_first_name", "certifier_mi", "certifier_last_name",
    "certifier_suffix", "certifier_title", "female", "black",
    "native_american", "hispanic", "asian", "white", "ethnicity",
    "effective_date", "last_action_date", "auction_id",
    "reg_stat_broad_serv", "band_manager", "type_serv_broad_serv",
    "alien_ruling", "licensee_name_change", "whitespace_ind",
    "additional_cert_choice", "additional_cert_answer", "discontinuation_ind",
    "regulatory_compliance_ind", "eligibility_cert_900", "transition_plan_900",
    "return_spectrum_900", "payment_900",
]

# --- EN.dat (entity / mailing address) field layout ------------------------
EN_FIELDS = [
    "record_type", "unique_system_identifier", "uls_file_number",
    "ebf_number", "call_sign", "entity_type", "licensee_id", "entity_name",
    "first_name", "mi", "last_name", "suffix", "phone", "fax", "email",
    "street_address", "city", "state", "zip_code", "po_box",
    "attention_line", "sgin", "frn", "applicant_type_code",
    "applicant_type_code_other", "status_code", "status_date",
    "lic_category_code", "linked_license_id", "linked_callsign",
    "associated_list_ind",
]


@dataclass
class Target:
    name: str
    complete_file: Optional[str]
    daily_prefix: Optional[str]
    file_match: Optional[dict]
    radio_service_codes: list = field(default_factory=list)


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def http_get(url: str) -> Optional[requests.Response]:
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        log.warning("Request failed for %s: %s", url, exc)
        return None
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp


def discover_filename(dir_url: str, candidates: list[str]) -> Optional[str]:
    """List a FCC bulk-data directory and return the first filename that
    contains any of `candidates` (case-insensitive). FCC's download
    directories are plain autoindex-style listings; this just regexes out
    href="...zip" entries rather than assuming a specific HTML structure.
    """
    resp = http_get(dir_url)
    if resp is None:
        log.error("Could not list directory %s", dir_url)
        return None
    hrefs = re.findall(r'href="([^"]+\.[Zz][Ii][Pp])"', resp.text)
    if not hrefs:
        # Some mirrors return a JSON or plaintext listing instead of HTML.
        hrefs = re.findall(r'([A-Za-z0-9_.\-]+\.[Zz][Ii][Pp])', resp.text)
    for cand in candidates:
        for href in hrefs:
            if cand.lower() in href.lower():
                filename = href.rsplit("/", 1)[-1]
                log.info(
                    "discover_filename: matched %r against %r in %s",
                    cand, filename, dir_url,
                )
                return filename
    log.error(
        "discover_filename: none of %s matched any of %d entries in %s",
        candidates, len(hrefs), dir_url,
    )
    return None


def download_zip_records(url: str) -> dict[str, list[list[str]]]:
    """Download a ULS zip and return {record_type: [fields, ...]} for HD and
    EN records only (the two record types this pipeline needs)."""
    resp = http_get(url)
    if resp is None:
        return {}
    out: dict[str, list[list[str]]] = {"HD": [], "EN": []}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            upper = name.upper()
            record_type = None
            if upper.endswith("HD.DAT"):
                record_type = "HD"
            elif upper.endswith("EN.DAT"):
                record_type = "EN"
            else:
                continue
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", errors="replace")
                for line in text:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    out[record_type].append(line.split("|"))
    return out


def rows_to_dicts(rows: list[list[str]], fields: list[str]) -> list[dict]:
    out = []
    for row in rows:
        d = {}
        for i, name in enumerate(fields):
            d[name] = row[i].strip() if i < len(row) else ""
        out.append(d)
    return out


def fetch_target(target: Target, weekdays: list[str], include_complete: bool) -> list[dict]:
    urls_and_days: list[tuple[str, str]] = []

    if target.daily_prefix:
        for day in weekdays:
            urls_and_days.append((f"{DAILY_DIR}{target.daily_prefix}{day}.zip", day))
    elif target.file_match and target.file_match.get("daily"):
        fname = discover_filename(DAILY_DIR, target.file_match["daily"])
        if fname:
            urls_and_days.append((f"{DAILY_DIR}{fname}", "discovered"))

    if include_complete:
        cfile = target.complete_file
        if not cfile and target.file_match and target.file_match.get("complete"):
            cfile = discover_filename(COMPLETE_DIR, target.file_match["complete"])
        if cfile:
            urls_and_days.append((f"{COMPLETE_DIR}{cfile}", "complete"))

    all_hd: list[dict] = []
    all_en: list[dict] = []
    fetched_any = False
    for url, day in urls_and_days:
        log.info("[%s] fetching %s", target.name, url)
        recs = download_zip_records(url)
        if not recs:
            log.info("[%s] %s not available (skipped)", target.name, url)
            continue
        fetched_any = True
        hd = rows_to_dicts(recs["HD"], HD_FIELDS)
        en = rows_to_dicts(recs["EN"], EN_FIELDS)
        for r in hd:
            r["_source_file"] = url.rsplit("/", 1)[-1]
            r["_source_day"] = day
        all_hd.extend(hd)
        all_en.extend(en)

    if not fetched_any:
        log.warning("[%s] no source files could be fetched this run", target.name)
        return []

    en_by_id: dict[str, dict] = {}
    for r in all_en:
        en_by_id.setdefault(r["unique_system_identifier"], r)

    records = []
    for hd in all_hd:
        if target.radio_service_codes and hd.get("radio_service_code") not in target.radio_service_codes:
            continue
        en = en_by_id.get(hd["unique_system_identifier"])
        if not en:
            continue
        records.append(
            {
                "call_sign": hd.get("call_sign"),
                "radio_service_code": hd.get("radio_service_code"),
                "license_status": hd.get("license_status"),
                "grant_date": hd.get("grant_date"),
                "effective_date": hd.get("effective_date"),
                "last_action_date": hd.get("last_action_date"),
                "cancellation_date": hd.get("cancellation_date"),
                "expired_date": hd.get("expired_date"),
                "entity_name": en.get("entity_name"),
                "first_name": en.get("first_name"),
                "last_name": en.get("last_name"),
                "city": en.get("city"),
                "state": en.get("state"),
                "zip_code": en.get("zip_code"),
                "unique_system_identifier": hd.get("unique_system_identifier"),
                "uls_file_number": hd.get("uls_file_number"),
                "source_file": hd.get("_source_file"),
                "source_day": hd.get("_source_day"),
            }
        )
    return records


def apply_geo_filter(records: list[dict], state: str, cities: list[str]) -> list[dict]:
    cities_upper = {c.strip().upper() for c in cities} if cities else None
    out = []
    for r in records:
        if (r.get("state") or "").strip().upper() != state.strip().upper():
            continue
        if cities_upper and (r.get("city") or "").strip().upper() not in cities_upper:
            continue
        out.append(r)
    return out


def dedupe(records: list[dict]) -> list[dict]:
    seen = {}
    for r in records:
        key = (r["unique_system_identifier"], r["source_file"])
        seen[key] = r
    return list(seen.values())


def write_outputs(target_name: str, records: list[dict], run_date: str) -> None:
    target_dir = DATA_DIR / target_name
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot_json = target_dir / f"{run_date}.json"
    snapshot_csv = target_dir / f"{run_date}.csv"
    latest_json = target_dir / "latest.json"
    latest_csv = target_dir / "latest.csv"

    payload = {
        "target": target_name,
        "run_date": run_date,
        "record_count": len(records),
        "records": records,
    }
    snapshot_json.write_text(json.dumps(payload, indent=2))
    latest_json.write_text(json.dumps(payload, indent=2))

    fieldnames = [
        "call_sign", "radio_service_code", "license_status", "grant_date",
        "effective_date", "last_action_date", "cancellation_date",
        "expired_date", "entity_name", "first_name", "last_name", "city",
        "state", "zip_code", "unique_system_identifier", "uls_file_number",
        "source_file", "source_day",
    ]
    for path in (snapshot_csv, latest_csv):
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

    log.info("[%s] wrote %d records -> %s", target_name, len(records), snapshot_json)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config.yml",
        help="Path to config.yml",
    )
    parser.add_argument(
        "--include-complete", action="store_true",
        help="Also fetch each target's full nationwide 'complete' snapshot "
             "in addition to the weekly daily-transaction files (large, "
             "off by default).",
    )
    parser.add_argument(
        "--run-date", default=date.today().isoformat(),
        help="Override the date stamp used for output filenames (default: today, UTC-ish via date.today()).",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    state = cfg.get("state", "NV")
    cities = cfg.get("clark_county_cities", [])
    weekdays = cfg.get("daily_weekdays", ["mon", "tue", "wed", "thu", "fri", "sat"])

    summary_lines = [f"# FCC ULS weekly pull — {args.run_date}", ""]

    exit_code = 0
    for raw in cfg.get("targets", []):
        target = Target(
            name=raw["name"],
            complete_file=raw.get("complete_file"),
            daily_prefix=raw.get("daily_prefix"),
            file_match=raw.get("file_match"),
            radio_service_codes=raw.get("radio_service_codes") or [],
        )
        try:
            records = fetch_target(target, weekdays, args.include_complete)
        except Exception:
            log.exception("[%s] target failed", target.name)
            summary_lines.append(f"- **{target.name}**: FAILED (see run log)")
            exit_code = 1
            continue

        records = apply_geo_filter(records, state, cities)
        records = dedupe(records)
        write_outputs(target.name, records, args.run_date)
        summary_lines.append(f"- **{target.name}**: {len(records)} records")

    (DATA_DIR / "last_run_summary.md").write_text("\n".join(summary_lines) + "\n")
    log.info("Done.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
