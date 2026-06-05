#!/usr/bin/env python3
"""
chip_compare.py — Live cloud instance pricing and comparison via Vantage Instances MCP.

Covers AWS EC2, GCP, and Azure.  Data is always fetched live — no local cache.

Usage:
    # As a module
    from chip_compare import get_price, compare_instances, get_instance_details

    price = get_price("aws", "m7a.large", "us-east-1")
    comparison = compare_instances(["m7a.large", "m8g.large", "c7a.4xlarge"], "us-east-1")

    # CLI
    python3 chip_compare.py --csp aws --instance m7a.large --region us-east-1
    python3 chip_compare.py --compare m7a.large,m6i.large,c7a.large --region us-east-1
    python3 chip_compare.py --amd-family m7a --region us-east-1
    python3 chip_compare.py --gcp --instance n2d-standard-8 --region us-central1
    python3 chip_compare.py --azure --instance Standard_D4as_v5 --region eastus

Data source: Vantage Instances MCP — https://instances-mcp.vantage.sh
"""

import json
import subprocess
import os
import sys
import argparse
import urllib.request
import urllib.error
from typing import Optional

# ─── Vantage MCP endpoint ──────────────────────────────────────────────────────
VANTAGE_MCP_URL = os.environ.get("VANTAGE_MCP_URL", "https://instances-mcp.vantage.sh/mcp")

# ─── Known AMD instance prefixes per CSP ──────────────────────────────────────
AMD_EC2_FAMILIES = ["m7a", "m8a", "c7a", "c8a", "r7a", "r8a", "hpc7a", "p5a", "x8g", "u7i"]
AMD_GCP_PREFIXES = ["n2d", "c2d", "t2d", "m3d", "a2d"]
AMD_AZURE_PREFIXES = ["Standard_D", "Standard_E", "Standard_F", "Standard_L"]  # *as_v* suffix = AMD


# ─── MCP transport ────────────────────────────────────────────────────────────

def _mcp_call(tool_name: str, arguments: dict, req_id: int = 1) -> dict:
    """Call a Vantage MCP tool and return parsed result dict."""
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        VANTAGE_MCP_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "curl/7.88.1",  # Vantage blocks Python default UA
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Vantage MCP unreachable: {e}") from e

    # SSE format: lines starting with "data: "
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError(f"Unexpected MCP response: {raw[:200]}")


def _extract_text(result: dict) -> str:
    """Pull the markdown text out of an MCP result."""
    try:
        return result["result"]["content"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Bad MCP result shape: {result}") from e


# ─── Pricing parsers ──────────────────────────────────────────────────────────

def _parse_ec2_pricing_table(markdown: str) -> dict:
    """
    Parse the EC2 region pricing markdown table into a dict of
    {os: {on_demand, spot_avg, reserved_1y, reserved_3y}}.
    """
    results = {}
    in_table = False
    headers = []
    for line in markdown.splitlines():
        if line.startswith("|") and "OS" in line:
            headers = [h.strip() for h in line.split("|") if h.strip()]
            in_table = True
            continue
        if in_table and line.startswith("| ---"):
            continue
        if in_table and line.startswith("|"):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) < 2:
                continue
            row = dict(zip(headers, cols))
            os_name = row.get("OS", "unknown").lower()
            results[os_name] = {
                "on_demand": row.get("On Demand", "N/A"),
                "spot_avg": row.get("Spot Avg", "N/A"),
                "spot_min": row.get("Spot Min", "N/A"),
                "spot_interrupt_freq": row.get("Spot Interrupt Frequency", "N/A"),
                "reserved_1y_no_upfront": row.get("1yr No Upfront", "N/A"),
                "reserved_3y_no_upfront": row.get("3yr No Upfront", "N/A"),
                "savings_plan_1y": row.get("1yr No Upfront (Savings Plan)", "N/A"),
                "savings_plan_3y": row.get("3yr No Upfront (Savings Plan)", "N/A"),
            }
        elif in_table and not line.startswith("|"):
            break
    return results


