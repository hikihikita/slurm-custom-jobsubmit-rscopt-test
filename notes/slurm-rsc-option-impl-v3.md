# `--rsc` オプション実装設計ノート v3

> 対象: Slurm 25.11.4  
> 根拠仕様: `specs/slurm-sched-spec.new.md` 項目 2-1〜2-4、8-2、8-4  
> v2.2 (`slurm-rsc-option-impl-v2.md`) からの変更点:
> - `node_gpus` + `node_sockets` → `node_gres_count` + `gres_alloc_mode` に統合
> - D分ノード実装を gres/numa het-job から use_device path（`--gpus + --cpus-per-gpu + --mem-bind=local`）に刷新
> - cli_filter の変換パスを 2 分岐から 3 分岐（use_device / full-node / core-count）に変更
> - `--rsc` マルチグループ構文（`|` 区切り）を実装（設計案 → 実装済み）
> - `--rsc` と `:` het-job 構文の組み合わせを明示禁止（`_SLURM_RSC_ACTIVE`）
> - cons_tres コアパッチ（`_cap_node_cpus_for_gres_per_job()`）を追加

---

## 1. 概要

spec 2-4 は、ジョブの CPU/GPU 資源を一括指定するための統合オプション `--rsc` を定義する。  
Slurm は `--rsc` を標準でサポートしないため、**2 つのプラグイン**と **1 つのコアパッチ**の組み合わせで実装する。

| # | コンポーネント | 役割 |
|---|---|---|
| ① | spank_rsc.so | `--rsc` CLI オプションの登録・受付・bridge env var のセット・`user_init()` で OMP_*/RSC_C_EFF をセット |
| ② | cli_filter_rsc.so | spec 解析・競合オプション検出（deny/warn）・c_eff 計算・ntasks/cpus/nodes 等を `slurm_opt_t` に設定・マルチグループ het-job append |
| ③ | cons_tres コアパッチ | `gres_per_job + cpus_per_gres` の端数ノードで CPU 過剰確保を修正 |

**v2.2 → v3 の変更経緯**:  
v2.2 の D分ノード実装は `gres/numa` GRES + het-job (`cli_filter_het_append`) で動作していたが、
`gres/numa` は Slurm 内部での扱いが複雑で運用コストが高い。v3 では `node_gres_count` / `gres_alloc_mode` により
GPU ドメイン単位の割当（use_device path）を一本化し、同時に `cons_tres` コアパッチで端数ノードの
CPU 過剰確保を Slurm スケジューラレベルで解決した。

---

## 2. `--rsc` オプション仕様まとめ

### 2-1. 書式（spec 2-4）

```
CPU 資源指定:  --rsc p={procs}:t={threads}:c={cores}:m={memory}
GPU 資源指定:  --rsc g={gpus}
マルチグループ: --rsc 'SPEC0 | SPEC1 | ...'  (最大 RSC_MAX_GROUPS=8 グループ)
```

- CPU 形式と GPU 形式は**排他**（同時指定不可）
- `|` 区切りで複数グループを指定すると het-job として投入される

### 2-2. 各パラメータの意味と制約（spec 2-1、2-2）

| パラメータ | 意味 | 制約 | 省略時デフォルト |
|---|---|---|---|
| `p` | プロセス数 | 正整数 | `1` |
| `t` | プロセスあたりスレッド数 | 正整数 | `1` |
| `c` | プロセスあたりコア数 | 正整数、c は t の約数または倍数 | `1` |
| `m` | プロセスあたりメモリ上限 (MiB) | 正整数（M/G/T サフィックス可）。省略時 `c × m_default` | `c × m_default` |
| `g` | GPU 数 | 正整数 | — |

`c_eff`（実効コア数）= `max(c, ceil(m_mib × node_cpus / node_mem_mib))`:  
メモリ制約が c より多くのコアを必要とする場合は切り上げる。

### 2-3. spec 8-4 との関係

`--rsc` は変換層であり、最終的には Slurm ネイティブのリソース指定に変換して投入される。

---

## 3. 実装アーキテクチャ

```
ユーザ: sbatch --rsc p=42:t=1:c=1 job.sh
        (node_cpus=40, node_mem=196608MiB, node_gres_count=2, gres_alloc_mode=node)

[クライアント側（ログインノード）]
  ┌─ slurm_spank_init()
  │    spank_rsc: setenv("_SLURM_RSC_CONFIG", "/etc/slurm/rsc_defaults.conf")
  │
  ├─ getopt ループ
  │    spank_rsc: _rsc_opt_cb()
  │      setenv("_SLURM_RSC_SPEC", "p=42:t=1:c=1")
  │      setenv("_SLURM_RSC_ACTIVE", "1")
  │
  ├─ cli_filter_g_pre_submit(opt, offset=0)
  │    cli_filter_rsc: pre_submit(opt)
  │      c_eff = 1 (メモリ制約なし)
  │      p * c_eff = 42 >= node_cpus=40 → use_device パス:
  │        total_gres = ceil(42*1 / 20) = 3
  │        c_d = 40 / 2 = 20
  │        n_nodes = ceil(3 / 2) = 2
  │        slurm_option_set(opt, "ntasks",       "42",    false)
  │        slurm_option_set(opt, "gpus",         "3",     false)
  │        slurm_option_set(opt, "cpus-per-gpu", "20",    false)
  │        slurm_option_set(opt, "nodes",        "2",     false)
  │        slurm_option_set(opt, "mem-bind",     "local", false)
  │      unsetenv("_SLURM_RSC_SPEC"), unsetenv("_SLURM_RSC_CONFIG")
  │
  ├─ spank_init_post_opt()
  │    spank_job_control_setenv(sp, "RSC_P", "42")
  │    spank_job_control_setenv(sp, "RSC_T", "1")
  │    spank_job_control_setenv(sp, "RSC_C", "1")
  │    spank_job_control_setenv(sp, "RSC_M", "1")
  │    setenv("SLURM_RSC_P", "42"), ...  ← --export=ALL で伝搬
  │    setenv("OMP_NUM_THREADS", "1", 0)
  │
  └─ slurm_opt_create_job_desc() → env_array_for_job()
       SLURM_NTASKS=42, SLURM_CPUS_PER_TASK=20 ✓
             |
             | RPC: job_submit_request
             v
[サーバ側（slurmctld）]
  cons_tres: _select_and_set_node()
    → gres_per_job=3, node 0: 2 GPU, node 1: 1 GPU
    → _cap_node_cpus_for_gres_per_job() [コアパッチ]
       node 0: cpus=40 (2 GPU × C_D=20) ✓
       node 1: cpus=20 (1 GPU × C_D=20) ✓  (修正前: cpus=40)
             |
             v
[REMOTE（slurmstepd）]
  slurm_spank_user_init()
    SLURM_CPUS_PER_TASK → SLURM_RSC_C_EFF
    OMP_* をセット
```

