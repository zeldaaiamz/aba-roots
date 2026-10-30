"""生成【示例/虚构】ABA CSV（非真实亚马逊数据），用于测通流程。"""
import csv, random
random.seed(7)
mods = ["ceramic","glass","stainless steel","blue","pink","black","large","small","cute","gift","personalized","insulated","bamboo","wooden","clear","plastic","kids","christmas","travel","storage","coffee","kitchen","bathroom","desk","vintage","funny","waterproof","portable","foldable","decorative"]
roots = ["mug","cup","tumbler","water bottle","box","container","organizer","basket","bin","set","kit","jar","plate","bowl","lunch box","shelf","rack","pillow","blanket","candle","ornament","keychain","necklace","phone case","backpack","bag","storage","lamp","frame","sign"]
odd = ["asdf qwer","stanley","owala","40 oz","b0cxyz123","for women","gifts for mom","dad gifts christmas","storage","mug","the and of","!!!","ceramic","taylor swift mug","stitch tumbler","stanley tumbler 40 oz","water bottles for kids","glass jars with lids"]
terms = set()
while len(terms) < 170:
    k = random.random()
    r = random.choice(roots)
    if k < .5: t = f"{random.choice(mods)} {r}"
    elif k < .85: a, b = random.sample(mods, 2); t = f"{a} {b} {r}"
    else: t = f"{r} {random.choice(['for women','for men','with lid','set of 4','for kids','2 pack'])}"
    terms.add(t)
terms = sorted(terms) + odd
rows = []
for i, t in enumerate(random.sample(terms, len(terms))):
    rows.append([f"{i*350+1}", t.upper() if i % 9 == 0 else t + ("," if i % 7 == 0 else ""), "B0EXAMPLE01", "示例品牌"])
for i in range(15): rows.append([str(random.randint(1, 50000)), random.choice(terms) + "  ", "B0EXAMPLE02", "dup"])   # 重复
for i in range(15): rows.append([str(random.randint(50001, 120000)), f"low rank term {i}", "B0EXAMPLE03", "过滤"])
with open("sample/aba_sample_EXAMPLE.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["示例数据 EXAMPLE ONLY - 非真实 ABA 导出", "Reporting Range=[Monthly]"])
    w.writerow(["搜索频率排名", "搜索词", "点击量最高的商品 #1: ASIN", "#1 品牌"])
    w.writerows(rows)
print(len(rows))