def _parse_instance_summary(markdown: str) -> dict:
    """Parse key compute/networking fields from instance detail markdown."""
    info = {}
    field_map = {
        "vCPUs:": "vcpu",
        "Memory (GiB):": "memory_gib",
        "Physical Processor:": "processor",
        "Clock Speed (GHz):": "clock_ghz",
        "CPU Architecture:": "arch",
        "Network Performance (Gibps):": "network_gibps",
        "CoreMark iterations/Second:": "coremark",
        "ffmpeg FPS:": "ffmpeg_fps",
    }
    for line in markdown.splitlines():
        line = line.strip("- ").strip()
        for key, field in field_map.items():
            if line.startswith(key):
                info[field] = line[len(key):].strip()
    # Also pull the base price from the summary line
    for line in markdown.splitlines():
        if "starting at $" in line:
            try:
                price_str = line.split("starting at $")[1].split(" ")[0]
                info["base_price_per_hr"] = float(price_str)
            except (IndexError, ValueError):
                pass
        if "per hour" in line and "$" in line:
            try:
                price_str = line.split("$")[1].split(" ")[0]
                info["base_price_per_hr"] = float(price_str)
            except (IndexError, ValueError):
                pass
    return info


# ─── Public API ───────────────────────────────────────────────────────────────

def get_instance_details(csp: str, instance_type: str) -> dict:
    """
    Return hardware details for an instance.

    csp: "aws" | "gcp" | "azure" | "rds" | "elasticache"
    """
    tool_map = {
        "aws": "get-ec2-instance",
        "ec2": "get-ec2-instance",
        "gcp": "get-gcp-instance",
        "azure": "get-azure-instance",
        "rds": "get-rds-instance",
        "elasticache": "get-elasticache-instance",
        "opensearch": "get-opensearch-instance",
    }
    tool = tool_map.get(csp.lower())
    if not tool:
        raise ValueError(f"Unknown CSP '{csp}'. Choose: aws, gcp, azure, rds, elasticache")

    result = _mcp_call(tool, {"instanceType": instance_type})
    text = _extract_text(result)

    if text.startswith("Error"):
        raise RuntimeError(f"Vantage: {text}")

    details = _parse_instance_summary(text)
    details["instance_type"] = instance_type
    details["csp"] = csp
    details["raw_markdown"] = text
    return details


def get_price(csp: str, instance_type: str, region: str, os: str = "linux") -> dict:
    """
    Return live pricing for a specific instance in a region.

    Returns dict with on_demand, spot_avg, reserved_1y, reserved_3y (all as strings like "$0.11592/hr").
    """
    tool_map = {
        "aws": "get-ec2-region-pricing",
        "ec2": "get-ec2-region-pricing",
        "gcp": "get-gcp-region-pricing",
        "azure": "get-azure-region-pricing",
        "rds": "get-rds-region-pricing",
        "elasticache": "get-elasticache-region-pricing",
        "opensearch": "get-opensearch-region-pricing",
    }
    tool = tool_map.get(csp.lower())
    if not tool:
        raise ValueError(f"Unknown CSP '{csp}'. Choose: aws, gcp, azure, rds, elasticache")

    result = _mcp_call(tool, {"instanceType": instance_type, "region": region})
    text = _extract_text(result)

    if text.startswith("Error"):
        raise RuntimeError(f"Vantage: {text}")

    pricing = _parse_ec2_pricing_table(text)

    # Find best OS match
    os_key = os.lower()
    matched = pricing.get(os_key) or pricing.get("linux") or (list(pricing.values())[0] if pricing else {})

    return {
        "instance_type": instance_type,
        "csp": csp,
        "region": region,
        "os": os_key,
        "on_demand": matched.get("on_demand", "N/A"),
        "spot_avg": matched.get("spot_avg", "N/A"),
        "spot_min": matched.get("spot_min", "N/A"),
        "spot_interrupt_freq": matched.get("spot_interrupt_freq", "N/A"),
        "reserved_1y": matched.get("reserved_1y_no_upfront", "N/A"),
        "reserved_3y": matched.get("reserved_3y_no_upfront", "N/A"),
        "savings_plan_1y": matched.get("savings_plan_1y", "N/A"),
        "all_os": pricing,
        "raw_markdown": text,
    }


