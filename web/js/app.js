import { SUPABASE_URL, SUPABASE_ANON_KEY, LOCAL_PREVIEW } from "./config.js";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let sb = null, batch = [], marks = new Map();
async function rpc(fn, args) {
  if (LOCAL_PREVIEW) {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${fn}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args) });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  }
  const { data, error } = await sb.rpc(fn, args); if (error) throw error; return data;
}
async function progress() {
  const p = Object.fromEntries((await rpc("aba_progress", {})).map((x) => [x.status, x.n]));
  $("#prog").textContent = `待处理 ${p.pending || 0} · 采用 ${p.adopted || 0} · 排除 ${p.excluded || 0}`;
}
async function banner() {
  try { const ps = await rpc("aba_periods", {}); $("#example").hidden = !ps.some((x) => x.period === "EXAMPLE"); } catch (e) {}
}
async function load() {
  marks.clear();
  batch = await rpc("aba_next_batch", { p_limit: +$("#fn").value, p_type: $("#ftype").value || null, p_q: $("#fq").value.trim() || null });
  render(); progress();
}
function render() {
  $("#rows").innerHTML = batch.map((r, i) => {
    const m = marks.get(r.id) || "";
    return `<tr class="m-${m}" data-id="${r.id}"><td>${i + 1}</td><td class="root">${esc(r.root_word)}</td>
<td><span class="t t-${r.root_type}">${r.root_type}</span>${r.is_name_ip ? '<span class="t ip">人名/IP</span>' : ""}</td>
<td>${r.best_rank ?? "—"}</td><td>${r.term_count}</td>
<td class="ex">${(r.examples || []).map((e) => `<span>${esc(e)}</span>`).join("")}</td>
<td class="seg"><button class="in ${m === "adopted" ? "on" : ""}" data-m="adopted">采用</button><button class="out ${m === "excluded" ? "on" : ""}" data-m="excluded">排除</button></td></tr>`;
  }).join("") || `<tr><td colspan="7">没有待处理词根 🎉</td></tr>`;
  $("#submit").textContent = `提交本批（已标 ${marks.size}/${batch.length}）并取下一批`;
}
$("#rows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-m]"); if (!b) return;
  const id = +b.closest("tr").dataset.id; marks.get(id) === b.dataset.m ? marks.delete(id) : marks.set(id, b.dataset.m); render();
});
const fill = (m) => { batch.forEach((r) => marks.has(r.id) || marks.set(r.id, m)); render(); };
$("#allin").onclick = () => fill("adopted"); $("#allout").onclick = () => fill("excluded");
$("#reload").onclick = load; $("#ftype").onchange = load; $("#fn").onchange = load;
$("#fq").addEventListener("keydown", (e) => e.key === "Enter" && load());
$("#submit").onclick = async () => {
  if (!marks.size) return alert("本批还没有标记");
  if (marks.size < batch.length && !confirm(`还有 ${batch.length - marks.size} 条未标记，会留在待处理。继续？`)) return;
  await rpc("aba_mark", { p_items: [...marks].map(([id, status]) => ({ id, status })), p_by: $("#who").textContent || null });
  load();
};
async function boot() {
  if (!LOCAL_PREVIEW) {
    const { createClient } = await import("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm");
    sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    const { data: { session } } = await sb.auth.getSession();
    if (!session) { $("#login").hidden = false; $("#send").onclick = () => sb.auth.signInWithOtp({ email: $("#email").value, options: { emailRedirectTo: location.href } }).then(() => alert("已发送")); return; }
    $("#who").textContent = session.user.email;
  } else $("#who").textContent = "本地预览";
  banner(); load();
}
boot();
