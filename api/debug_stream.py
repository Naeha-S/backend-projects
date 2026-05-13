import httpx
import json
import traceback

def test_analyze():
    url = "http://127.0.0.1:8000/analyze"
    payload = {
        "problem": "test",
        "domain": "general"
    }
    
    print(f"Sending request to {url}...")
    try:
        with httpx.stream("POST", url, json=payload, timeout=60.0) as r:
            print(f"Status code: {r.status_code}")
            try:
                for line in r.iter_lines():
                    if line:
                        print(f"Received: {line}")
            except Exception as inner_e:
                print(f"Inner Exception while streaming: {inner_e}")
                traceback.print_exc()
    except Exception as e:
        print(f"Exception: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    test_analyze()