### 3 パス変換の概要（RSC_MODE_CPU）

`c_d = node_cpus / node_gres_count`:

| パス | 条件 | 設定するオプション |
|---|---|---|
| use_device | `node_gres_count > 0` かつ `gres_alloc_mode` 閾値以上 | `--ntasks=p --gpus=total_gres --cpus-per-gpu=c_d --mem-bind=local [--nodes=n_nodes]` |
| full-node | `p*c_eff >= node_cpus` | `--ntasks=p --cpus-per-task=c_eff --ntasks-per-node=n_tpn --nodes=n_needed` |
| core-count | `p*c_eff < node_cpus` | `--ntasks=p --cpus-per-task=c_eff --nodes=1` |

`gres_alloc_mode` の閾値:

| 設定値 | use_device に切り替わる条件 |
|---|---|
| `node`（デフォルト）| `p*c_eff >= node_cpus`（ノード排他以上） |
| `domain` | `p*c_eff >= c_d`（1 ドメイン以上） |
| `always` | 常に（最小 1 GPU）|

---

## 4. プラグイン構成

### 4-1. ファイル一覧

| コンポーネント | ソースファイル | plugin_type | 実行コンテキスト |
|---|---|---|---|
| spank_rsc | `src/plugins/cli_filter/rsc/spank_rsc.c` | —（SPANK） | LOCAL / ALLOCATOR / REMOTE |
| cli_filter_rsc | `src/plugins/cli_filter/rsc/cli_filter_rsc.c` | `cli_filter/rsc` | クライアント側（salloc/sbatch/srun） |
| cons_tres パッチ | `src/plugins/select/cons_tres/gres_select_filter.c` | — | slurmctld スケジューラ |
| salloc パッチ | `src/salloc/salloc.c` | — | クライアント側 |
| sbatch パッチ | `src/sbatch/sbatch.c` | — | クライアント側 |
| cli_filter インターフェース拡張 | `src/interfaces/cli_filter.c` / `cli_filter.h` | — | クライアント側 |

共有コード: `src/plugins/cli_filter/rsc/rsc_common.c` / `rsc_common.h`

### 4-2. デプロイ設定

```ini
# /etc/slurm/plugstack.conf
required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf

# /etc/slurm/slurm.conf
CliFilterPlugins=rsc
# JobSubmitPlugins は不要（v2.2 で廃止済み）
```

パッチ済みバイナリの配置（マルチグループ使用時は必須）:

```bash
install -m 755 src/salloc/salloc  $(prefix)/bin/salloc
install -m 755 src/sbatch/sbatch  $(prefix)/bin/sbatch
```

> **Note**: シングルグループ（`|` なし）の `--rsc` は未パッチバイナリでも動作する。
> マルチグループ構文（`|` 区切り）を使用する場合は、パッチ済み salloc/sbatch が必要。

### 4-3. ビルド

```bash
# cli_filter + SPANK プラグイン
cd slurm-25.11.4
make -j"$(nproc)" -C src/plugins/cli_filter/rsc/

# cons_tres select プラグイン（コアパッチ適用後）
make -j"$(nproc)" -C src/plugins/select/cons_tres/

# salloc / sbatch（マルチグループ対応のためコアパッチ適用後に再ビルド必須）
make -j"$(nproc)" src/salloc/salloc
make -j"$(nproc)" src/sbatch/sbatch
```

---

## 5. SPANK プラグイン実装詳細（spank_rsc）

### 5-1. 主要 API

```c
SPANK_PLUGIN(rsc, 1);

static struct spank_option rsc_options[] = {
    {
        .name    = "rsc",
        .arginfo = "RESOURCE_SPEC",
        .usage   = "Resource specification: p=N:t=N:c=N:m=N or g=N",
        .has_arg = 1,
        .val     = 1,
        .cb      = _rsc_opt_cb,
    },
    SPANK_OPTIONS_TABLE_END
};
```

### 5-2. `slurm_spank_init()` でのオプション登録と config パスのセット

```c
int slurm_spank_init(spank_t sp, int ac, char **av)
{
    const char *cfg = _get_plugin_arg(ac, av, "config");
    if (cfg)
        setenv("_SLURM_RSC_CONFIG", cfg, 1);  /* bridge for cli_filter_rsc */
    return spank_option_register(sp, &rsc_options[0]);
}
```

### 5-3. コールバック関数

```c
static char *rsc_optarg;  /* モジュールスタティック */

static int _rsc_opt_cb(int val, const char *optarg, int remote)
{
    (void) val; (void) remote;
    xfree(rsc_optarg);
    rsc_optarg = xstrdup(optarg);
    setenv("_SLURM_RSC_SPEC",   optarg, 1);  /* bridge for cli_filter_rsc */
    setenv("_SLURM_RSC_ACTIVE", "1",    1);  /* ':' het-job 禁止チェック用 */
    return ESPANK_SUCCESS;
}
```

### 5-4. `slurm_spank_init_post_opt()` での処理

LOCAL / ALLOCATOR コンテキストのみ実行。`rsc_optarg` 未セット時は
`_SLURM_SPANK_OPTION_rsc_rsc` 環境変数をフォールバックとして使用。

