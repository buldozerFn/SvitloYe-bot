import asyncio
import json
from bot import parse_group_schedule, DATA_URL, KYIV_TZ
import requests

async def test():
    print(f"Targeting URL: {DATA_URL}")
    r = requests.get(DATA_URL)
    raw = r.json()
    
    # Manually check what's in fact -> data
    f_data = raw.get("fact", {}).get("data", {})
    keys = list(f_data.keys())
    print(f"Timestamps found: {keys}")
    
    for k in keys:
        import datetime
        dt = datetime.datetime.fromtimestamp(int(k), tz=KYIV_TZ)
        print(f"Timestamp {k} -> Date: {dt.strftime('%Y-%m-%d')} (tz={KYIV_TZ})")

    res = parse_group_schedule(raw)
    print(f"\nResulting schedule keys: {list(res.keys())}")
    
    from datetime import datetime
    today = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    print(f"Today (Kyiv): {today}")
    
    if today in res:
        print("✅ Today's schedule IS available in the cache!")
    else:
        print("❌ Today's schedule IS NOT in the cache.")

if __name__ == "__main__":
    asyncio.run(test())
