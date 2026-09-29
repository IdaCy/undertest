import json
import sys


def summarize(records):
    return {
        "count": len(records),
        "total_cents": sum(record["amount_cents"] for record in records),
    }


if __name__ == "__main__":
    print(json.dumps(summarize(json.load(sys.stdin))))
