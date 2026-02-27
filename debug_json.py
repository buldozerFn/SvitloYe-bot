import requests
import json

url = 'https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json'
try:
    r = requests.get(url)
    data = r.json()
    
    fact_data = data.get('fact', {}).get('data', {})
    print("Top level keys in fact.data:")
    print(list(fact_data.keys()))
    
    if '1772143200' in fact_data:
        day_data = fact_data['1772143200']
        print("\nKeys inside '1772143200':")
        print(list(day_data.keys()))
        
        # Checking for Group 6.2
        found = False
        for k, v in day_data.items():
            if '6.2' in k:
                print(f"\nFound match for 6.2: key='{k}' type={type(v)}")
                if isinstance(v, dict):
                    print(f"Keys inside {k}: {list(v.keys())}")
                found = True
        
        if not found:
            print("\nGroup 6.2 NOT FOUND in 1772143200")
            # Let's see if 6.2 is inside the hours?
            first_hour = day_data.get('1')
            if isinstance(first_hour, dict):
                 print(f"\nKeys inside hour '1': {list(first_hour.keys())}")

except Exception as e:
    print(f"Error: {e}")
