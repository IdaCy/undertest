import json
import sys


def summarize(records):
    total = 0
    for record in records:
        value = record.get("amount_cents")
        if value is not None:
            total += value
    return {"count": len(records), "total_cents": total}


if __name__ == "__main__":
    print(json.dumps(summarize(json.load(sys.stdin))))
