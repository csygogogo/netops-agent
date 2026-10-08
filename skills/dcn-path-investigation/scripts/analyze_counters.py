"""计算相邻采样的 CRC 增量比例；只处理输入数字，不访问设备。"""
import argparse
from datetime import datetime
import json
import math


def analyze(samples, threshold):
    """拒绝时间倒序、计数回退及无效数字，避免把累计错误数当成当前错误率。"""
    if not isinstance(samples, list) or len(samples) != 2:
        raise ValueError("samples 必须包含前后两次采样")
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold 必须为 0..1 的比例")
    before, after = samples
    seconds = (datetime.fromisoformat(after["time"]) - datetime.fromisoformat(before["time"])).total_seconds()
    if seconds <= 0:
        raise ValueError("采样时间必须递增")
    for sample in samples:
        for key in ("rx_packets", "crc_errors"):
            value = sample[key]
            if type(value) is not int or value < 0:
                raise ValueError("计数必须是非负整数")
    packets = after["rx_packets"] - before["rx_packets"]
    errors = after["crc_errors"] - before["crc_errors"]
    if packets < 0 or errors < 0 or errors > packets:
        raise ValueError("计数回退或错误包增量超过接收包增量，请重新采样")
    rate = errors / packets if packets else None
    return {"interval_seconds": seconds, "packets_delta": packets, "crc_delta": errors,
            "error_rate": rate, "error_percent": rate * 100 if rate is not None else None,
            "threshold": threshold, "assessment": "unknown" if rate is None else "degraded" if rate > threshold else "healthy"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True, help="两次采样组成的 JSON 数组")
    parser.add_argument("--threshold", type=float, required=True, help="错误率阈值，例如 0.01 表示 1%")
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.samples), args.threshold), ensure_ascii=False))
