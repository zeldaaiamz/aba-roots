"""本地预览：静态 web/ + 模拟 Supabase /rest/v1/rpc/<fn>（直连本地 PG）。仅用于 box 预览。"""
import json, os, http.server, psycopg2, psycopg2.extras
DSN = os.environ.get("ABA_PG_DSN", "host=/tmp port=5433 user=box dbname=aba_roots")
FNS = {"aba_list_roots": "select * from aba_list_roots(%(p_filter)s,%(p_limit)s,%(p_offset)s,%(p_q)s)",
       "aba_progress": "select * from aba_progress()", "aba_periods": "select * from aba_periods()"}
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"), **k)
    def do_POST(s):
        fn = s.path.rsplit("/", 1)[-1]
        if fn not in FNS: return s.send_error(404)
        args = json.loads(s.rfile.read(int(s.headers["Content-Length"])) or b"{}")
        args = {"p_limit": 100, "p_filter": "all", "p_offset": 0, "p_q": None, **args}
        with psycopg2.connect(DSN) as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(FNS[fn], args); out = cur.fetchall()
        b = json.dumps(out, default=str).encode()
        s.send_response(200); s.send_header("Content-Type", "application/json"); s.send_header("Content-Length", len(b)); s.end_headers(); s.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", 8787), H).serve_forever()
