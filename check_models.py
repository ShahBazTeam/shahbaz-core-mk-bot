import requests

r = requests.get("https://api.freemodel.dev/v1/models",
    headers={"Authorization": "Bearer fe_oa_ced0a787fca501e5d943f95e30ff8276abeb48e4452ff24f"})
if r.status_code == 200:
    models = r.json()
    data = models.get("data", [])
    if not data:
        data = models.get("models", [])
    for m in data:
        print(m["id"] if isinstance(m, dict) else m)
else:
    print(f"Error {r.status_code}: {r.text[:300]}")