```
[init_post_opt: LOCAL/ALLOCATOR]
  spank_job_control_setenv(sp, "RSC_CONFIG", config_path)
  spank_job_control_setenv(sp, "RSC_CLUSTER", cluster)
  spank_job_control_setenv(sp, "RSC_SPEC", spec)
  CPU モード:
    spank_job_control_setenv(sp, "RSC_P", p_str)
    spank_job_control_setenv(sp, "RSC_T", t_str)
    spank_job_control_setenv(sp, "RSC_C", c_str)
    spank_job_control_setenv(sp, "RSC_M", m_str)
    setenv("SLURM_RSC_P", ...)  ← --export=ALL でジョブ環境に伝搬
    setenv("SLURM_RSC_T", ...)
    setenv("SLURM_RSC_C", ...)
    setenv("SLURM_RSC_M", ...)
    setenv("OMP_NUM_THREADS", t_str, 0)  ← salloc 子プロセス向け
    setenv("OMP_PROC_BIND", ..., 0)
    setenv("OMP_PLACES", ..., 0)
  GPU モード:
    spank_job_control_setenv(sp, "RSC_G", g_str)
    setenv("SLURM_RSC_G", ...)
  マルチグループ:
    _SLURM_RSC_NUM_GROUPS → spank_job_control_setenv(sp, "RSC_NUM_GROUPS", ...)
    _SLURM_RSC_HET_MAP → RSC_HET_GROUP_0..N-1 を個別にセット
    unsetenv("_SLURM_RSC_NUM_GROUPS"), unsetenv("_SLURM_RSC_HET_MAP")
```

### 5-5. ユーザジョブへの環境変数伝搬

ジョブ内で参照できる環境変数は 2 層ある:

- **`SLURM_SPANK_RSC_*`**: Slurm が spank_job_env に `SLURM_SPANK_` プレフィックスを付与して自動展開
- **`SLURM_RSC_*`**: `init_post_opt` の `setenv()` が `--export=ALL` で伝搬するユーザー向け変数

| 変数名 | 内容 | セット元 |
|---|---|---|
| `SLURM_RSC_SPEC` / `SLURM_SPANK_RSC_SPEC` | `--rsc` に渡した未加工の文字列 | setenv / job_control_setenv |
| `SLURM_RSC_P` / `SLURM_SPANK_RSC_P` | プロセス数 | 同上 |
| `SLURM_RSC_T` / `SLURM_SPANK_RSC_T` | プロセスあたりスレッド数 | 同上 |
| `SLURM_RSC_C` / `SLURM_SPANK_RSC_C` | 指定コア数（c_eff 適用前） | 同上 |
| `SLURM_RSC_M` / `SLURM_SPANK_RSC_M` | プロセスあたりメモリ (MiB) | 同上 |
| `SLURM_RSC_G` / `SLURM_SPANK_RSC_G` | GPU 数（GPU モード時のみ） | 同上 |
| `SLURM_RSC_NUM_GROUPS` / `SLURM_SPANK_RSC_NUM_GROUPS` | マルチグループ数 | setenv / job_control_setenv |
| `SLURM_RSC_HET_GROUP_N` / `SLURM_SPANK_RSC_HET_GROUP_N` | rsc グループ N の実際の het-group index | setenv / job_control_setenv |
| `SLURM_SPANK_RSC_CONFIG` | config ファイルパス（内部用） | job_control_setenv |
| `SLURM_SPANK_RSC_CLUSTER` | クラスタ名（内部用） | job_control_setenv |
| `SLURM_RSC_C_EFF` | 実効コア数（c_eff 適用後） | user_init（`SLURM_CPUS_PER_TASK` を転写） |
| `OMP_NUM_THREADS` | スレッド数 (= t) | user_init（未設定時のみ） |
| `OMP_PROC_BIND` | スレッドバインドポリシー | user_init（c=t→`close` / c<t→`close` / c>t→`spread`） |
| `OMP_PLACES` | スレッド配置単位 | user_init（c=t→`cores` / c<t→`threads` / c>t→`cores`） |

GPU モードの場合は `SLURM_RSC_SPEC` / `SLURM_RSC_G` のみがセットされ、
`SLURM_RSC_P/T/C/M`、`SLURM_RSC_C_EFF`、`OMP_*` はセットされない。

### 5-6. `slurm_spank_user_init()` での RSC_C_EFF と OMP_* セット

REMOTE コンテキストで実行される:

```c
int slurm_spank_user_init(spank_t sp, int ac, char **av)
{
    char c_eff_buf[32];

    /* SLURM_RSC_C_EFF: SLURM_CPUS_PER_TASK を転写 */
    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", c_eff_buf,
                     sizeof(c_eff_buf)) == ESPANK_SUCCESS)
        spank_setenv(sp, "SLURM_RSC_C_EFF", c_eff_buf, 1);

    /* OMP_*: SLURM_RSC_T / SLURM_RSC_C を読んで設定 */
    _set_omp_defaults(sp);

    return 0;
}
```

### 5-7. `slurm_spank_fini()`

```c
void slurm_spank_fini(spank_t sp, int ac, char **av)
{
    xfree(rsc_optarg);
    unsetenv("_SLURM_RSC_SPEC");
    unsetenv("_SLURM_RSC_CONFIG");
    unsetenv("_SLURM_RSC_ACTIVE");       /* ':' het-job チェック用 */
    unsetenv("_SLURM_RSC_NUM_GROUPS");   /* マルチグループ bridge */
    unsetenv("_SLURM_RSC_HET_MAP");      /* マルチグループ bridge */
}
```

---

## 6. cli_filter プラグイン実装詳細（cli_filter_rsc）

### 6-1. bridge env var の仕組み

`slurm_spank_init` が `_SLURM_RSC_CONFIG`、`_rsc_opt_cb` が `_SLURM_RSC_SPEC` と
`_SLURM_RSC_ACTIVE` をセット。`cli_filter_g_pre_submit(opt)` はこれらを読み取る。

### 6-2. `cli_filter_p_pre_submit()` の主要ロジック

