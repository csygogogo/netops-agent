"""Compute a ratio from supplied counters; no device/network access."""
import argparse
import json


def ratio(total: int, errors: int):
    if total < 0 or errors < 0 or errors > total:
        raise ValueError("需要 0 <= errors <= total")
    return {"total": total, "errors": errors, "error_ratio": errors / total if total else None,
            "note": "比例基于输入计数；累计值不能直接代表当前故障率。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, required=True)
    parser.add_argument("--errors", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(ratio(args.total, args.errors), ensure_ascii=False))
