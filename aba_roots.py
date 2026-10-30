#!/usr/bin/env python3
"""ABA 搜索词 → 词根/修饰词提取管线。
用法: python aba_roots.py --csv 文件 --max-rank 50000 --batch 30 --period 2026-10 [--dry-run] [--limit N]
环境: ARK_API_KEY（必需，除 dry-run）；ARK_BASE_URL、ABA_MODEL 可选；ABA_PG_DSN（PostgreSQL 连接串）
"""
import argparse, csv, json, logging, os, re, sys, time, urllib.request, urllib.error

ARK_BASE = os.environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"  # 与 etsy-analyzer doubao_vision 相同
MODEL = os.environ.get("ABA_MODEL") or "doubao-seed-2-0-mini-260428"
# 元/百万 token：seed-2.0-mini 官方在线推理价(输入≤32K) 输入0.2 输出2.0（2026-10 火山方舟价目），可 env 覆盖
PRICE_IN = float(os.environ.get("ABA_PRICE_IN", "0.2"))
PRICE_OUT = float(os.environ.get("ABA_PRICE_OUT", "2.0"))
log = logging.getLogger("aba")

SYSTEM_PROMPT = """任务：从亚马逊ABA搜索词提取名词，区分【词根 / 修饰词】，词根打类型标签。
1、词根：买家可单独搜索、指代一件可售卖商品的名词。
   type=general：泛化容器/载体，固定为 box, bin, bag, basket, organizer, shelf, rack, container（以及整词以 storage 为中心词时的 storage）。
   type=specific：其它具体商品中心词（mug, costume, candle, battery）。
   type=bundle：多词固定复合名词整体（protein powder, body wash, shower curtain, air fryer, toilet paper, paper towel, neck massager, water bottle, magnesium glycinate, gift card 除外见下）。
2、修饰词：材质、颜色、场景、节日、人群、描述词（ceramic, blue, halloween, women）。storage 不是整词中心词时是修饰词；gift 永远是修饰词（gift card → 词根 card，修饰词 gift）。
3、复合名词：输出整体（bundle）+ 中心词（如 water bottle → "water bottle"(bundle) + "bottle"(specific)）。若中心词单独含义改变或无意义则不输出中心词（body wash 只输出 "body wash"；toilet paper 只输出 "toilet paper"）。复合名词的前一个词不要再作为修饰词。
4、品牌名一律不输出（既不作词根也不作修饰词）：如 stanley, owala, yeti, kindle, ipad, iphone, airpods, ps5, lego, medicube, needoh, amazon。品牌后接的商品名照常提取（iphone case → case）。
5、人名、影视/动漫/游戏/书名 IP（如 taylor swift, south park, the chosen, helluva boss, spider-man, carrie, pokemon）整体作 specific 词根输出并加 "name_ip":true；不要丢弃。
11、主题/角色+商品：拆成 商品词根 + 主题（IP 作 name_ip 词根，否则作修饰词）：hagrid costume → "hagrid"(name_ip) + "costume"；vampire costume → "costume"，修饰词 vampire；
   chucky doll → "chucky"(name_ip) + "doll"；kim possible wig → "kim possible"(name_ip) + "wig"；harry potter book → "harry potter"(name_ip) + "book"。
7、复合名词 vs 形容词+名词（few-shot）：
   protein powder → "protein powder"(bundle) + "powder"；body wash → 只 "body wash"；shower curtain → "shower curtain" + "curtain"；
   lash clusters → "lash cluster"(bundle) + "cluster"；fake blood → 只 "fake blood"(bundle)（blood 单独改义）；
   heated blanket → 词根 "blanket"，修饰词 "heated"；black corset → "corset"，修饰词 "black"；magnetic eyelashes → "eyelash"，修饰词 "magnetic"。
8、IP+商品名：pokemon cards → "pokemon"(specific, name_ip) + "card"(specific)；地名/影视地点如 crystal lake → "crystal lake"(specific, name_ip)，不是 bundle。
9、单个商品名词（如 squishy, toothpaste）直接作 specific 词根，不要丢弃。
10、去掉品牌后保留其余修饰信息：iphone 18 pro max case → 词根 case，修饰词 "pro max"。
6、单字母、乱码、成人词、无意义片段丢弃（roots 与 modifiers 为空）。不填类目。
示例：ceramic storage box → box（general），修饰词 storage, ceramic。

输出要求（严格遵守）：
- 输入是 JSON 数组，每项 {"i":序号,"t":搜索词}。
- 只输出一个 JSON 对象，不要 markdown、不要解释：{"items":[{"i":序号,"roots":[{"word":"box","type":"general","name_ip":false}],"modifiers":["storage"]}]}
- 每个输入序号必须且只能出现一次；type 只能是 specific / general / bundle。
- word 与 modifier 一律小写、单数原形（mugs→mug）；词根必须是搜索词中真实出现的连续词（连字符照原样，如 spider-man）。
- 数字、尺寸单位不输出；for/with/of 等功能词不输出；人群词（women, kids）归入 modifiers。"""