```c
extern int cli_filter_p_pre_submit(slurm_opt_t *opt, int offset)
{
    /* ':' het-job との組み合わせ禁止 */
    if (offset > 0 && getenv("_SLURM_RSC_ACTIVE")) {
        error("rsc: --rsc cannot be combined with the ':' het-job syntax");
        return SLURM_ERROR;
    }

    cfg_path = getenv("_SLURM_RSC_CONFIG");
    spec     = getenv("_SLURM_RSC_SPEC");
    if (!cfg_path || !spec)
        return SLURM_SUCCESS;  /* --rsc 未使用 */

    rsc_load_config(cfg_path, cluster, &cfg, ...);
    rsc_parse_groups(spec, &cfg, &reqs, &n_groups, ...);

    req = &reqs[0];  /* グループ 0 はプライマリコンポーネント */

    /* 競合チェック（SLURM_JOB_ID がセット済みの srun ステップはスキップ）*/
    if (!getenv("SLURM_JOB_ID")) {
        for (int i = 0; i < cfg.entry_count; i++) {
            if (slurm_option_isset(opt, entries[i].key)) {
                if (entries[i].policy == RSC_POLICY_DENY) → SLURM_ERROR
                else → info(警告)
            }
        }
    }

    /* --- RSC_MODE_CPU: 3 パス分岐 --- */
    c_eff = rsc_compute_effective_cores(req->c, req->m_mib,
                                        cfg.node_cpus, cfg.node_mem_mib);
    slurm_option_set(opt, "ntasks", p_str, false);

    if (cfg.node_cpus && c_eff) {
        if (cfg.node_gres_count > 0) {
            c_d = cfg.node_cpus / cfg.node_gres_count;
            use_device = (gres_alloc_mode で閾値判定);
        }

        if (use_device) {
            /* use_device path: GPU ドメイン単位 */
            total_gres = ceil(p * c_eff / c_d);   /* 必要 GPU ドメイン数 */
            c_eff_final = floor(total_gres * c_d / p); /* 実効 cpus-per-gpu 相当 */
            n_nodes = ceil(total_gres / node_gres_count);

            slurm_option_set(opt, "gpus",         total_gres_str, false);
            slurm_option_set(opt, "cpus-per-gpu", c_d_str,        false);
            slurm_option_set(opt, "mem-per-cpu",  mpc_str,        false);
            slurm_option_set(opt, "nodes",        n_nodes_str,    false);
            slurm_option_set(opt, "mem-bind",     "local",        false);

        } else if (p * c_eff >= node_cpus) {
            /* full-node path: ノード単位 */
            n_tpn    = node_cpus / c_eff;
            n_needed = ceil(p / n_tpn);

            slurm_option_set(opt, "cpus-per-task",   c_eff_str,    false);
            slurm_option_set(opt, "mem-per-cpu",     mem_cpu_str,  false);
            slurm_option_set(opt, "ntasks-per-node", n_tpn_str,    false);
            slurm_option_set(opt, "nodes",           n_needed_str, false);

        } else {
            /* core-count path: 1 ノードに収まる */
            slurm_option_set(opt, "cpus-per-task", c_eff_str,   false);
            slurm_option_set(opt, "mem-per-cpu",   mem_cpu_str, false);
            slurm_option_set(opt, "nodes",         "1",         false);
        }
    }

    /* --- マルチグループ（グループ 1..N-1）--- */
    if (n_groups > 1) {
        for (int i = 1; i < n_groups; i++) {
            _apply_rsc_group(&reqs[i], &cfg, &next_idx);
            /* cli_filter_het_append() で追加コンポーネントを登録 */
        }
        /* bridge env vars でインデックスマップを spank_rsc に伝える */
        setenv("_SLURM_RSC_NUM_GROUPS", n_stored_str, 1);
        setenv("_SLURM_RSC_HET_MAP", "0,1,2,...", 1);
    }

    unsetenv("_SLURM_RSC_SPEC");
    unsetenv("_SLURM_RSC_CONFIG");
    rsc_config_free(&cfg);
    return SLURM_SUCCESS;
}
```

公開 API（`src/common/slurm_opt.h`）:
- `slurm_option_isset(opt, name)` — 既設定チェック
- `slurm_option_set(opt, name, value, early_pass)` — オプション値設定

### 6-3. `_apply_rsc_group()` — マルチグループ用 het-job コンポーネント生成

グループ 1..N-1 を `cli_filter_het_append()` に渡す:

```c
static int _apply_rsc_group(const rsc_request_t *req, const rsc_config_t *cfg,
                             int *next_idx)
{
    job_desc_msg_t *jd = xmalloc(sizeof(*jd));
    slurm_init_job_desc_msg(jd);

    if (req->mode == RSC_MODE_GPU) {
        jd->num_tasks   = req->g;
        jd->tres_per_task = xstrdup("gres/gpu:1");
        if (cfg->node_cpus && cfg->node_gres_count)
            jd->cpus_per_task = node_cpus / node_gres_count;
    } else {
        /* RSC_MODE_CPU: 3 パス分岐（プライマリと同じロジック） */
        /* use_device: tres_per_job + cpus_per_tres + mem_bind_type */
        /* full-node:  ntasks_per_node + min/max_nodes */
        /* core-count: min_nodes=max_nodes=1 */
    }

    cli_filter_het_append(jd);
    (*next_idx)++;
    return 0;
}
```

`cli_filter_het_append()` はパッチ済み salloc/sbatch でのみ有効な weak シンボル:
- 未パッチバイナリでは NULL になる（単一グループは動作可能）
- マルチグループ構文使用時は明示エラーを返す

### 6-4. エラーハンドリング方針

| エラー種別 | 返り値 | 理由 |
|---|---|---|
| `:` het-job との組み合わせ | `SLURM_ERROR` | 意図しないコンポーネント数を防ぐ |
| 競合 deny | `SLURM_ERROR` | 投入前にユーザへ即フィードバック |
| 競合 warn | `SLURM_SUCCESS` + `slurm_info()` | 警告のみ、ジョブは通す |
| config 読み込み失敗 | `SLURM_SUCCESS` + `info()` | `spank_init_post_opt()` でも報告 |
| マルチグループで cli_filter_het_append が NULL | `SLURM_ERROR` | 未パッチバイナリの明示エラー |

### 6-5. `salloc` / `sbatch` へのパッチ

マルチグループ（`--rsc 'S0 | S1'`）に対応するため、`cli_filter_rsc` が生成した
追加 het-job コンポーネントを `salloc`/`sbatch` が回収する仕組みを追加した。

**新規 API（`src/interfaces/cli_filter.h`）**:

```c
void      cli_filter_het_ctx_init(void);              /* het-job コンテキスト初期化 */
list_t   *cli_filter_het_ctx_fini(void);              /* 追加コンポーネント回収 */
void      cli_filter_het_append(job_desc_msg_t *jd);  /* weak シンボル */
```

`cli_filter_het_append()` は **weak シンボル**として宣言されている。
未パッチバイナリでは NULL になるため、`cli_filter_rsc` はこの関数が NULL の場合に
マルチグループ使用時は `SLURM_ERROR` を返す（Section 6-4 参照）。

**`src/salloc/salloc.c` の変更箇所**:

