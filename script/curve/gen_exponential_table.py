#!/usr/bin/env python3
"""Generate, check and export the ExponentialMintCurve price table.

The curve the vault charges is a step function: supply is cut into buckets of `BucketWidth`
iAI, and every bucket is priced flat at the value the smooth formula takes at the bucket's
**upper** bound:

    price_i = ceil_to_wei( Base * exp( Exponent * (((i + 1) * BucketWidth) / Target) ^ Power ) )

for i in 0 .. N-1. `Power` shapes the curve: 1 is the pure exponential, above 1 keeps the price
near `Base` for longer and steepens it towards `Target`. Pricing at the upper bound means the table never sits below the formula
anywhere in a bucket, and rounding each price up to the wei keeps that true after quantisation.
Both roundings favour the vault.

`Top` is the supply ceiling: the curve's `maxSafeSupply()`, and so the vault's cap while the
curve is in force. It is a constructor argument, so it need not be a multiple of the bucket
width. The table is exactly long enough to cover it, `N = ceil(Top / BucketWidth)`; the last
bucket may be partially used and is priced like any other, at its own upper bound.

Encodings, all 18-decimal fixed point ("WAD"): `Base` is wei-0G per iAI, `Exponent` and `Power`
are dimensionless and scaled by 1e18, `Target`, `BucketWidth` and `Top` are wei-iAI. Prices are
wei-0G per iAI, stored as decimal strings like every other integer in the record.

`Power` lives in the record and here only. The contract's constructor records `Base`, `Exponent`
and `Target` as provenance but has no field for `Power`, so for a curve with `Power` other than 1
the on-chain parameters alone do not describe the table; the record does. A block without the key
is read as `Power` = 1, the formula every table had before the key existed, so older records keep
checking against the tables they were generated with.

The contract holds only the table and `Top`. Nothing on chain evaluates `exp`, so this script
is the single definition of how the table is derived. It is deliberately dependency-free
(standard library `decimal` at 60 significant digits) so anyone can rerun it and compare:

    python3 script/curve/gen_exponential_table.py deployments/iai-<chainId>.json
        writes CurveParams.ExponentialMintCurve into the record (parameters from the flags,
        or from the block already in the record when no flag is given)

    python3 script/curve/gen_exponential_table.py deployments/iai-<chainId>.json --check
        regenerates from the parameters in the record and fails if the stored Prices differ

    python3 script/curve/gen_exponential_table.py deployments/iai-<chainId>.json \\
        --solidity test/unit/curves/ExponentialTable.sol
        also emits the table as a Solidity library, for tests that may not read files
"""

import argparse
import json
import sys
from decimal import ROUND_CEILING, Decimal, getcontext

WAD = 10**18
BLOCK = "ExponentialMintCurve"
# Everything the table is derived from. All but `Power` are also constructor arguments.
KEYS = {
    "base": "Base",
    "exponent": "Exponent",
    "power": "Power",
    "target": "Target",
    "width": "BucketWidth",
    "top": "Top",
}
# Keys a block may omit, with the value an absent key stands for. `Power` postdates the tables
# already recorded, all of which were generated with the pure exponential.
OPTIONAL = {"Power": WAD}

# Every Decimal operation in this file, flag parsing included, runs at 60 significant digits.
getcontext().prec = 60

# The shipped constants: 586 0G at zero supply, e^4.711 = 111x at the normalising supply of
# 9,270 iAI, 25 iAI per bucket, and a supply ceiling of 9,270 iAI.
DEFAULTS = {"base": "586", "exponent": "4.711", "power": "1", "target": "9270", "width": "25", "top": "9270"}