def compare_instances(instance_types: list[str], region: str,
                      csp: str = "aws", os: str = "linux") -> list[dict]:
    """
    Fetch pricing for multiple instances and return sorted by on-demand price.

    Each entry includes hardware details + pricing.
    """
    rows = []
    for inst in instance_types:
        try:
            details = get_instance_details(csp, inst)
            pricing = get_price(csp, inst, region, os)
            row = {
                "instance_type": inst,
                "vcpu": details.get("vcpu", "?"),
                "memory_gib": details.get("memory_gib", "?"),
                "processor": details.get("processor", "?"),
                "network_gibps": details.get("network_gibps", "?"),
                "on_demand": pricing.get("on_demand", "N/A"),
                "spot_avg": pricing.get("spot_avg", "N/A"),
                "reserved_1y": pricing.get("reserved_1y", "N/A"),
                "reserved_3y": pricing.get("reserved_3y", "N/A"),
                "coremark": details.get("coremark", "N/A"),
            }
            rows.append(row)
        except RuntimeError as e:
            rows.append({"instance_type": inst, "error": str(e)})

    # Sort by on-demand price (parse $X.XXXX/hr)
    def price_key(r):
        try:
            return float(r.get("on_demand", "$999").lstrip("$").rstrip("/hr"))
        except ValueError:
            return 999

    return sorted(rows, key=price_key)


def get_amd_family(family: str, region: str, os: str = "linux") -> list[dict]:
    """
    Get all EC2 instances in an AMD family (e.g. 'm7a', 'c7a', 'r7a')
    with pricing for the given region.
    """
    result = _mcp_call("get-ec2-instances-for-family", {"family": family})
    text = _extract_text(result)
    if text.startswith("Error"):
        raise RuntimeError(f"Vantage: {text}")

    try:
        instances_raw = json.loads(text)
    except json.JSONDecodeError:
        # May be a list of strings or markdown
        instances_raw = []
        for line in text.splitlines():
            line = line.strip("- ").strip()
            if line and not line.startswith("#"):
                instances_raw.append(line)

    instance_types = []
    for item in instances_raw:
        if isinstance(item, str):
            instance_types.append(item)
        elif isinstance(item, dict):
            instance_types.append(item.get("name") or item.get("instanceType") or str(item))

    rows = []
    for inst in instance_types:
        try:
            pricing = get_price("aws", inst, region, os)
            row = {
                "instance_type": inst,
                "on_demand": pricing.get("on_demand", "N/A"),
                "spot_avg": pricing.get("spot_avg", "N/A"),
                "reserved_1y": pricing.get("reserved_1y", "N/A"),
            }
            rows.append(row)
        except RuntimeError:
            pass

    def price_key(r):
        try:
            return float(r.get("on_demand", "$999").lstrip("$").rstrip("/hr"))
        except ValueError:
            return 999

    return sorted(rows, key=price_key)


# ─── Pretty-print helpers ─────────────────────────────────────────────────────

def print_instance_card(details: dict, pricing: dict = None):
    """Print a formatted instance summary card."""
    SEP = "─" * 60
    print(f"\n{SEP}")
    print(f"  {details['csp'].upper()}  {details['instance_type']}")
    print(SEP)
    print(f"  vCPU       : {details.get('vcpu', 'N/A')}")
    print(f"  Memory     : {details.get('memory_gib', 'N/A')} GiB")
    print(f"  Processor  : {details.get('processor', 'N/A')}")
    print(f"  Clock      : {details.get('clock_ghz', 'N/A')}")
    print(f"  Network    : {details.get('network_gibps', 'N/A')} Gibps")
    print(f"  CoreMark   : {details.get('coremark', 'N/A')}")
    if pricing:
        print()
        print(f"  Region     : {pricing.get('region', 'N/A')}")
        print(f"  On-Demand  : {pricing.get('on_demand', 'N/A')}")
        print(f"  Spot Avg   : {pricing.get('spot_avg', 'N/A')}  (interrupt: {pricing.get('spot_interrupt_freq', 'N/A')})")
        print(f"  Reserved 1y: {pricing.get('reserved_1y', 'N/A')}")
        print(f"  Reserved 3y: {pricing.get('reserved_3y', 'N/A')}")
        print(f"  SP 1yr     : {pricing.get('savings_plan_1y', 'N/A')}")
    print(SEP)
    sys.stdout.flush()


