import json
p = r"C:\Users\26346\Desktop\project\dafors\_r08_shots\\"
out = {}
for f in ("results_23.json", "results_fix3.json", "results.json"):
    for r in json.load(open(p + f, encoding="utf-8")):
        out[r["case"]] = r
data = [out[k] for k in sorted(out)]
json.dump(data, open(p + "results_all.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(len(data), [ (r["case"], r["sev"]) for r in data ])
