#!/usr/bin/env python3
"""
Model Availability Tester for AIHub API (https://aihub.071129.xyz/)

Fetches the complete model registry and tests each model asynchronously
by sending a lightweight completion request. Verifies HTTP status, response
payload structure, and latency, then outputs a categorized summary and saves
verified models to JSON.
"""

import sys
import os
import time
import json
import asyncio
import argparse
from typing import Dict, List, Any, Optional

import httpx

# Ensure proper encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pathlib import Path
from config import BASE_URL, API_KEY, ensure_api_key

DEFAULT_OUTPUT_FILE = str(Path(__file__).resolve().parent / "available_models.json")


class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def colorize(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


async def fetch_all_models(client: httpx.AsyncClient, base_url: str, api_key: str) -> List[Dict[str, Any]]:
    """Fetch the list of registered models from the API."""
    headers = {"Authorization": f"Bearer {api_key}"}
    urls_to_try = [f"{base_url}/models", f"{base_url.replace('/v1', '')}/models"]

    for url in urls_to_try:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "data" in data:
                    return data["data"]
                elif isinstance(data, list):
                    return data
        except Exception:
            continue
    raise RuntimeError(f"Failed to fetch models list from {base_url}/models")


async def test_single_model(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_name: str,
    timeout: float = 12.0
) -> Dict[str, Any]:
    """
    Test a single model by sending a minimal chat completion request.
    Returns a result dict with success status, latency, error details, and sample response.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
        "temperature": 0.1
    }

    start_time = time.time()
    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout
        )
        latency_ms = round((time.time() - start_time) * 1000)
        status_code = resp.status_code

        if status_code == 200:
            text = resp.text.strip()
            # Catch upstream errors concealed inside 200 responses
            if "upstream returned 403" in text or "unhandled err" in text or '"upstream_error"' in text:
                return {
                    "model": model_name,
                    "available": False,
                    "status_code": 200,
                    "category": "upstream_error",
                    "reason": "Upstream service error inside 200 OK",
                    "latency_ms": latency_ms,
                    "sample": ""
                }

            try:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if content and len(content.strip()) > 0:
                        sample = content.strip().replace("\n", " ")[:60]
                        return {
                            "model": model_name,
                            "available": True,
                            "status_code": 200,
                            "category": "working",
                            "reason": "OK",
                            "latency_ms": latency_ms,
                            "sample": sample
                        }
                    else:
                        return {
                            "model": model_name,
                            "available": False,
                            "status_code": 200,
                            "category": "empty_content",
                            "reason": "Empty message content returned",
                            "latency_ms": latency_ms,
                            "sample": ""
                        }
                elif "error" in data:
                    err_msg = data["error"].get("message", "Unknown error in json")
                    return {
                        "model": model_name,
                        "available": False,
                        "status_code": 200,
                        "category": "upstream_error",
                        "reason": f"API error in payload: {err_msg[:60]}",
                        "latency_ms": latency_ms,
                        "sample": ""
                    }
                else:
                    return {
                        "model": model_name,
                        "available": False,
                        "status_code": 200,
                        "category": "no_choices",
                        "reason": "Missing choices in response payload",
                        "latency_ms": latency_ms,
                        "sample": ""
                    }
            except Exception:
                return {
                    "model": model_name,
                    "available": True,
                    "status_code": 200,
                    "category": "working_raw",
                    "reason": "OK (raw text)",
                    "latency_ms": latency_ms,
                    "sample": text[:50]
                }

        # Handle specific error status codes
        category_map = {
            400: "bad_request_or_unknown_model",
            401: "unauthorized_key",
            402: "insufficient_balance",
            403: "model_disabled",
            404: "model_not_found",
            410: "end_of_life",
            429: "rate_limited",
            500: "internal_server_error",
            502: "bad_gateway",
            503: "service_unavailable"
        }
        category = category_map.get(status_code, f"http_{status_code}")

        reason = ""
        try:
            err_data = resp.json()
            reason = err_data.get("error", {}).get("message", "")
        except Exception:
            reason = resp.text[:60]

        return {
            "model": model_name,
            "available": False,
            "status_code": status_code,
            "category": category,
            "reason": reason or f"HTTP {status_code}",
            "latency_ms": latency_ms,
            "sample": ""
        }

    except httpx.TimeoutException:
        latency_ms = round((time.time() - start_time) * 1000)
        return {
            "model": model_name,
            "available": False,
            "status_code": None,
            "category": "timeout",
            "reason": f"Request timed out ({timeout}s)",
            "latency_ms": latency_ms,
            "sample": ""
        }
    except Exception as e:
        latency_ms = round((time.time() - start_time) * 1000)
        return {
            "model": model_name,
            "available": False,
            "status_code": None,
            "category": "network_exception",
            "reason": f"{type(e).__name__}: {str(e)[:60]}",
            "latency_ms": latency_ms,
            "sample": ""
        }


async def run_scanner(
    base_url: str,
    api_key: str,
    filter_keyword: Optional[str] = None,
    filter_owner: Optional[str] = None,
    concurrency: int = 12,
    timeout: float = 12.0,
    output_file: str = "available_models.json",
    single_model: Optional[str] = None,
    verbose: bool = False
):
    print(colorize("\n" + "=" * 65, Colors.CYAN))
    print(colorize("   AIHub API Model Availability & Health Checker", Colors.BOLD + Colors.CYAN))
    print(colorize("=" * 65, Colors.CYAN))
    print(f"Target Base URL : {colorize(base_url, Colors.BOLD)}")
    masked_key = api_key[:7] + "..." + api_key[-4:] if len(api_key) > 12 else "******"
    print(f"API Key         : {colorize(masked_key, Colors.DIM)}")
    print(f"Concurrency     : {concurrency} workers | Timeout: {timeout}s")
    print(colorize("-" * 65, Colors.CYAN))

    async with httpx.AsyncClient(timeout=timeout + 5.0) as client:
        # Fetch models
        try:
            models_data = await fetch_all_models(client, base_url, api_key)
        except Exception as e:
            print(colorize(f"Error fetching model registry: {e}", Colors.RED))
            return

        all_models = [m.get("id") for m in models_data if isinstance(m, dict) and "id" in m]
        owner_map = {m.get("id"): m.get("owned_by", "unknown") for m in models_data if isinstance(m, dict)}

        if single_model:
            selected_models = [single_model]
        else:
            selected_models = all_models
            if filter_keyword:
                selected_models = [m for m in selected_models if filter_keyword.lower() in m.lower()]
            if filter_owner:
                selected_models = [m for m in selected_models if filter_owner.lower() in owner_map.get(m, "").lower()]

        total_to_test = len(selected_models)
        print(f"Total models discovered : {len(all_models)}")
        if filter_keyword or filter_owner or single_model:
            print(f"Models matching filter  : {colorize(str(total_to_test), Colors.YELLOW)}")
        print(colorize(f"Starting test on {total_to_test} models...\n", Colors.BOLD))

        sem = asyncio.Semaphore(concurrency)

        async def worker(model: str):
            async with sem:
                return await test_single_model(client, base_url, api_key, model, timeout=timeout)

        tasks = [worker(m) for m in selected_models]
        results = []
        working_count = 0
        failed_count = 0
        category_breakdown: Dict[str, int] = {}

        for idx, fut in enumerate(asyncio.as_completed(tasks), start=1):
            res = await fut
            results.append(res)
            model_name = res["model"]
            lat = res["latency_ms"]
            owner = owner_map.get(model_name, "")
            owner_label = f"[{owner}]" if owner else ""

            cat = res["category"]
            category_breakdown[cat] = category_breakdown.get(cat, 0) + 1

            if res["available"]:
                working_count += 1
                sample_preview = res["sample"]
                print(
                    f"[{idx:>3}/{total_to_test}] "
                    f"{colorize('✅ AVAILABLE', Colors.GREEN + Colors.BOLD)} "
                    f"{colorize(model_name, Colors.BOLD):<42} "
                    f"{colorize(f'({lat:>4}ms)', Colors.CYAN)} "
                    f"{colorize(owner_label, Colors.DIM)} "
                    f"-> \"{sample_preview}\""
                )
            else:
                failed_count += 1
                if verbose or single_model or total_to_test <= 20:
                    reason = res["reason"][:45]
                    print(
                        f"[{idx:>3}/{total_to_test}] "
                        f"{colorize('❌ FAILED   ', Colors.RED)} "
                        f"{model_name:<42} "
                        f"{colorize(f'({lat:>4}ms)', Colors.DIM)} "
                        f"{colorize(owner_label, Colors.DIM)} "
                        f"-> {colorize(cat, Colors.YELLOW)}: {reason}"
                    )
                else:
                    # Print brief progress counter every 25 models
                    if idx % 25 == 0 or idx == total_to_test:
                        sys.stdout.write(f"\rProgress: {idx}/{total_to_test} models checked ({working_count} available)...")
                        sys.stdout.flush()

        if not verbose and total_to_test > 20:
            print()  # newline after progress counter

        # Sort working models by latency
        working_models = [r for r in results if r["available"]]
        working_models.sort(key=lambda x: x["latency_ms"])

        # Display Final Summary
        print(colorize("\n" + "=" * 65, Colors.CYAN))
        print(colorize("                    SCAN SUMMARY RESULTS", Colors.BOLD + Colors.CYAN))
        print(colorize("=" * 65, Colors.CYAN))
        print(f"Total Models Tested : {total_to_test}")
        print(f"Available & Working : {colorize(str(working_count), Colors.GREEN + Colors.BOLD)} ({round(working_count/total_to_test*100, 1) if total_to_test else 0}%)")
        print(f"Unavailable / Failed: {colorize(str(failed_count), Colors.RED)}")

        if working_models:
            fastest = working_models[0]
            print(f"Fastest Responder   : {colorize(fastest['model'], Colors.GREEN)} ({fastest['latency_ms']}ms)")

        print(colorize("\n--- Failure Reasons Breakdown ---", Colors.BOLD))
        for cat, count in sorted(category_breakdown.items(), key=lambda x: -x[1]):
            if cat in ("working", "working_raw"):
                continue
            cat_color = Colors.YELLOW if "rate" in cat or "balance" in cat else Colors.RED
            print(f"  • {colorize(f'{cat:<30}', cat_color)} : {count}")

        if working_models:
            print(colorize(f"\n--- Verified Working Models ({len(working_models)}) ---", Colors.BOLD + Colors.GREEN))
            for i, r in enumerate(working_models, 1):
                owner = owner_map.get(r['model'], '')
                owner_str = f"({owner})" if owner else ""
                lat_val = r["latency_ms"]
                print(f"  {i:>2}. {colorize(r['model'], Colors.BOLD):<40} {colorize(f'{lat_val:>5}ms', Colors.CYAN)} {colorize(owner_str, Colors.DIM)}")

        # Save to output JSON
        save_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": base_url,
            "total_tested": total_to_test,
            "working_count": len(working_models),
            "working_models": [
                {
                    "model": r["model"],
                    "latency_ms": r["latency_ms"],
                    "owner": owner_map.get(r["model"], ""),
                    "sample": r["sample"]
                }
                for r in working_models
            ],
            "all_results": results
        }

        try:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=2, ensure_ascii=False)
            print(colorize(f"\n[+] Saved detailed results to: {output_file}", Colors.GREEN))
        except Exception as e:
            print(colorize(f"\n[-] Failed to save results to {output_file}: {e}", Colors.RED))


def main():
    parser = argparse.ArgumentParser(
        description="Test model availability and latency on AIHub API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan all models and save working models to available_models.json:
  python test_models.py

  # Test only models containing 'mistral':
  python test_models.py --filter mistral

  # Test only models containing 'free':
  python test_models.py --filter free

  # Test a single specific model:
  python test_models.py -m ministral-8b-latest

  # Show verbose output for all models (including failed ones):
  python test_models.py -v --filter gpt
"""
    )
    parser.add_argument("--base-url", default=BASE_URL, help=f"API Base URL (default: {BASE_URL})")
    parser.add_argument("--api-key", default=None, help="API Key (default: loaded from .env file)")
    parser.add_argument("-f", "--filter", help="Filter models by keyword (case-insensitive substring)")
    parser.add_argument("-o", "--owner", help="Filter models by owner/provider (e.g., mistral, openai, siliconflow)")
    parser.add_argument("-m", "--model", help="Test a single specific model ID")
    parser.add_argument("-c", "--concurrency", type=int, default=12, help="Number of concurrent test requests (default: 12)")
    parser.add_argument("-t", "--timeout", type=float, default=12.0, help="Per-model request timeout in seconds (default: 12.0)")
    parser.add_argument("-s", "--output", default=DEFAULT_OUTPUT_FILE, help=f"Output JSON file for results (default: {DEFAULT_OUTPUT_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print failure details for every model")

    args = parser.parse_args()
    active_key = ensure_api_key(args.api_key)

    asyncio.run(
        run_scanner(
            base_url=args.base_url.rstrip("/"),
            api_key=active_key,
            filter_keyword=args.filter,
            filter_owner=args.owner,
            concurrency=args.concurrency,
            timeout=args.timeout,
            output_file=args.output,
            single_model=args.model,
            verbose=args.verbose
        )
    )


if __name__ == "__main__":
    main()
