import { SUPABASE_URL, SUPABASE_ANON_KEY, LOCAL_PREVIEW } from "./config.js";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (t) => t ? new Date(t).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
const st = (t) => t ? `<span class="badge sent">已发送</span> <small>${fmt(t)}</small>` : '<span class="badge unsent">未发送</span>';
let sb = null, offset = 0, total = 0;
async function rpc(fn, args) {
  if (LOCAL_PREVIEW) {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${fn}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args) });
    if (!r.ok) throw new Error(await r.text()); return r.json();
  }
  const { data, error } = await sb.rpc(fn, args); if (error) throw error; return data;
}
async function progress() {
  const p = (await rpc("aba_progress", {}))[0] || {};
  $("#prog").textContent = `共 ${p.total || 0} · 选品雷达 已发送 ${p.radar_sent || 0} / 未发送 ${p.radar_unsent || 0} · SIF词根 已发送 ${p.sif_sent || 0} / 未发送 ${p.sif_unsent || 0}`;
}
async function banner() {
  try { const ps = await rpc("aba_periods", {}); $("#example").hidden = !ps.some((x) => x.period === "EXAMPLE"); } catch (e) {}
}
async function load() {
  const n = +$("#fn").value;
  const [flt, cons] = $("#ff").value.split(":");
  const rows = await rpc("aba_list_roots", { p_filter: flt, p_consumer: cons || null, p_limit: n, p_offset: offset, p_q: $("#fq").value.trim() || null });
  total = rows.length ? +rows[0].total : 0;
  $("#rows").innerHTML = rows.map((r, i) => `<tr><td>${offset + i + 1}</td><td class="root">${esc(r.root_word)}</td>
<td><span class="t t-${esc(r.root_type)}">${esc(r.root_type)}</span>${r.is_name_ip ? '<span class="t ip">人名/IP</span>' : ""}</td>
<td>${r.best_rank ?? "—"}</td><td>${r.term_count}</td>
<td class="ex">${(r.modifiers || []).map((e) => `<span>${esc(e)}</span>`).join("") || "—"}</td>
<td>${st(r.radar_sent_at)}</td><td>${st(r.sif_sent_at)}</td></tr>`).join("") || `<tr><td colspan="8">没有数据</td></tr>`;
  const pages = Math.max(1, Math.ceil(total / n));
  $("#page").textContent = ` 第 ${Math.floor(offset / n) + 1}/${pages} 页（${total} 条） `;
  $("#prev").disabled = offset === 0; $("#next").disabled = offset + n >= total;
  progress();
}
const reset = () => { offset = 0; load(); };
$("#ff").onchange = reset; $("#fn").onchange = reset;
$("#fq").addEventListener("keydown", (e) => e.key === "Enter" && reset());
$("#prev").onclick = () => { offset = Math.max(0, offset - +$("#fn").value); load(); };
$("#next").onclick = () => { offset += +$("#fn").value; load(); };
async function boot() {
  if (!LOCAL_PREVIEW) {
    const { createClient } = await import("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm");
    sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  }
  banner(); load();
}
boot();