def print_comparison_table(rows: list[dict]):
    """Print a comparison table for multiple instances."""
    if not rows:
        print("No results.")
        return

    cols = ["instance_type", "vcpu", "memory_gib", "on_demand", "spot_avg", "reserved_1y", "processor"]
    widths = {c: max(len(c), max(len(str(r.get(c, "N/A"))) for r in rows)) for c in cols}

    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    sep = "  ".join("─" * widths[c] for c in cols)
    print()
    print(header)
    print(sep)
    for row in rows:
        if "error" in row:
            print(f"  {row['instance_type']}: ERROR — {row['error']}")
            continue
        line = "  ".join(str(row.get(c, "N/A")).ljust(widths[c]) for c in cols)
        print(line)
    print()
    sys.stdout.flush()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Live cloud instance pricing via Vantage Instances MCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single instance pricing
  python3 chip_compare.py --instance m7a.large --region us-east-1
  python3 chip_compare.py --gcp --instance n2d-standard-8 --region us-central1
  python3 chip_compare.py --azure --instance Standard_D4as_v5 --region eastus

  # Side-by-side comparison
  python3 chip_compare.py --compare m7a.large,m6i.large,c7a.large --region us-east-1

  # All instances in an AMD family
  python3 chip_compare.py --amd-family m7a --region us-east-1

  # JSON output for scripting
  python3 chip_compare.py --instance m7a.large --region us-east-1 --json
""")

    parser.add_argument("--instance", help="Instance type to look up")
    parser.add_argument("--region", default="us-east-1", help="Region (default: us-east-1)")
    parser.add_argument("--os", default="linux", help="OS (default: linux)")
    parser.add_argument("--compare", help="Comma-separated list of instance types to compare")
    parser.add_argument("--amd-family", metavar="FAMILY",
                        help="List all AMD instances in family (e.g. m7a, c7a, r7a)")
    parser.add_argument("--csp", default="aws", choices=["aws", "gcp", "azure", "rds", "elasticache"],
                        help="Cloud provider (default: aws)")
    parser.add_argument("--gcp", action="store_true", help="Shorthand for --csp gcp")
    parser.add_argument("--azure", action="store_true", help="Shorthand for --csp azure")
    parser.add_argument("--json", action="store_true", dest="json_out", help="Output as JSON")
    parser.add_argument("--details-only", action="store_true", help="Hardware details only, no pricing")

    args = parser.parse_args()

    if args.gcp:
        args.csp = "gcp"
    if args.azure:
        args.csp = "azure"

    # ── AMD family listing ───────────────────────────────────────────────────
    if args.amd_family:
        rows = get_amd_family(args.amd_family, args.region, args.os)
        if args.json_out:
            print(json.dumps(rows, indent=2))
        else:
            print(f"\nAMD {args.amd_family.upper()} family — {args.region} on-demand pricing\n")
            print_comparison_table(rows)
        return

    # ── Comparison table ─────────────────────────────────────────────────────
    if args.compare:
        instances = [i.strip() for i in args.compare.split(",")]
        rows = compare_instances(instances, args.region, args.csp, args.os)
        if args.json_out:
            print(json.dumps(rows, indent=2))
        else:
            print(f"\nComparison — {args.region} ({args.csp.upper()}, {args.os})")
            print_comparison_table(rows)
        return

    # ── Single instance ──────────────────────────────────────────────────────
    if args.instance:
        details = get_instance_details(args.csp, args.instance)
        pricing = None if args.details_only else get_price(args.csp, args.instance, args.region, args.os)

        if args.json_out:
            out = {"details": details}
            if pricing:
                out["pricing"] = pricing
            # Remove raw markdown from JSON to keep it clean
            out["details"].pop("raw_markdown", None)
            if pricing:
                out["pricing"].pop("raw_markdown", None)
                out["pricing"].pop("all_os", None)
            print(json.dumps(out, indent=2))
        else:
            print_instance_card(details, pricing)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
