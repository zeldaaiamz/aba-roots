-- 005：词根中文翻译 root_zh；aba_claim_batch / aba_list_roots 输出追加 root_zh（末列），授权/安全属性不变。
alter table public.aba_root_words add column if not exists root_zh text;
drop function if exists public.aba_claim_batch(text,int);
drop function if exists public.aba_list_roots(text,text,int,int,text);
create function public.aba_claim_batch(p_consumer text, p_limit int default 5)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int, modifiers text[], sent_at timestamptz, root_zh text)
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
    ins.sent_at, r.root_zh
  from ins join aba_root_words r on r.id = ins.root_id
  order by r.best_rank nulls last, r.term_count desc, r.id;
end $$;

create function public.aba_list_roots(p_filter text default 'all', p_consumer text default null,
  p_limit int default 100, p_offset int default 0, p_q text default null)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int,
              modifiers text[], examples text[], radar_sent_at timestamptz, sif_sent_at timestamptz, total bigint, root_zh text)
language plpgsql stable security definer set search_path = public as $$
declare f text := coalesce(p_filter,'all');
begin
  if f <> 'all' then perform aba_check_consumer(p_consumer); end if;
  return query
  with b as (
    select r.*,
      (select s.sent_at from aba_root_sends s where s.root_id=r.id and s.consumer='selection-radar') ra,
      (select s.sent_at from aba_root_sends s where s.root_id=r.id and s.consumer='sif-root-finder') sa
    from aba_root_words r where (coalesce(p_q,'')='' or r.root_word ilike '%'||p_q||'%' or r.root_zh ilike '%'||p_q||'%')),
  x as (select b.* from b where f='all'
     or (f='unsent' and (case p_consumer when 'selection-radar' then b.ra else b.sa end) is null)
     or (f='sent'   and (case p_consumer when 'selection-radar' then b.ra else b.sa end) is not null))
  select x.id, x.root_word, x.root_type, x.is_name_ip, x.best_rank, x.term_count,
    array(select m.word from aba_term_roots t join aba_term_roots m on m.search_term=t.search_term and m.kind='modifier'
          where t.word=x.root_word and t.kind='root' group by m.word order by count(*) desc, m.word limit 8),
    array(select t.search_term from aba_term_roots t where t.word=x.root_word and t.kind='root' order by t.search_rank nulls last limit 5),
    x.ra, x.sa, count(*) over(), x.root_zh
  from x order by x.best_rank nulls last, x.term_count desc, x.id
  limit least(greatest(coalesce(p_limit,100),1),500) offset greatest(coalesce(p_offset,0),0);
end $$;

revoke all on function public.aba_claim_batch(text,int) from public, anon, authenticated;
grant execute on function public.aba_claim_batch(text,int) to service_role;
revoke all on function public.aba_list_roots(text,text,int,int,text) from public;
grant execute on function public.aba_list_roots(text,text,int,int,text) to anon, authenticated, service_role;