```c
/* main(): het-job ループ前に初期化 */
cli_filter_het_ctx_init();

/* ループ後: cli_filter プラグインが追加したコンポーネントを回収 */
list_t *extra = cli_filter_het_ctx_fini();
if (extra && list_count(extra)) {
    if (!job_req_list) {
        job_req_list = list_create(NULL);
        list_append(job_req_list, desc);
    }
    het_job_limit += list_count(extra);
    list_transfer(job_req_list, extra);
    FREE_NULL_LIST(extra);
}
```

`src/sbatch/sbatch.c` も同一パターンで変更している。

**処理の流れ**:

1. `cli_filter_het_ctx_init()` でスレッドローカルリストを初期化
2. `cli_filter_g_pre_submit()` 内で `cli_filter_rsc` が `cli_filter_het_append(jd)` を呼び出し、グループ 1..N-1 の `job_desc_msg_t` をリストに追加
3. ループ完了後、`cli_filter_het_ctx_fini()` でリストを返却
4. `salloc`/`sbatch` がリストの内容を `job_req_list` に転送し、`het_job_limit` を加算して通常の het-job として投入

---

## 7. デフォルト値・設定ファイル

### 7-1. ファイル配置

```
# /etc/slurm/plugstack.conf
required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf
```

### 7-2. 設定ファイルフォーマット

```ini
# /etc/slurm/rsc_defaults.conf

[default]
m_default = 4096        # コアあたりのデフォルトメモリ (MiB/コア)
node_count_mode = exact # exact | min | none
node_cpus      = 48     # ノードあたり実効コア数（CoreSpec 除外後）
node_mem       = 196608 # ノードあたり利用可能メモリ MiB
node_gres_count = 0     # GPU 数 / NUMA ドメイン数（0 = 無効）
                        #   GPU クラスタ:    --rsc g=N の cpus_per_task 計算に使用
                        #   CPU ドメイン割当: C_D = node_cpus / node_gres_count
gres_alloc_mode = node  # node | domain | always
                        #   node: p*c_eff >= node_cpus で use_device path に切替
                        #   domain: p*c_eff >= C_D で切替（1 ドメイン以上）
                        #   always: 常に use_device path（最小 1 GPU）

[cluster:gpuCluster]
m_default = 8192
node_cpus  = 64
node_mem   = 262144
node_gres_count = 8     # 8 GPU/ノード
gres_alloc_mode = node

[cluster:clusterA]
m_default = 8192
node_cpus  = 64
node_mem   = 262144
node_gres_count = 2     # 2 ソケット構成（CPU ドメイン割当）
gres_alloc_mode = domain

[cluster:clusterB]
m_default = 2048
node_count_mode = min
node_cpus  = 48
node_mem   = 196608

[forbidden_with_rsc]
# 値: deny (エラーで拒否) または warn (警告のみ)
# リストにないオプションは常に許可
# SLURM_JOB_ID がセット済みの srun ステップではチェックをスキップ

# グループ A: 資源量の直接競合 (deny 推奨)
ntasks           = deny
cpus-per-task    = deny
mem              = deny
mem-per-cpu      = deny
mem-per-task     = deny
nodes            = deny
ntasks-per-node  = deny
gres             = deny
gpus             = deny
gpus-per-node    = deny
gpus-per-socket  = deny
gpus-per-task    = deny
cpus-per-gpu     = deny
mem-per-gpu      = deny
mincpus          = deny
ntasks-per-gpu   = deny
threads-per-core = deny

# グループ B: トポロジ・配置制約 (warn 推奨)
cores-per-socket  = warn
sockets-per-node  = warn
ntasks-per-core   = warn
ntasks-per-socket = warn
core-spec         = warn
thread-spec       = warn
exclusive         = warn
oversubscribe     = warn
overcommit        = warn
distribution      = warn
spread-job        = warn

# グループ C: ノード選択制約 (warn 推奨)
constraint         = warn
cluster-constraint = warn
contiguous         = warn
exclude            = warn
nodelist           = warn
nodefile           = warn
switches           = warn
user-min-nodes     = warn

# グループ D: 運用ルール上の制御対象 (warn 推奨)
batch      = warn
clusters   = warn
qos        = warn
gpu-bind   = warn
mem-bind   = warn
gres-flags = warn

# --rsc 未指定時（暗黙デフォルト適用時）にもチェックを行うか
implicit_check = relaxed  # strict | relaxed
```

#### 設定フィールドの意味と省略時の扱い

| フィールド | 意味 | 省略・0 の場合 |
|---|---|---|
| `node_cpus` | CoreSpec 除外後の実効コア数 | c_eff 計算をスキップ（c をそのまま使用） |
| `node_mem` | MemSpec 除外後の利用可能メモリ MiB | c_eff のメモリ換算をスキップ |
| `node_gres_count` | GPU 数または CPU ドメイン数 | use_device path を無効化 |
| `gres_alloc_mode` | use_device path への切り替え閾値 | デフォルト `node` |

---

## 8. job_submit プラグイン（v2.2 で廃止）

`job_submit_rsc.so` は不要。`JobSubmitPlugins=rsc` の設定も不要。  
すべての処理がクライアント側（cli_filter + spank）に移動済み。

### 8-1. Slurm パラメータマッピング（最終）

| `--rsc` パラメータ | Slurm オプション | 設定主体 |
|---|---|---|
| `p` | `--ntasks=p` | cli_filter_rsc |
| `c_eff`（use_device） | `--cpus-per-gpu=c_d`（注: cpus_per_task は設定しない）| cli_filter_rsc |
| `c_eff`（full/core-count） | `--cpus-per-task=c_eff` | cli_filter_rsc |
| GPU ドメイン数（use_device） | `--gpus=total_gres` | cli_filter_rsc |
| `floor(C/c_eff)`（full-node） | `--ntasks-per-node=n_tpn` | cli_filter_rsc |
| `ceil(p/n_tpn)` または `ceil(total_gres/G)` | `--nodes=n_needed` | cli_filter_rsc |
| `t` | `OMP_NUM_THREADS` | spank_rsc（init_post_opt / user_init） |
| `g` | `--ntasks=g --gpus-per-task=1` | cli_filter_rsc |
| c_eff | `SLURM_RSC_C_EFF` | spank_rsc（user_init で `SLURM_CPUS_PER_TASK` を転写） |

---

## 9. `--rsc` 解析・判定フローチャート

