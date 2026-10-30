-- 004：每个下游独立跟踪「已发送」。下游白名单：selection-radar（选品雷达）、sif-root-finder（SIF词根）。
create table if not exists public.aba_root_sends (
  root_id  bigint not null references public.aba_root_words(id) on delete cascade,
  consumer text   not null check (consumer in ('selection-radar','sif-root-finder')),
  sent_at  timestamptz not null default now(),
  primary key (root_id, consumer)
);
create index if not exists aba_root_sends_consumer_idx on public.aba_root_sends(consumer, root_id);
alter table public.aba_root_sends enable row level security;
revoke all on public.aba_root_sends from public, anon, authenticated;

-- 迁移旧 sent_at/sent_to（仅白名单内的 consumer），然后删除旧列
insert into public.aba_root_sends(root_id, consumer, sent_at)
  select id, sent_to, sent_at from public.aba_root_words
  where sent_at is not null and sent_to in ('selection-radar','sif-root-finder')
on conflict do nothing;

drop function if exists public.aba_claim_batch(int,text);
drop function if exists public.aba_mark_sent(bigint[],text);
drop function if exists public.aba_list_roots(text,int,int,text);
drop function if exists public.aba_progress();
drop index if exists public.aba_root_words_unsent_idx;
alter table public.aba_root_words drop column if exists sent_at, drop column if exists sent_to;

create or replace function public.aba_check_consumer(p_consumer text) returns text
language plpgsql immutable set search_path = public as $$
begin
  if p_consumer is null or p_consumer not in ('selection-radar','sif-root-finder') then
    raise exception 'invalid consumer %, allowed: selection-radar | sif-root-finder', coalesce(p_consumer,'NULL') using errcode = '22023';
  end if;
  return p_consumer;
end $$;

-- 下游领取：按 best_rank 取该 consumer 尚未发送的下一批，写入发送记录并返回（仅 service_role）。
-- 并发安全：同一 consumer 的领取通过事务级 advisory lock 串行化；PK + on conflict 兜底防重复。
create function public.aba_claim_batch(p_consumer text, p_limit int default 5)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int, modifiers text[], sent_at timestamptz)
language plpgsql volatile security definer set search_path = public as $$
declare c text := aba_check_consumer(p_consumer);
begin
  perform pg_advisory_xact_lock(hashtext('aba_claim_batch:' || c));
  return query
  with pick as (
    select r.id from aba_root_words r
    where not exists (select 1 from aba_root_sends s where s.root_id = r.id and s.consumer = c)
    order by r.best_rank nulls last, r.term_count desc, r.id
    limit least(greatest(coalesce(p_limit,5),1),1000)),
  ins as (
    insert into aba_root_sends(root_id, consumer) select pick.id, c from pick
    on conflict do nothing returning aba_root_sends.root_id, aba_root_sends.sent_at)
  select r.id, r.root_word, r.root_type, r.is_name_ip, r.best_rank, r.term_count,
    array(select m.word from aba_term_roots t join aba_term_roots m on m.search_term=t.search_term and m.kind='modifier'
          where t.word=r.root_word and t.kind='root' group by m.word order by count(*) desc, m.word limit 8),
    ins.sent_at
  from ins join aba_root_words r on r.id = ins.root_id
  order by r.best_rank nulls last, r.term_count desc, r.id;
end $$;

create function public.aba_mark_sent(p_ids bigint[], p_consumer text) returns int
language plpgsql volatile security definer set search_path = public as $$
declare c text := aba_check_consumer(p_consumer); n int;
begin
  insert into aba_root_sends(root_id, consumer)
    select r.id, c from aba_root_words r where r.id = any(p_ids) on conflict do nothing;
  get diagnostics n = row_count; return n;
end $$;

create function public.aba_progress()
returns table(total bigint, radar_sent bigint, radar_unsent bigint, sif_sent bigint, sif_unsent bigint)
language sql stable security definer set search_path = public as $$
  with t as (select count(*) n from aba_root_words),
       s as (select count(*) filter (where consumer='selection-radar') r, count(*) filter (where consumer='sif-root-finder') f from aba_root_sends)
  select t.n, s.r, t.n - s.r, s.f, t.n - s.f from t, s;
$$;

-- 页面只读列表（anon）：p_filter = all | unsent | sent；unsent/sent 需配合 p_consumer
create function public.aba_list_roots(p_filter text default 'all', p_consumer text default null,
  p_limit int default 100, p_offset int default 0, p_q text default null)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int,
              modifiers text[], examples text[], radar_sent_at timestamptz, sif_sent_at timestamptz, total bigint)
language plpgsql stable security definer set search_path = public as $$
declare f text := coalesce(p_filter,'all');
begin
  if f <> 'all' then perform aba_check_consumer(p_consumer); end if;
  return query
  with b as (
    select r.*,
      (select s.sent_at from aba_root_sends s where s.root_id=r.id and s.consumer='selection-radar') ra,
      (select s.sent_at from aba_root_sends s where s.root_id=r.id and s.consumer='sif-root-finder') sa
    from aba_root_words r where (coalesce(p_q,'')='' or r.root_word ilike '%'||p_q||'%')),
  x as (select b.* from b where f='all'
     or (f='unsent' and (case p_consumer when 'selection-radar' then b.ra else b.sa end) is null)
     or (f='sent'   and (case p_consumer when 'selection-radar' then b.ra else b.sa end) is not null))
  select x.id, x.root_word, x.root_type, x.is_name_ip, x.best_rank, x.term_count,
    array(select m.word from aba_term_roots t join aba_term_roots m on m.search_term=t.search_term and m.kind='modifier'
          where t.word=x.root_word and t.kind='root' group by m.word order by count(*) desc, m.word limit 8),
    array(select t.search_term from aba_term_roots t where t.word=x.root_word and t.kind='root' order by t.search_rank nulls last limit 5),
    x.ra, x.sa, count(*) over()
  from x order by x.best_rank nulls last, x.term_count desc, x.id
  limit least(greatest(coalesce(p_limit,100),1),500) offset greatest(coalesce(p_offset,0),0);
end $$;

revoke all on function public.aba_check_consumer(text) from public, anon, authenticated;
revoke all on function public.aba_claim_batch(text,int), public.aba_mark_sent(bigint[],text) from public, anon, authenticated;
grant execute on function public.aba_claim_batch(text,int), public.aba_mark_sent(bigint[],text) to service_role;
revoke all on function public.aba_list_roots(text,text,int,int,text), public.aba_progress() from public;
grant execute on function public.aba_list_roots(text,text,int,int,text), public.aba_progress() to anon, authenticated, service_role;