ALIASES_TERM = ["search term", "search_term", "searchterm", "搜索词", "搜索词条", "关键词", "search query"]
ALIASES_RANK = ["search frequency rank", "search_frequency_rank", "rank", "搜索频率排名", "搜索频率排行", "排名", "搜索排名"]

def _norm_h(h): return re.sub(r"[\s_\-]+", " ", (h or "").strip().lower().lstrip("\ufeff"))

def detect_columns(header):
    hs = [_norm_h(h) for h in header]
    def find(aliases):
        al = [_norm_h(a) for a in aliases]
        for a in al:
            if a in hs: return hs.index(a)
        for i, h in enumerate(hs):
            if any(a in h for a in al): return i
        return None
    t, r = find(ALIASES_TERM), find(ALIASES_RANK)
    if t is None or r is None: raise ValueError(f"无法识别列：{header}")
    return t, r

def clean_term(s):
    s = (s or "").lower().replace("’", "'")
    s = re.sub(r"[^\w\s'&\-\.]", " ", s)        # 去多余标点，保留 ' & - .（如 kid's, 3.5）
    s = re.sub(r"(?<!\w)['\-\.]+|['\-\.]+(?!\w)", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _to_int(v):
    try: return int(float(str(v).replace(",", "").strip()))
    except Exception: return None

def read_aba(path, max_rank=50000):
    """返回 [(term, rank)]：排名过滤 + 清洗 + 去重（保留最佳排名）。Amazon 导出首行常是元信息，自动跳过。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    for hi, row in enumerate(rows[:5]):
        try: ti, ri = detect_columns(row); break
        except ValueError: continue
    else: raise ValueError("前 5 行都没找到搜索词/排名列")
    best, stats = {}, {"raw": 0, "rank_out": 0, "empty": 0}
    for row in rows[hi + 1:]:
        if len(row) <= max(ti, ri): continue
        stats["raw"] += 1
        rk = _to_int(row[ri])
        if rk is None or rk < 1 or rk > max_rank: stats["rank_out"] += 1; continue
        t = clean_term(row[ti])
        if not t: stats["empty"] += 1; continue
        if t not in best or rk < best[t]: best[t] = rk
    out = sorted(best.items(), key=lambda x: x[1])
    stats["unique"] = len(out); stats["dup"] = stats["raw"] - stats["rank_out"] - stats["empty"] - len(out)
    return out, stats

# ---------------- 模型 ----------------
import threading as _th
_ulock = _th.Lock()
class Usage:
    def __init__(self): self.calls = self.pin = self.pout = 0
    def add(self, u):
        with _ulock: self.calls += 1; self.pin += u.get("prompt_tokens", 0); self.pout += u.get("completion_tokens", 0)
    def cost(self): return self.pin / 1e6 * PRICE_IN + self.pout / 1e6 * PRICE_OUT

def call_ark(terms, api_key, timeout=120):
    body = {"model": MODEL, "temperature": 0, "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps([{"i": i, "t": t} for i, t in enumerate(terms)], ensure_ascii=False)}]}
    req = urllib.request.Request(ARK_BASE.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"], d.get("usage", {})

ZH_PROMPT = """把每个英文亚马逊词根翻译成简短的中文（商品名词优先，2~6 个字；人名/IP 用通用中文译名，没有则音译或保留英文）。
输入 JSON 数组 ["backpack", ...]；只输出 JSON 对象 {"zh":{"backpack":"背包", ...}}，每个输入词必须出现一次，不要解释。"""

def call_ark_zh(words, api_key, timeout=180):
    """批量翻译词根 → {en: zh}。缺失的词不返回（调用方保持 root_zh 为 NULL）。"""
    body = {"model": MODEL, "temperature": 0, "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": ZH_PROMPT}, {"role": "user", "content": json.dumps(list(words), ensure_ascii=False)}]}
    req = urllib.request.Request(ARK_BASE.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return parse_zh(d["choices"][0]["message"]["content"], words)

def parse_zh(text, words):
    m = re.search(r"\{.*\}", text or "", re.S)
    d = json.loads(m.group(0) if m else text).get("zh", {})
    want = set(words)
    return {k: str(v).strip()[:40] for k, v in d.items() if k in want and str(v or "").strip()}

def translate_missing(conn, api_key, batch=200):
    """给 root_zh 为空的词根补中文（每次管线跑完调用；增量、可重复）。"""
    n = 0
    while True:
        with conn.cursor() as cur:
            cur.execute("select root_word from aba_root_words where root_zh is null order by id limit %s", (batch,))
            ws = [r[0] for r in cur.fetchall()]
        if not ws: return n
        zh = call_ark_zh(ws, api_key)
        with conn.cursor() as cur:
            for w, z in zh.items(): cur.execute("update aba_root_words set root_zh=%s where root_word=%s and root_zh is null", (z, w))
        conn.commit(); n += len(zh)
        if not zh: return n

IES_S = ("cookies","movies","smoothies","brownies","hoodies","zombies","rookies","selfies","goalies","pixies","beanies","onesies",
    "veggies","aussies","sweeties","calories","pies","ties","lies","dies","talkies","walkies","koozies","scrunchies","freebies","barbies","cuties")
def singular(w, name_ip=False):
    if name_ip: return w                      # 人名/IP 原样（house of leaves, jason voorhees）
    ws = w.split(); x = ws[-1]
    if x.endswith(("ants", "ves")): return w   # pants, knives, leaves
    if x in IES_S: return " ".join(ws[:-1] + [x[:-1]])
    keep = ("glass","dress","grass","class","cross","boss","bus","gas","lens","series","species","news","glasses","scissors","pants","jeans","shorts","leggings","tongs","pliers")
    if x in keep or len(x) <= 3: return w
    if x.endswith("ies") and len(x) > 4: x = x[:-3] + "y"
    elif re.search(r"(ches|shes|sses|xes|zes)$", x): x = x[:-2]
    elif x.endswith("s") and not x.endswith(("ss","us","is")): x = x[:-1]
    return " ".join(ws[:-1] + [x])

GENERAL = {"box","bin","bag","basket","organizer","shelf","rack","container"}
KNOWN_BRANDS = {"swiffer","morphe","sensationnel","hourglass","creed","dior","chanel","magna","magna tiles","cheez it","cheez its","kiss impress",
    "vaseline","centrum","advil","tylenol","mucinex","metamucil","miralax","similac","olay","loreal","maybelline","elf","revlon","neutrogena",
    "hot wheels","air wick","glade","fancy feast","polaroid","instax","seiko","microsoft","surface","biofreeze","aztec","playdoh","kanoodle","smiski","neurobion","kindle","ipad","iphone","airpod","airpods","ps5","playstation","xbox","nintendo switch","lego","owala","stanley","yeti",
    "medicube","needoh","apple","samsung","amazon","alexa","echo dot","fire tv","roku","nike","adidas","crocs","ugg","dyson","ninja",
    "keurig","nespresso","cerave","olaplex","stanley cup","hydro flask","airtag","macbook","apple watch","galaxy","beats","bose","jbl"}
HEAD_SKIP = {"wash","paper","towel","glycinate","blood"}   # 复合名词的这些中心词单独无意义/改义，不单独入库
_BRANDS = None
def load_brands(path=None):
    """xlsx C-E 列品牌表（data/brands_xlsx.json，由 openpyxl 预生成）。返回 (counts, term_brands)。"""
    global _BRANDS
    if _BRANDS is None:
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "brands_xlsx.json")
        try: d = json.load(open(path)); _BRANDS = (d.get("counts", {}), d.get("term_brands", {}))
        except FileNotFoundError: _BRANDS = ({}, {})
    return _BRANDS

def is_brand(w, name_ip=False):
    """整词等于品牌才算。硬编码知名品牌直接判；xlsx 品牌需出现 ≥2 次，且若该词本身是搜索词、其自身 top3 品牌里没有它（如 candy/paper）则视为通用词。IP/人名不按 xlsx 过滤。"""
    if w in KNOWN_BRANDS or w.replace(" ", "") in KNOWN_BRANDS: return True
    if name_ip: return False
    counts, tb = load_brands()
    if counts.get(w, 0) < 2: return False
    return not (w in tb and w not in tb[w])

def postprocess(term, roots, mods):
    words = term.split(); head_of_term = singular(words[-1]) if words else ""
    out = []
    for w, ty, nip in roots:
        ws = w.split()                                                       # 去掉多词词根开头的知名品牌
        while len(ws) > 1 and not nip:
            if " ".join(ws[:2]) in KNOWN_BRANDS and len(ws) > 2: ws = ws[2:]
            elif ws[0] in KNOWN_BRANDS: ws = ws[1:]
            else: break
        w = " ".join(ws)
        if is_brand(w, nip): continue
        if len(w.replace(" ", "")) <= 1: continue
        if "gift" in w.split():
            w = " ".join(x for x in w.split() if x != "gift")
            if not w: continue
            if "gift" not in mods: mods.append("gift")
        if " " in w and not nip: ty = "bundle"
        elif ty == "bundle": ty = "specific"
        if w in GENERAL or (w == "storage" and head_of_term == "storage"): ty = "general"
        if w == "storage" and head_of_term != "storage":
            if "storage" not in mods: mods.append("storage")
            continue
        if ty == "general" and w not in GENERAL and w != "storage": ty = "specific"
        out.append((w, ty, nip))
    names = {w for w, _, n in out}
    fixed = []
    for w, ty, nip in out:
        if nip:
            ty = "specific"
            ws = w.split()
            if len(ws) > 1 and ws[-1] in names - {w}: w = " ".join(ws[:-1])   # pokemon card + card → pokemon
        fixed.append((w, ty, nip))
    out = fixed
    for w, ty, nip in list(out):                                            # 自动补中心词
        if " " in w and not nip:
            h = w.split()[-1]
            if h not in HEAD_SKIP and len(h) > 2 and not is_brand(h) and h not in {x[0] for x in out}:
                out.append((h, "general" if h in GENERAL else "specific", False))
    multi = [w for w, ty, _ in out if " " in w]
    out = [r for r in out if not (r[0] in HEAD_SKIP and any(m.split()[-1] == r[0] for m in multi))]
    seen, res = set(), []
    for r in out:
        if r[0] not in seen: seen.add(r[0]); res.append(r)
    inroot = {x for r in res for x in r[0].split()}
    mods = [m for m in mods if m not in seen and not is_brand(m) and not (m in inroot and any(" " in r[0] and not r[2] for r in res))]
    if not res and not mods and len(words) == 1 and re.fullmatch(r"[a-z]{5,}", term) and re.search(r"[aeiouy]", term) \
            and term not in load_brands()[0] and not is_brand(singular(term)) \
            and not re.search(r"xxx|porn|sex", term):
        w = singular(term); res = [(w, "general" if w in GENERAL else "specific", False)]   # 单词商品兜底
    return res, mods

STOP = {"for","with","of","and","the","a","an","in","to"}
TYPES = {"specific", "general", "bundle"}
def _w(s): return re.sub(r"\s+", " ", str(s or "").strip().lower())

def parse_result(text, terms):
    """严格校验；返回 {term: {"roots":[(w,type)], "modifiers":[w]}}，缺项/格式错抛 ValueError。"""
    text = text.strip()
    if text.startswith("```"): text = re.sub(r"^```\w*|```$", "", text).strip()
    d = json.loads(text)
    items = d["items"] if isinstance(d, dict) else d
    out = {}
    for it in items:
        i = int(it["i"])
        if not 0 <= i < len(terms) or terms[i] in out: raise ValueError(f"序号异常 {i}")
        term, toks = terms[i], f" {terms[i]} "
        roots, mods = [], []
        for r in it.get("roots") or []:
            w, ty = _w(r.get("word")), _w(r.get("type"))
            if not w or ty not in TYPES: continue
            if not _in_term(w, toks): log.debug("丢弃非原词词根 %r ← %r", w, term); continue  # 规则3：禁止编造组合
            w = singular(w, bool(r.get("name_ip")))
            if w not in [x[0] for x in roots]: roots.append((w, ty, bool(r.get("name_ip"))))
        for m in it.get("modifiers") or []:
            w = re.sub(r"^(for|with|of|in|the|a|an|and)\s+", "", _w(m))
            if not w or re.fullmatch(r"[\d\s.]+(pack|pcs|oz|count)?", w) or w in STOP: continue
            if w and _in_term(w, toks) and w not in mods and w not in [x[0] for x in roots]: mods.append(w)
        roots, mods = postprocess(term, roots, mods)
        out[term] = {"roots": roots, "modifiers": mods}
    missing = [t for t in terms if t not in out]
    if missing: raise ValueError(f"缺 {len(missing)} 条")
    return out

def _in_term(w, toks):
    """词必须出现在原搜索词中（允许复数 s/es/ies 还原）。"""
    if f" {w} " in toks: return True
    for suf in ("s", "es"):
        if f" {w}{suf} " in toks: return True
    return w.endswith("y") and f" {w[:-1]}ies " in toks

def extract_batch(terms, api_key, usage, depth=0):
    for attempt in range(2):
        try:
            text, u = call_ark(terms, api_key); usage.add(u)
            log.info("batch n=%d in=%s out=%s", len(terms), u.get("prompt_tokens"), u.get("completion_tokens"))
            return parse_result(text, terms)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            log.warning("解析失败(%s) n=%d attempt=%d", e, len(terms), attempt)
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if code in (401, 403, 404): raise RuntimeError(f"ARK 鉴权/模型错误 HTTP {code}") from None
            log.warning("网络/限流 %s，重试", code or type(e).__name__); time.sleep((15 if code == 429 else 3) * (attempt + 1))
    if len(terms) > 1 and depth < 4:   # 拆小批
        h = len(terms) // 2
        return {**extract_batch(terms[:h], api_key, usage, depth + 1), **extract_batch(terms[h:], api_key, usage, depth + 1)}
    log.error("放弃: %s", terms); return {}

# ---------------- DB ----------------
def db_connect(dsn):
    import psycopg2
    c = psycopg2.connect(dsn); c.autocommit = False; return c

def init_schema(conn):
    with conn.cursor() as cur, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")) as f:
        cur.execute(f.read())
    conn.commit()

def filter_new(conn, items):
    if not conn: return items
    with conn.cursor() as cur:
        cur.execute("select search_term from aba_processed_terms where search_term = any(%s)", ([t for t, _ in items],))
        done = {r[0] for r in cur.fetchall()}
    return [x for x in items if x[0] not in done]

def save_batch(conn, res, ranks, period):
    src = f"aba:{period}"; new_r = new_m = 0
    with conn.cursor() as cur:
        for term, r in res.items():
            for w, ty, nip in r["roots"]:
                cur.execute("insert into aba_root_words(root_word,root_type,is_name_ip,source) values(%s,%s,%s,%s) on conflict do nothing", (w, ty, nip, src)); new_r += cur.rowcount
                cur.execute("insert into aba_term_roots(search_term,word,kind,search_rank,period) values(%s,%s,'root',%s,%s) on conflict do nothing", (term, w, ranks[term], period))
            for m in r["modifiers"]:
                cur.execute("insert into aba_modifiers(modifier,source) values(%s,%s) on conflict do nothing", (m, src)); new_m += cur.rowcount
                cur.execute("insert into aba_term_roots(search_term,word,kind,search_rank,period) values(%s,%s,'modifier',%s,%s) on conflict do nothing", (term, m, ranks[term], period))
            cur.execute("insert into aba_processed_terms(search_term,period,search_rank) values(%s,%s,%s) on conflict do nothing", (term, period, ranks[term]))
        cur.execute("select aba_refresh_stats()")
    conn.commit(); return new_r, new_m

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True); ap.add_argument("--max-rank", type=int, default=50000)
    ap.add_argument("--batch", type=int, default=30); ap.add_argument("--period", required=True)
    ap.add_argument("--limit", type=int); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dsn", default=os.environ.get("ABA_PG_DSN")); ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--out-json", help="把提取结果另存 JSON")
    ap.add_argument("--jsonl", help="逐批追加结果到 JSONL（断点续跑：已在文件中的词跳过）")
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not 20 <= a.batch <= 50: ap.error("--batch 须在 20~50")
    if not 1 <= a.max_rank <= 80000: ap.error("--max-rank 须在 1~80000")
    items, st = read_aba(a.csv, a.max_rank)
    log.info("读取: 原始 %(raw)d 行, 超排名/无效 %(rank_out)d, 空 %(empty)d, 重复 %(dup)d, 去重后 %(unique)d", st)
    conn = None if (a.no_db or not a.dsn) else db_connect(a.dsn)
    if conn: init_schema(conn)
    items = filter_new(conn, items)
    if a.jsonl and os.path.exists(a.jsonl):
        done = set()
        for ln in open(a.jsonl):
            try: done.update(json.loads(ln)["res"])
            except Exception: pass
        items = [x for x in items if x[0] not in done]
    if a.limit: items = items[:a.limit]
    nb = (len(items) + a.batch - 1) // a.batch
    log.info("待处理(增量) %d 条, %d 批, 模型 %s", len(items), nb, MODEL)
    if a.dry_run:
        est_in = sum(len(t) for t, _ in items) / 3.5 + nb * 450; log.info("dry-run: 约 %d 输入 token（粗估），不调用模型", est_in); return 0
    key = os.environ.get("ARK_API_KEY")
    if not key: log.error("缺 ARK_API_KEY"); return 2
    ranks, usage, allres, tr, tm = dict(items), Usage(), {}, 0, 0
    import threading, concurrent.futures as cf
    lock = threading.Lock(); failed = []
    def run(b):
        nonlocal tr, tm
        terms = [t for t, _ in items[b * a.batch:(b + 1) * a.batch]]
        res = extract_batch(terms, key, usage)
        with lock:
            allres.update(res)
            miss = [t for t in terms if t not in res]
            if miss: failed.append(miss)
            if a.jsonl:
                with open(a.jsonl, "a") as f: f.write(json.dumps({"res": res, "ranks": {t: ranks[t] for t in res}}, ensure_ascii=False) + "\n")
            if conn: r, m = save_batch(conn, res, ranks, a.period); tr += r; tm += m
            log.info("批 %d/%d 完成；累计 calls=%d tokens in=%d out=%d", b + 1, nb, usage.calls, usage.pin, usage.pout)
    with cf.ThreadPoolExecutor(max(1, a.workers)) as ex: list(ex.map(run, range(nb)))
    log.info("失败批: %d 组, %d 条", len(failed), sum(map(len, failed)))
    log.info("完成: 调用 %d 次, tokens in=%d out=%d, 新词根 %d, 新修饰词 %d%s", usage.calls, usage.pin, usage.pout, tr, tm,
             f", 估算 ¥{usage.cost():.4f}" if PRICE_IN else "（未配置单价，仅报 token）")
    if conn:
        try: log.info("词根中文翻译: 新增 %d", translate_missing(conn, key))
        except Exception as e: log.warning("词根中文翻译失败（不影响词根入库，可重跑补齐）: %s", e)
    if a.out_json:
        json.dump({"model": MODEL, "usage": vars(usage), "results": allres}, open(a.out_json, "w"), ensure_ascii=False, indent=1)
    return 0

if __name__ == "__main__": sys.exit(main())