```
START
  |
  v
[クライアント: getopt ループ]
  _rsc_opt_cb() → setenv("_SLURM_RSC_SPEC", spec)
               → setenv("_SLURM_RSC_ACTIVE", "1")

  v
[クライアント: cli_filter_g_pre_submit(opt, offset)]
  |
  +-- offset > 0 && _SLURM_RSC_ACTIVE?
       YES: error() → SLURM_ERROR
  |
  +-- _SLURM_RSC_CONFIG / _SLURM_RSC_SPEC → どちらか NULL?
       YES: return SLURM_SUCCESS
  |
  rsc_load_config() → cfg
  rsc_parse_groups() → reqs[0..n_groups-1]

  req = &reqs[0]

  [競合チェック: SLURM_JOB_ID なし時のみ]
    deny → SLURM_ERROR
    warn → info() → 継続

  [CPU モード]
    c_eff = rsc_compute_effective_cores(c, m_mib, node_cpus, node_mem)

    slurm_option_set("ntasks", p)

    node_gres_count > 0?
      YES: c_d = node_cpus / node_gres_count
           gres_alloc_mode で use_device 判定:
             always → use_device=true
             domain → use_device = (p*c_eff >= c_d)
             node   → use_device = (p*c_eff >= node_cpus)

    use_device?
      YES: total_gres = ceil(p*c_eff / c_d)
           c_eff_final = floor(total_gres*c_d / p)
           n_nodes = ceil(total_gres / node_gres_count)
           slurm_option_set("gpus",         total_gres)
           slurm_option_set("cpus-per-gpu", c_d)
           slurm_option_set("mem-per-cpu",  max(device_mpc, user_mpc))
           slurm_option_set("nodes",        n_nodes)
           slurm_option_set("mem-bind",     "local")
      NO:
        p*c_eff >= node_cpus?
          YES (full-node):
               n_tpn = node_cpus / c_eff
               n_needed = ceil(p / n_tpn)
               slurm_option_set("cpus-per-task",   c_eff)
               slurm_option_set("mem-per-cpu",     ceil(m/c_eff))
               slurm_option_set("ntasks-per-node", n_tpn)
               slurm_option_set("nodes",           n_needed)
          NO (core-count):
               slurm_option_set("cpus-per-task", c_eff)
               slurm_option_set("mem-per-cpu",   ceil(m/c_eff))
               slurm_option_set("nodes",         "1")

  [GPU モード]
    slurm_option_set("ntasks",        g)
    slurm_option_set("gpus-per-task", "1")
    node_gres_count > 0?
      YES: slurm_option_set("cpus-per-task", node_cpus/node_gres_count)

  [マルチグループ n_groups > 1]
    for i in 1..n_groups-1:
      _apply_rsc_group(&reqs[i], &cfg, &next_idx)
        → cli_filter_het_append(jd)
    setenv("_SLURM_RSC_NUM_GROUPS", n_stored)
    setenv("_SLURM_RSC_HET_MAP",    "0,1,2,...")

  unsetenv("_SLURM_RSC_SPEC"), unsetenv("_SLURM_RSC_CONFIG")

  v
[クライアント: spank_init_post_opt()]
  LOCAL/ALLOCATOR context → SLURM_RSC_* + OMP_* をセット

  v
[クライアント: slurm_opt_create_job_desc()]
  ntasks_set=true → JOB_NTASKS_SET bit
  env_array_for_job() → SLURM_NTASKS, SLURM_CPUS_PER_TASK ✓

  v
[RPC → slurmctld]
  cons_tres: gres_select_filter_select_and_set()
    → _select_and_set_node()
    → _cap_node_cpus_for_gres_per_job() [コアパッチ]  ← 端数ノード修正

  v
[REMOTE: slurm_spank_user_init()]
  SLURM_CPUS_PER_TASK → SLURM_RSC_C_EFF
  SLURM_RSC_T/C → OMP_* をセット

END
```

---

## 10. 実装上の注意点・制約

### 10-1. use_device path と cpus_per_task の排他関係

`--cpus-per-gpu` と `--cpus-per-task` は Slurm 内部で**排他**。
use_device path では `cpus_per_task` を設定せず `cpus-per-gpu=c_d` のみを使用する。
これにより `SLURM_CPUS_PER_TASK` は Slurm が自動的に `c_d` の値でセットし、
`spank_rsc.user_init` が `SLURM_RSC_C_EFF` に転写する。

### 10-2. `mem-bind=local` の役割

use_device path で `--mem-bind=local` を設定する理由:
- `gres.conf Cores=` でコアが GPU ドメインに紐づいている
- `mem-bind=local` により NUMA ローカルメモリのみ使用
- 端数ノード（GPU が少ない）でも GPU ドメイン外のメモリを確保しない

### 10-3. cons_tres コアパッチとの関係

`--gpus=N --cpus-per-gpu=C_D` は `gres_per_job` パスを使用する。
Slurm のスケジューラ（cons_tres）は `gres_per_job` の場合、ノード間で GPU を配分する際に
端数ノード（GPU が少ない）でも全コアを割り当てる問題があった。
コアパッチ（Section 11）がこれを修正する。

### 10-4. node_count_mode の扱い

| 設定値 | 動作 |
|---|---|
| `exact`（デフォルト）| min_nodes = max_nodes = n_needed（厳密固定） |
| `min` | min_nodes = n_needed のみ（上限フリー） |
| `none` | ノード数を設定しない（Slurm デフォルト） |

use_device path では `--gpus=N` を使用するため Slurm が GPU 配置を決定する。
`node_count_mode` は n_nodes をヒントとして `--nodes` に設定するが、
GPU の配置次第で異なる場合がある。

### 10-5. `--rsc` と `:` het-job 構文の禁止

`_SLURM_RSC_ACTIVE` bridge env var を使用して `offset > 0` の呼び出しを拒否する:
- `_rsc_opt_cb` で `_SLURM_RSC_ACTIVE=1` をセット
- `pre_submit` で `offset > 0 && _SLURM_RSC_ACTIVE` → `SLURM_ERROR`
- `slurm_spank_fini` で `unsetenv("_SLURM_RSC_ACTIVE")`

マルチグループは `--rsc 'SPEC0 | SPEC1'` 構文を使用し、`:` 構文は不要。

### 10-6. bridge env var のスコープとクリーンアップ

