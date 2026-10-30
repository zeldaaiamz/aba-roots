-- 003：去掉人工采用/排除；增加「已发送给下游」状态。
alter table public.aba_root_words add column if not exists sent_at timestamptz;
alter table public.aba_root_words add column if not exists sent_to text;
create index if not exists aba_root_words_unsent_idx on public.aba_root_words(best_rank, id) where sent_at is null;

-- 页面只读列表（anon）：p_filter = all | unsent | sent
create or replace function public.aba_list_roots(p_filter text default 'all', p_limit int default 100, p_offset int default 0, p_q text default null)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int,
              modifiers text[], examples text[], sent_at timestamptz, sent_to text, total bigint)
language sql stable security definer set search_path = public as $$
  with f as (
    select r.* from aba_root_words r
    where (coalesce(p_filter,'all')='all' or (p_filter='unsent' and r.sent_at is null) or (p_filter='sent' and r.sent_at is not null))
      and (coalesce(p_q,'')='' or r.root_word ilike '%'||p_q||'%'))
  select f.id, f.root_word, f.root_type, f.is_name_ip, f.best_rank, f.term_count,
    array(select m.word from aba_term_roots t join aba_term_roots m on m.search_term=t.search_term and m.kind='modifier'
          where t.word=f.root_word and t.kind='root' group by m.word order by count(*) desc, m.word limit 8),
    array(select t.search_term from aba_term_roots t where t.word=f.root_word and t.kind='root' order by t.search_rank nulls last limit 5),
    f.sent_at, f.sent_to, count(*) over()
  from f order by f.best_rank nulls last, f.term_count desc, f.id
  limit least(greatest(coalesce(p_limit,100),1),500) offset greatest(coalesce(p_offset,0),0);
$$;

-- 下游领取：原子地取下一批未发送并标记为已发送（仅 service_role）
create or replace function public.aba_claim_batch(p_limit int default 100, p_consumer text default null)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int, modifiers text[], sent_at timestamptz)
language sql volatile security definer set search_path = public as $$
  with c as (
    select r.id from aba_root_words r where r.sent_at is null
    order by r.best_rank nulls last, r.term_count desc, r.id
    limit least(greatest(coalesce(p_limit,100),1),1000) for update skip locked),
  u as (update aba_root_words r set sent_at = now(), sent_to = p_consumer from c where r.id = c.id returning r.*)
  select u.id, u.root_word, u.root_type, u.is_name_ip, u.best_rank, u.term_count,
    array(select m.word from aba_term_roots t join aba_term_roots m on m.search_term=t.search_term and m.kind='modifier'
          where t.word=u.root_word and t.kind='root' group by m.word order by count(*) desc, m.word limit 8),
    u.sent_at
  from u order by u.best_rank nulls last, u.term_count desc, u.id;
$$;

create or replace function public.aba_mark_sent(p_ids bigint[], p_consumer text default null) returns int
language sql volatile security definer set search_path = public as $$
  with u as (update aba_root_words set sent_at = now(), sent_to = p_consumer where id = any(p_ids) and sent_at is null returning 1)
  select count(*)::int from u;
$$;

drop function if exists public.aba_progress();
create function public.aba_progress() returns table(total bigint, unsent bigint, sent bigint)
language sql stable security definer set search_path = public as $$
  select count(*), count(*) filter (where sent_at is null), count(*) filter (where sent_at is not null) from aba_root_words;
$$;

revoke all on function public.aba_claim_batch(int,text), public.aba_mark_sent(bigint[],text) from public, anon, authenticated;
grant execute on function public.aba_claim_batch(int,text), public.aba_mark_sent(bigint[],text) to service_role;
revoke all on function public.aba_mark(jsonb,text) from public, anon, authenticated;
revoke all on function public.aba_list_roots(text,int,int,text), public.aba_progress() from public;
grant execute on function public.aba_list_roots(text,int,int,text), public.aba_progress() to anon, authenticated, service_role;
revoke update on public.aba_root_words from authenticated;
drop policy if exists aba_rw_mark on public.aba_root_words;