def bucket_count(top_wei: int, width_wei: int) -> int:
    """`ceil(Top / BucketWidth)`: the smallest table that covers the ceiling."""
    return -(-top_wei // width_wei)


def price_table(
    base_wei: int, exponent_wad: int, power_wad: int, target_wei: int, width_wei: int, top_wei: int
) -> list[int]:
    """The whole derivation. Everything else in this file is plumbing."""
    base = Decimal(base_wei)
    k = Decimal(exponent_wad) / WAD
    power = Decimal(power_wad) / WAD
    target = Decimal(target_wei)
    prices = []
    for i in range(bucket_count(top_wei, width_wei)):
        x = Decimal((i + 1) * width_wei) / target
        prices.append(int((base * (k * x**power).exp()).to_integral_value(rounding=ROUND_CEILING)))
    return prices


def cost_to_top(prices: list[int], width_wei: int, top_wei: int) -> int:
    """`cost(0, top)` as the contract computes it: the exact bucket sum, one ceiling."""
    exact_sum = 0
    cursor = 0
    for i, price in enumerate(prices):
        stop = min((i + 1) * width_wei, top_wei)
        exact_sum += price * (stop - cursor)
        cursor = stop
    return -(-exact_sum // WAD)


def to_wad(text: str) -> int:
    """'586.5' -> 586500000000000000000, exactly."""
    value = Decimal(text) * WAD
    if value != value.to_integral_value():
        sys.exit(f"{text} has more than 18 decimals")
    return int(value)


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save(path: str, record: dict) -> None:
    # Forge rewrites the record with sorted keys and two-space indentation; match it so a
    # later `run.sh` pass does not reformat what this script wrote.
    with open(path, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")


def parameters(record: dict, args: argparse.Namespace) -> dict:
    """Flags win; otherwise the block already in the record; otherwise the shipped defaults."""
    block = record.get("CurveParams", {}).get(BLOCK, {})
    if "Budget" in block and "Top" not in block and args.top is None:
        # The pre-`Top` record format sized the table by a 0G budget. That key is dropped, and
        # the ceiling comes from the default unless given; say so rather than rewrite silently.
        print(f"note: {BLOCK} block carries the old Budget key and no Top; using the default Top and dropping Budget")
    out = {}
    for flag, key in KEYS.items():
        given = getattr(args, flag)
        if given is not None:
            out[key] = to_wad(given)
        elif key in block:
            out[key] = int(block[key])
        elif key in OPTIONAL and BLOCK in record.get("CurveParams", {}):
            # A block that predates the key: keep the value its table was generated with, rather
            # than the default, so changing one flag does not silently reshape the curve.
            out[key] = OPTIONAL[key]
        else:
            out[key] = to_wad(DEFAULTS[flag])
    return out


def recorded_parameters(record: dict, path: str) -> dict:
    """For --check: every parameter must come from the block itself. A default or a flag standing
    in for a missing key would let the check agree with something the record does not say."""
    block = record.get("CurveParams", {}).get(BLOCK, {})
    missing = [key for key in KEYS.values() if key not in block and key not in OPTIONAL]
    if missing:
        sys.exit(f"{path}: {BLOCK} block lacks {', '.join(missing)}; run without --check to regenerate it")
    return {key: int(block[key]) if key in block else OPTIONAL[key] for key in KEYS.values()}


def solidity_library(prices: list[int], params: dict, record_path: str) -> str:
    packed = b"".join(p.to_bytes(16, "big") for p in prices)
    chunks = [packed[i : i + 64].hex() for i in range(0, len(packed), 64)]
    body = "\n".join(f'        hex"{c}"' for c in chunks)
    return f"""// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

/**
 * @title ExponentialTable
 * @notice The production ExponentialMintCurve table, as compile-time data.
 *
 * @dev GENERATED -- do not edit. Regenerate with
 *
 *      python3 script/curve/gen_exponential_table.py {record_path} \\
 *          --solidity test/unit/curves/ExponentialTable.sol
 *
 *      Unit tests may not read files, so the table the deployment record carries is mirrored
 *      here; `test/script/Deploy.t.sol` asserts the two are identical. Each price is 16 bytes,
 *      big-endian, in bucket order.
 */
library ExponentialTable {{
    uint256 internal constant BUCKET_WIDTH = {params['BucketWidth']};
    uint256 internal constant TOP = {params['Top']};
    uint256 internal constant BASE = {params['Base']};
    uint256 internal constant EXPONENT = {params['Exponent']};
    uint256 internal constant TARGET = {params['Target']};

    bytes internal constant PACKED =
{body};

    function prices() internal pure returns (uint128[] memory p) {{
        bytes memory packed = PACKED;
        p = new uint128[](packed.length / 16);
        for (uint256 i = 0; i < p.length; i++) {{
            uint256 word;
            // solhint-disable-next-line no-inline-assembly
            assembly {{
                word := mload(add(add(packed, 32), mul(i, 16)))
            }}
            p[i] = uint128(word >> 128);
        }}
    }}
}}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record", help="deployments/iai-<chainId>.json")
    ap.add_argument("--base", help="0G per iAI at zero supply, e.g. 586")
    ap.add_argument("--exponent", help="exponent coefficient, e.g. 4.711")
    ap.add_argument("--power", help="power applied to supply/target inside the exponent, e.g. 3.274; 1 is the pure exponential")
    ap.add_argument("--target", help="supply the exponent is normalised against, in iAI, e.g. 9270")
    ap.add_argument("--width", help="bucket width in iAI, e.g. 25")
    ap.add_argument("--top", help="supply ceiling in iAI, e.g. 9270; the table is sized to cover it")
    ap.add_argument("--check", action="store_true", help="verify the stored table instead of writing it")
    ap.add_argument(
        "--require",
        action="store_true",
        help="with --check: the record must carry the block (the kind is about to be deployed)",
    )
    ap.add_argument("--solidity", metavar="PATH", help="also write the table as a Solidity library")
    args = ap.parse_args()

    record = load(args.record)
    kind_is_exponential = record.get("MintCurveKind") == BLOCK

    if args.check:
        if any(getattr(args, flag) is not None for flag in KEYS):
            sys.exit("--check verifies the record against itself; parameter flags are not accepted with it")
        if BLOCK not in record.get("CurveParams", {}):
            if args.require or kind_is_exponential:
                sys.exit(f"{args.record}: no {BLOCK} block -- run genCurve first")
            print(f"{args.record}: no {BLOCK} block, nothing to check")
            if args.solidity:
                sys.exit(f"cannot write {args.solidity}: the record carries no table to export")
            return
        params = recorded_parameters(record, args.record)
    else:
        params = parameters(record, args)
    for key, value in params.items():
        if value <= 0:
            sys.exit(f"{key} must be positive")
    # Refused before anything is written, so a rejected export leaves the record as it was.
    # The mirror's constants are the constructor's provenance arguments, which have no field for
    # `Power`; exported with any other value, they would not describe the table beside them.
    if args.solidity and params["Power"] != WAD:
        sys.exit(f"cannot write {args.solidity}: the mirror has no Power constant, and this table's Power is not 1")

    prices = price_table(
        params["Base"], params["Exponent"], params["Power"], params["Target"], params["BucketWidth"], params["Top"]
    )
    total = cost_to_top(prices, params["BucketWidth"], params["Top"])

    if args.check:
        stored = [int(p) for p in record["CurveParams"][BLOCK].get("Prices", [])]
        if stored != prices:
            first = next((i for i, (a, b) in enumerate(zip(stored, prices)) if a != b), min(len(stored), len(prices)))
            sys.exit(
                f"{args.record}: stored Prices do not match the parameters beside them "
                f"(first difference at bucket {first}; stored {len(stored)} entries, derived {len(prices)}). "
                f"Run without --check to regenerate."
            )
        print(f"{args.record}: {len(prices)} prices match their parameters (top {params['Top']}, cost to top {total} wei-0G)")
    else:
        block = {
            "Base": str(params["Base"]),
            "BucketWidth": str(params["BucketWidth"]),
            "Exponent": str(params["Exponent"]),
            "Prices": [str(p) for p in prices],
            "Target": str(params["Target"]),
            "Top": str(params["Top"]),
        }
        # Written only when it says something: a pure-exponential block stays byte-identical to
        # the ones generated before the key existed.
        if params["Power"] != WAD:
            block["Power"] = str(params["Power"])
        record.setdefault("CurveParams", {})[BLOCK] = block
        save(args.record, record)
        print(
            f"{args.record}: wrote {len(prices)} prices, width {params['BucketWidth']}, top {params['Top']}, "
            f"cost to top {total} wei-0G, first {prices[0]}, last {prices[-1]}"
        )

    if args.solidity:
        with open(args.solidity, "w") as f:
            f.write(solidity_library(prices, params, args.record))
        print(f"{args.solidity}: written")


if __name__ == "__main__":
    main()
