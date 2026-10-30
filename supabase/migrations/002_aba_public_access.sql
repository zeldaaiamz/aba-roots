-- 公开访问（无需登录）：仅通过 RPC 访问，表仍开 RLS，anon 无直接表权限。
-- RPC 改为 security definer（固定 search_path），这样 anon 不需要表级 grant。
alter function public.aba_next_batch(int,text,text) security definer set search_path = public;
alter function public.aba_mark(jsonb,text) security definer set search_path = public;
alter function public.aba_progress() security definer set search_path = public;
alter function public.aba_periods() security definer set search_path = public;
grant execute on function public.aba_next_batch(int,text,text), public.aba_mark(jsonb,text),
  public.aba_progress(), public.aba_periods() to anon, authenticated;
-- aba_refresh_stats 保持仅服务端
revoke all on function public.aba_refresh_stats() from public, anon, authenticated;
