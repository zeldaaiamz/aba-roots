# aba-roots — ABA 搜索词 → 词根/修饰词

一次性处理亚马逊美国站 ABA 导出（约 5 万条），用火山方舟豆包批量提取「词根(specific/general/bundle) / 修饰词」，入 PostgreSQL；网站按批（默认 100 条）展示待处理词根，逐条「采用/排除」后存库再取下一批。

## 结构
- `aba_roots.py` 管线 CLI；`schema.sql` 本地 PG 表结构；`supabase/migrations/001_aba_roots.sql` Supabase 迁移（public.aba_* + RLS，**未应用**）
- `web/` 静态前端（GitHub Pages）；`serve_local.py` 本地预览服务（模拟 Supabase RPC）
- `sample/` **虚构示例** CSV 与运行结果；`tests/` pytest

## 运行
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ARK_API_KEY=...        # 与 etsy-analyzer 同一个，见 ~/.config/etsy-selection/worker.env
export ABA_PG_DSN="host=... dbname=... user=... password=..."
python aba_roots.py --csv aba.csv --max-rank 50000 --batch 30 --period 2026-10 --dry-run   # 只统计
python aba_roots.py --csv aba.csv --max-rank 50000 --batch 30 --period 2026-10 --limit 60  # 小样本
python aba_roots.py --csv aba.csv --max-rank 50000 --batch 30 --period 2026-10             # 全量，可断点续跑
```
可选 env：`ABA_MODEL`（默认 doubao-seed-2-0-mini-260428）、`ARK_BASE_URL`、`ABA_PRICE_IN/OUT`（元/百万 token，默认 0.2/2.0，seed-2.0-mini）。

## 规则落地
- 列名中英文自动识别（搜索词/Search Term，搜索频率排名/Search Frequency Rank），跳过 Amazon 导出首行元信息
- 去重保留最佳排名；小写；清理标点
- 每批 20~50 条（默认 30）；严格 JSON 校验；失败重试 1 次再对半拆批（最多 4 层）
- 后处理：词根必须是原搜索词里的连续词（防编造组合）；单复数合并为单数；修饰词去掉 for/with/of 前缀、纯数字/规格
- 品牌丢弃，人名/IP 保留并标 `is_name_ip`；多词词根和中心词都入库，各存一次；category 不填；不做长尾组合
- `aba_processed_terms` 用于断点续跑

## 本地预览
```
.venv/bin/python serve_local.py   # http://127.0.0.1:8787
```

## 上线步骤（待批准）
1. 建仓库 `zeldaaiamz/aba-roots`，推送本目录（排除 .venv、sample/run*.json）
2. Supabase（oiymgpargbesvcfxyksj）应用 `supabase/migrations/001_aba_roots.sql`（仅新建 aba_* 对象，不触碰现有表）
3. 仓库 Variables 设 SUPABASE_URL / SUPABASE_ANON_KEY；加 Pages Actions：构建时写 `web/js/config.js`（LOCAL_PREVIEW=false），发布 `web/`；Supabase Auth 的 Redirect URLs 加 Pages 地址
4. 跑管线：数据只跑一次，可在 box 或 ECS `/opt/aba-roots` 手动执行（无需 systemd/定时），DSN 用 Supabase 直连串（service 角色），密钥只放 env 文件
