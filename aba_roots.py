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

SYSTEM_PROMPT = """任务：从亚马逊ABA搜索词提取名词，区分【词根 / 修饰词】，词根内部打3类标签。
1、词根：买家可单独搜索、指代一件可售卖商品。specific 具体实物（mug, cup, tumbler）；general 泛化大类容器/载体（box, container）；bundle 套装（set, kit）。
2、修饰词：材质、颜色、场景、描述词，不能单独作为商品主体（ceramic, blue, gift）。storage 绝大多数是修饰词，只有买家单独搜 storage 买整套收纳商品时才算 general 词根。
3、禁止输出语义不通的组合词，无意义词丢弃。
示例：ceramic storage box → box（general），storage（修饰词）。

输出要求（严格遵守）：
- 输入是 JSON 数组，每项 {"i":序号,"t":搜索词}。
- 只输出一个 JSON 对象，不要 markdown、不要解释：{"items":[{"i":序号,"roots":[{"word":"box","type":"general","name_ip":false}],"modifiers":["storage"]}]}
- 每个输入序号必须且只能出现一次；type 只能是 specific / general / bundle。
- word 与 modifier 一律小写、单数原形（mugs→mug）；词根可以是固定复合名词（如 "water bottle"），但必须是搜索词中真实出现的连续词。
- 品牌名（如 stanley, owala, yeti）、数字、尺寸单位、无意义词不输出。
- 人名、影视/动漫/游戏 IP（如 taylor swift, stitch, hello kitty）若作商品主题，可作为 specific 词根输出，并加 "name_ip":true。
- 多词固定名词作词根时（如 water bottle），其中心词若也能单独指代商品（bottle），两者都输出为词根。
- 不要把 for/with/of 等功能词、人群词（women, kids 作“给谁用”时）当词根；人群词归入 modifiers。"""

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
class Usage:
    def __init__(self): self.calls = self.pin = self.pout = 0
    def add(self, u): self.calls += 1; self.pin += u.get("prompt_tokens", 0); self.pout += u.get("completion_tokens", 0)
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

def singular(w):
    ws = w.split(); x = ws[-1]
    keep = ("glass","dress","grass","class","cross","boss","bus","gas","lens","series","species","news","glasses","scissors","pants","jeans","shorts","leggings","tongs","pliers")
    if x in keep or len(x) <= 3: return w
    if x.endswith("ies") and len(x) > 4: x = x[:-3] + "y"
    elif re.search(r"(ches|shes|sses|xes|zes)$", x): x = x[:-2]
    elif x.endswith("s") and not x.endswith(("ss","us","is")): x = x[:-1]
    return " ".join(ws[:-1] + [x])

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
            w = singular(w)
            if w not in [x[0] for x in roots]: roots.append((w, ty, bool(r.get("name_ip"))))
        for m in it.get("modifiers") or []:
            w = re.sub(r"^(for|with|of|in|the|a|an|and)\s+", "", _w(m))
            if not w or re.fullmatch(r"[\d\s.]+(pack|pcs|oz|count)?", w) or w in STOP: continue
            if w and _in_term(w, toks) and w not in mods and w not in [x[0] for x in roots]: mods.append(w)
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
            log.warning("网络/限流 %s，重试", code or type(e).__name__); time.sleep(3 * (attempt + 1))
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
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not 20 <= a.batch <= 50: ap.error("--batch 须在 20~50")
    if not 1 <= a.max_rank <= 80000: ap.error("--max-rank 须在 1~80000")
    items, st = read_aba(a.csv, a.max_rank)
    log.info("读取: 原始 %(raw)d 行, 超排名/无效 %(rank_out)d, 空 %(empty)d, 重复 %(dup)d, 去重后 %(unique)d", st)
    conn = None if (a.no_db or not a.dsn) else db_connect(a.dsn)
    if conn: init_schema(conn)
    items = filter_new(conn, items)
    if a.limit: items = items[:a.limit]
    nb = (len(items) + a.batch - 1) // a.batch
    log.info("待处理(增量) %d 条, %d 批, 模型 %s", len(items), nb, MODEL)
    if a.dry_run:
        est_in = sum(len(t) for t, _ in items) / 3.5 + nb * 450; log.info("dry-run: 约 %d 输入 token（粗估），不调用模型", est_in); return 0
    key = os.environ.get("ARK_API_KEY")
    if not key: log.error("缺 ARK_API_KEY"); return 2
    ranks, usage, allres, tr, tm = dict(items), Usage(), {}, 0, 0
    for b in range(nb):
        terms = [t for t, _ in items[b * a.batch:(b + 1) * a.batch]]
        res = extract_batch(terms, key, usage); allres.update(res)
        if conn: r, m = save_batch(conn, res, ranks, a.period); tr += r; tm += m
        log.info("批 %d/%d 完成；累计 tokens in=%d out=%d", b + 1, nb, usage.pin, usage.pout)
    log.info("完成: 调用 %d 次, tokens in=%d out=%d, 新词根 %d, 新修饰词 %d%s", usage.calls, usage.pin, usage.pout, tr, tm,
             f", 估算 ¥{usage.cost():.4f}" if PRICE_IN else "（未配置单价，仅报 token）")
    if a.out_json:
        json.dump({"model": MODEL, "usage": vars(usage), "results": allres}, open(a.out_json, "w"), ensure_ascii=False, indent=1)
    return 0

if __name__ == "__main__": sys.exit(main())