| bridge env var | セット元 | クリーンアップ |
|---|---|---|
| `_SLURM_RSC_CONFIG` | `slurm_spank_init` | `pre_submit` 正常終了 + `spank_fini` |
| `_SLURM_RSC_SPEC` | `_rsc_opt_cb` | `pre_submit` 正常終了 + `spank_fini` |
| `_SLURM_RSC_ACTIVE` | `_rsc_opt_cb` | `spank_fini` |
| `_SLURM_RSC_NUM_GROUPS` | `pre_submit`（マルチグループ） | `spank_init_post_opt` + `spank_fini` |
| `_SLURM_RSC_HET_MAP` | `pre_submit`（マルチグループ） | `spank_init_post_opt` + `spank_fini` |

---

## 11. cons_tres コアパッチ

### 11-1. 問題と根本原因

`--gpus=3 --cpus-per-gpu=20`（`gres_per_job=3`, `cpus_per_gres=20`）で
2 ノードに GPU を 2+1 配分したとき、1 GPU のノードが `CPU_IDs=0-39`（期待: `0-19`）になる。

**根本原因**: `gres_select_filter.c:_select_and_set_node()` が
`gres_cnt_node_select[node_inx]`（ノードあたり GPU 数）を決定するのは、
`job_res->core_bitmap` と `job_res->cpus[]` が `dist_tasks()` によって確定した**後**。
そのため per-node GPU 数に応じた CPU 制限が間に合わない。

### 11-2. 修正内容

**対象ファイル**: `src/plugins/select/cons_tres/gres_select_filter.c`

**新規ヘルパー関数** `_cap_node_cpus_for_gres_per_job()`:

```c
/*
 * _cap_node_cpus_for_gres_per_job - gres_per_job + cpus_per_gres の
 * 端数ノードで CPU 過剰確保を防ぐ。
 *
 * _set_job_bits1() が gres_bit_select[node_inx] を確定した直後に呼ぶ。
 * 割り当て GPU の topo_core_bitmap を OR 合成し、そのコアのみ
 * job_res->core_bitmap に残す。job_res->cpus[job_node_inx] も上限制限。
 */
static void _cap_node_cpus_for_gres_per_job(
    struct job_resources *job_res, int job_node_inx,
    int node_inx, int node_cnt,
    gres_job_state_t *gres_js, gres_node_state_t *gres_ns,
    node_record_t *node_ptr)
```

処理の流れ:
1. `gres_bit_select[node_inx]` から割り当て済み GPU インデックスを走査
2. 各 GPU に対応する `topo_core_bitmap` を `bound_cores` に OR 合成
3. `bound_cores` が空なら早期リターン（非標準 `--distribution` への安全策）
4. `core_bitmap` から `bound_cores` 外のコアを除去
5. `cpus[job_node_inx]` を `bit_set_count(bound_cores) × tpc` に上限制限

**変更箇所 1: no-topology パス**（`gres.conf` に `Cores=` なし）

`gres_cnt_node_select` 確定後、`return 0` の前に CPU 数のみ上限制限:

```c
if (gres_js->gres_per_job && gres_js->cpus_per_gres &&
    gres_js->gres_cnt_node_select[node_inx] > 0) {
    uint32_t max_cpus =
        (uint32_t)gres_js->gres_cnt_node_select[node_inx]
        * gres_js->cpus_per_gres;
    if (job_res->cpus[job_node_inx] > (uint16_t)max_cpus)
        job_res->cpus[job_node_inx] = (uint16_t)max_cpus;
}
```

**変更箇所 2: topology パス**（`gres.conf` に `Cores=` あり）

`_set_job_bits1()` の直後に `_cap_node_cpus_for_gres_per_job()` を呼び出し:

```c
if (gres_js->cpus_per_gres &&
    gres_js->gres_bit_select && gres_js->gres_bit_select[node_inx] &&
    gres_ns->topo_cnt && gres_ns->topo_core_bitmap && gres_ns->topo_gres_bitmap)
    _cap_node_cpus_for_gres_per_job(job_res, job_node_inx,
                                    node_inx, node_cnt,
                                    gres_js, gres_ns, node_ptr);
```

### 11-3. 期待効果

環境: `p=42`, `node_cpus=40`, `node_gres_count=2`, `C_D=20`, `total_gres=3`, `n_nodes=2`

| ノード | パッチ前 | パッチ後 |
|---|---|---|
| xx0001 (2 GPU) | CPU_IDs=0-39 ✓ | CPU_IDs=0-39 ✓ |
| xx0002 (1 GPU) | CPU_IDs=0-39 ✗ | CPU_IDs=0-19 ✓ |
| AllocTRES | cpu=80 ✗ | cpu=60 ✓ |

### 11-4. 注意事項

`dist_tasks()` はパッチ適用前に完了しているため、タスクはパッチ前の
`core_bitmap` に基づいて配置済みである。デフォルト分配（block/cyclic）は
コア 0 から順に割り当てるため、端数ノードのタスクは 0-19 内に収まる。

非標準 `--distribution` 使用時（例: cyclic で 0, 20, 1, 21 ... に配置した場合）
コア 20 がパッチにより除去されてタスク配置と不整合になる可能性がある。
この安全策として `bound_cores` が空のノードにはパッチを適用しない設計とした。

---

## 12. GPU ドメイン割当と gres によるアフィニティ

### 12-1. spec 2-3 の定義との対応

仕様書（spec 2-3）の **G 分ノード**（GPU ドメイン）と **D 分ノード**（CPU ドメイン）は
同一の機構（`node_gres_count` + use_device path）で実装する。

| 概念 | rsc_defaults.conf | gres.conf |
|---|---|---|
| G 分ノード（GPU ドメイン） | `node_gres_count = G`（実際の GPU 数）| `Name=gpu File=/dev/nvidia[0-N] Cores=...` |
| D 分ノード（CPU ドメイン） | `node_gres_count = D`（ソケット/NUMA 数）| `Name=gpu File=/dev/slurm-rsc/socketN Cores=...`（仮想 GRES）|

### 12-2. gres.conf の設定例

**GPU クラスタ（4 GPU × 48 コア、各 GPU に 12 コア）**:

```ini
# /etc/slurm/slurm.conf
GresTypes=gpu

# /etc/slurm/gres.conf
NodeName=gpu[01-08] Name=gpu File=/dev/nvidia[0-3] \
    Cores=0-11,12-23,24-35,36-47 AutoDetect=off
```

