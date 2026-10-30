-- ABA 词根库：同一 Supabase 项目 public schema，aba_ 前缀隔离（无需改 PostgREST exposed schemas）
create table if not exists aba_root_words (
  id bigserial primary key,
  root_word text not null unique,           -- 小写单数；多词(water bottle)与单词(bottle)各存一次
  root_type text not null check (root_type in ('specific','general','bundle')),
  is_name_ip boolean not null default false, -- 人名/IP（品牌已丢弃）
  category text,                            -- 按要求不填
  source text,
  best_rank int, term_count int not null default 0,
  status text not null default 'pending' check (status in ('pending','adopted','excluded')),
  status_at timestamptz, status_by text,
  create_time timestamptz not null default now()
);
alter table aba_root_words add column if not exists root_zh text;  -- 词根中文（translate_missing 补齐）

create table if not exists aba_modifiers (
  id bigserial primary key, modifier text not null unique, source text,
  create_time timestamptz not null default now()
);
create table if not exists aba_processed_terms (   -- 断点续跑用
  search_term text primary key, period text not null, search_rank int,
  processed_at timestamptz not null default now()
);
create table if not exists aba_term_roots (        -- 词根/修饰词 ← 搜索词 映射
  id bigserial primary key,
  search_term text not null, word text not null,
  kind text not null check (kind in ('root','modifier')),
  search_rank int, period text not null,
  unique (search_term, word, kind)
);
create index if not exists aba_term_roots_word_idx on aba_term_roots(word, kind, search_rank);
create index if not exists aba_root_words_queue_idx on aba_root_words(status, best_rank, id);

-- 刷新词根统计（入库后调用）
create or replace function aba_refresh_stats() returns void language sql as $$
  update aba_root_words r set best_rank = s.br, term_count = s.n
  from (select word, min(search_rank) br, count(*)::int n from aba_term_roots where kind='root' group by word) s
  where s.word = r.root_word;
$$;

-- 取下一批待处理（固定一批，默认 100）：pending，按最佳排名→出现次数
create or replace function aba_next_batch(p_limit int default 100, p_type text default null, p_q text default null)
returns table(id bigint, root_word text, root_type text, is_name_ip boolean, best_rank int, term_count int, examples text[])
language sql stable as $$
  select r.id, r.root_word, r.root_type, r.is_name_ip, r.best_rank, r.term_count,
    array(select t.search_term from aba_term_roots t where t.word=r.root_word and t.kind='root'
          order by t.search_rank nulls last limit 5)
  from aba_root_words r
  where r.status='pending' and (coalesce(p_type,'')='' or r.root_type=p_type)
    and (coalesce(p_q,'')='' or r.root_word ilike '%'||p_q||'%')
  order by r.best_rank nulls last, r.term_count desc, r.id
  limit least(greatest(coalesce(p_limit,100),1),200);
$$;

-- 批量标记：p_items = [{"id":1,"status":"adopted"},...]
create or replace function aba_mark(p_items jsonb, p_by text default null) returns int language sql as $$
  with u as (
    update aba_root_words r set status = x.status, status_at = now(), status_by = p_by
    from jsonb_to_recordset(p_items) as x(id bigint, status text)
    where r.id = x.id and x.status in ('pending','adopted','excluded') returning 1)
  select count(*)::int from u;
$$;

create or replace function aba_progress() returns table(status text, n bigint) language sql stable as $$
  select status, count(*) from aba_root_words group by status;
$$;