```ini
# /etc/slurm/rsc_defaults.conf
[cluster:gpuCluster]
node_cpus = 48
node_mem  = 196608
node_gres_count = 4    # G = 4 GPU/ノード
gres_alloc_mode = node # p*c_eff >= 48 で use_device path へ
```

**CPU ドメイン割当（2 ソケット × 24 コア、仮想 GRES を使用）**:

```ini
# /etc/slurm/slurm.conf
GresTypes=gpu

# /etc/slurm/gres.conf（仮想 GRES: ソケットを GPU ドメインとして定義）
NodeName=node[01-32] Name=gpu File=/dev/slurm-rsc/socket[01] \
    Cores=0-23,24-47 AutoDetect=off
```

```ini
# /etc/slurm/rsc_defaults.conf
[cluster:clusterA]
node_cpus = 48
node_mem  = 196608
node_gres_count = 2    # D = 2 ソケット/ノード
gres_alloc_mode = domain  # p*c_eff >= C_D=24 で use_device path へ
```

### 12-3. use_device path の計算例

`p=42, c=1, node_cpus=40, node_gres_count=2, gres_alloc_mode=node`:

| 変数 | 計算 | 値 |
|---|---|---|
| `c_eff` | `max(1, ceil(m/M × 40))` | `1`（m_mib=m_default=4096 と仮定） |
| `c_d` | `40 / 2` | `20` |
| `use_device` | `42*1=42 >= node_cpus=40` → node モード | `true` |
| `total_gres` | `ceil(42×1 / 20)` | `3` |
| `c_eff_final` | `floor(3×20 / 42)` | `1`（→ max(c_eff_final, c_eff) = 1） |
| `n_nodes` | `ceil(3 / 2)` | `2` |
| Slurm オプション | `--ntasks=42 --gpus=3 --cpus-per-gpu=20 --mem-bind=local --nodes=2` | — |

---

## 13. マルチグループ構文

### 13-1. 構文

```bash
# 2 グループの het-job
sbatch --rsc 'p=4:c=4 | p=2:c=8' job.sh

# GPU と CPU の混在
sbatch --rsc 'g=4 | p=8:c=2' job.sh
```

`|` 区切りで最大 `RSC_MAX_GROUPS=8` グループまで指定可能。

### 13-2. 処理フロー

1. `rsc_parse_groups()` が `|` 区切りをパース → `rsc_request_t` 配列
2. グループ 0: `slurm_option_set()` でプライマリコンポーネントに設定（通常の single-group と同じ）
3. グループ 1..N-1: `_apply_rsc_group()` → `cli_filter_het_append(jd)` で追加コンポーネント登録
4. 追加完了後、het-group index マップを bridge env var でセット:
   - `_SLURM_RSC_NUM_GROUPS=N`
   - `_SLURM_RSC_HET_MAP=0,1,2,...`
5. `spank_init_post_opt` がマップを読んで `SLURM_SPANK_RSC_HET_GROUP_N` をジョブ環境にセット

### 13-3. het-group インデックスの使い方

```bash
# job.sh 内（インデックスがずれても正しく参照できる）
srun --het-group=$SLURM_SPANK_RSC_HET_GROUP_0 ./app_a &
srun --het-group=$SLURM_SPANK_RSC_HET_GROUP_1 ./app_b &
wait
```

### 13-4. 前提条件

`cli_filter_het_append()` はパッチ済み salloc/sbatch にのみ存在する weak シンボル。
未パッチバイナリでは NULL になるため、マルチグループ使用時は専用 RPM のインストールが必要。

---

## 14. 検証方法

```bash
# ビルド
cd slurm-25.11.4
make -j"$(nproc)" -C src/plugins/cli_filter/rsc/
make -j"$(nproc)" -C src/plugins/select/cons_tres/

# --- CPU モード（core-count path）確認 ---
salloc --rsc p=2:t=2:c=2:m=1G
# 期待: --ntasks=2 --cpus-per-task=2 --nodes=1

# --- CPU モード（full-node path）確認 ---
# node_cpus=48, c_eff=4 → n_tpn=12, n_needed=ceil(4/12)=1
salloc --rsc p=4:c=4
scontrol show job $SLURM_JOBID -d
# 期待: NtasksPerN=12, NumNodes=1, CPU_IDs=0-47

# --- use_device path 確認 ---
# node_cpus=40, node_gres_count=2, gres_alloc_mode=node
# p=42, c=1: total_gres=3, n_nodes=2
salloc --rsc p=42:t=1:c=1
scontrol show job $SLURM_JOBID -d
# 期待:
#   Nodes=xx0001 CPU_IDs=0-39 GRES=gpu:2(IDX:0-1)  AllocTRES=cpu=40
#   Nodes=xx0002 CPU_IDs=0-19 GRES=gpu:1(IDX:0)    AllocTRES=cpu=20
#   AllocTRES=cpu=60  ← コアパッチが有効なこと

# --- 環境変数確認 ---
salloc --rsc p=2:t=2:c=2:m=1G /bin/env | grep -E "SLURM_RSC|OMP_|SLURM_NTASKS"
# 期待値:
#   SLURM_NTASKS=2
#   SLURM_RSC_SPEC=p=2:t=2:c=2:m=1G
#   SLURM_RSC_P=2, SLURM_RSC_T=2, SLURM_RSC_C=2, SLURM_RSC_M=1024
#   SLURM_RSC_C_EFF=2
#   OMP_NUM_THREADS=2, OMP_PROC_BIND=close, OMP_PLACES=cores
#   SLURM_SPANK_RSC_* も同じ値で存在する

# --- GPU モード確認（node_gres_count=8 クラスタ）---
salloc --rsc g=2
scontrol show job $SLURM_JOBID -d
# 期待: TRES=cpu=16,gres/gpu=2  (cpus-per-task=64/8=8, gpus-per-task=1)

# --- ':' het-job 禁止確認 ---
salloc --rsc p=4:c=4 : --ntasks=2
# 期待: "rsc: --rsc cannot be combined with the ':' het-job syntax" エラー

# --- マルチグループ確認 ---
sbatch --rsc 'p=4:c=4 | p=2:c=8' job.sh
# 期待: het-job として投入、SLURM_SPANK_RSC_HET_GROUP_0=0, HET_GROUP_1=1

# --- 競合オプション確認 ---
salloc --rsc p=4:c=4 --ntasks=8
# 期待: "rsc: option --ntasks conflicts with --rsc" エラー
```
