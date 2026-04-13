# `--rsc` オプション実装設計ノート v2

> 対象: Slurm 25.11.4  
> 根拠仕様: `specs/slurm-sched-spec.new.md` 項目 2-1〜2-4、8-2、8-4  
> v1 (`slurm-rsc-option-impl.md`) からの変更点:
> - cli_filter プラグインを追加し dlopen/dlsym による `slurm_opt_t` アクセスを廃止
> - ノード情報（C/M/G）を conf ファイルに定義し、c_eff 計算・ノード数設定をクライアント側に移動

---

## 1. 概要

spec 2-4 は、ジョブの CPU/GPU 資源を一括指定するための統合オプション `--rsc` を定義する。  
Slurm は `--rsc` を標準でサポートしないため、**2 つのプラグイン**の組み合わせで実装する。

| # | プラグイン | 役割 |
|---|---|---|
| ① | spank_rsc.so | `--rsc` CLI オプションの登録・受付・bridge env var のセット（spec + config パス）・`user_init()` で `SLURM_RSC_C_EFF` をセット |
| ② | cli_filter_rsc.so | spec 解析・競合オプション検出（deny/warn）・c_eff 計算・ntasks/cpus/nodes/ntasks-per-node を `slurm_opt_t` に設定 |

**v1 → v2 の変更経緯**:  
v1 では spank_rsc が `dlopen(NULL, 0) + dlsym(h, "opt")` で `slurm_opt_t *` を直接操作していたが、
これは Slurm 内部の非公式なパターンで構造体レイアウト変化に脆弱だった。  
v2.0 では cli_filter プラグインが公開 API `slurm_option_set()` を通じて設定する方式に変更した。

**v2.0 → v2.1 の変更経緯**:  
v2.0 では c_eff（メモリ換算実効コア数）の計算を job_submit（slurmctld 側）で行っていた。
理由はノード情報（C = 実効コア数、M = 利用可能メモリ）が slurmctld 内部 API でのみ
正確に取得できるためだった。  
v2.1 では `rsc_defaults.conf` に `node_cpus` / `node_mem` / `node_gpus` を定義し、
cli_filter がこれを読んで c_eff を計算・設定する。投入前に即座にエラーを返せる利点がある。

**v2.1 → v2.2 の変更経緯**:  
v2.1 時点で job_submit_rsc に残っていた 3 つの責務がすべてクライアント側で代替できることが確認された。  
- 競合オプション検出: `slurm_option_isset()` で cli_filter から投入前に検出可能（`#SBATCH` ディレクティブも cli_filter 呼び出し前に `slurm_opt_t` へ展開済み）  
- GPU gres 設定: `--gpus-per-task=1` を cli_filter で設定すれば Slurm が `tres_per_task` に自動変換  
- RSC_C_EFF: spank `user_init()` が `SLURM_CPUS_PER_TASK`（cli_filter 設定済み）を転写するだけでよい（OMP_NUM_THREADS と同パターン）  
以上により job_submit_rsc を廃止し 2 プラグイン構成に整理した。`JobSubmitPlugins=rsc` も不要になる。

---

## 2. `--rsc` オプション仕様まとめ

### 2-1. 書式（spec 2-4）

```
CPU 資源指定:  --rsc p={procs}:t={threads}:c={cores}:m={memory}
GPU 資源指定:  --rsc g={gpus}
```

- (1) と (2) は**排他**（同時指定不可）
- `{...}` はプレースホルダ。ジョブ投入時に実際の数値で置き換える

### 2-2. 各パラメータの意味と制約（spec 2-1、2-2）

| パラメータ | 意味 | 制約 | 省略時デフォルト | 最大値 |
|---|---|---|---|---|
| `p` | プロセス数 | 正整数 | `1` | Slurm パーティション / QOS 設定に委任 |
| `t` | プロセスあたりスレッド数 | 正整数 | `1` | `c × 4`（ハードコーディング） |
| `c` | プロセスあたりコア数 | `1 ≤ c ≤ C`、かつ c は t の約数または倍数 | `1` | Slurm パーティション / QOS 設定に委任 |
| `m` | プロセスあたりメモリ上限 (MiB) | 正整数（単位サフィックス M/G/T 可、省略時は MiB 扱い、1024 ベース）。`c' = ⌈(m/M)×C⌉ > c` なら c' を実効コア数に使用 | `c × m_default`（m_default はコアあたりのデフォルトメモリ MiB/コア） | Slurm パーティション設定 `MaxMemPerNode` に委任 |
| `g` | ジョブあたり GPU 数（サブシステム C） | `1 ≤ g ≤ G × N_C` | — | — |

### 2-3. spec 8-4 との関係

spec 8-4 は「`--rsc` 指定があった場合、その内容に基づいて項目 2-1(3)〜(5) および 2-2(1) の
ジョブ属性を**自動設定**すること」を要求する。  
つまり `--rsc` は**変換層**であり、最終的には Slurm ネイティブのリソース指定に変換して投入される。

---

## 3. 実装アーキテクチャ

```
ユーザ: sbatch --rsc p=4:t=2:c=4:m=8G job.sh  (node_cpus=48, node_mem=196608MiB)

[クライアント側（ログインノード）]
  ┌─ slurm_spank_init()
  │    spank_rsc: _get_plugin_arg(av, "config") → "/etc/slurm/rsc_defaults.conf"
  │      setenv("_SLURM_RSC_CONFIG", cfg_path, 1)  ← bridge env var ①
  │
  ├─ getopt ループ
  │    spank_rsc: _rsc_opt_cb()
  │      xstrdup(optarg) → rsc_optarg
  │      setenv("_SLURM_RSC_SPEC", optarg, 1)      ← bridge env var ②
  │
  ├─ cli_filter_g_pre_submit(opt)               ← getopt 完了後、job_desc 生成前
  │    cli_filter_rsc: pre_submit(opt)
  │      cfg_path = getenv("_SLURM_RSC_CONFIG")
  │      spec     = getenv("_SLURM_RSC_SPEC")
  │      rsc_load_config(cfg_path, cluster, &cfg)
  │        → cfg.node_cpus=48, cfg.node_mem_mib=196608
  │      rsc_parse_request(spec, &cfg, &req)
  │        → req.p=4, req.t=2, req.c=4, req.m_mib=8192
  │      競合チェック（forbidden_with_rsc）
  │        deny オプションが isset → slurm_error() → SLURM_ERROR を返す
  │        warn オプションが isset → slurm_info() → 継続
  │      c_eff = rsc_compute_effective_cores(4, 8192, 48, 196608)  → 4
  │      n_tpn = 48 / 4 = 12  (ノードあたりプロセス数)
  │      n_needed = ceil(4/12) = 1
  │      slurm_option_set(opt, "ntasks",          "4",  false)
  │      slurm_option_set(opt, "cpus-per-task",   "4",  false)  ← c_eff
  │      slurm_option_set(opt, "ntasks-per-node", "12", false)
  │      slurm_option_set(opt, "nodes",           "1",  false)
  │      unsetenv("_SLURM_RSC_SPEC"), unsetenv("_SLURM_RSC_CONFIG")
  │
  ├─ spank_init_post_opt()
  │    setenv("SLURM_RSC_P/T/C/M", ...)
  │    setenv("OMP_NUM_THREADS", t_str, 0)               ← salloc 子プロセス用
  │
  └─ slurm_opt_create_job_desc()                ← ntasks_set=true → JOB_NTASKS_SET bit
       env_array_for_job()                      ← SLURM_NTASKS=4, TASKS_PER_NODE=4 ✓
             |
             |  RPC: job_submit_request
             v
[サーバ側（slurmctld）]
  [Slurm スケジューラ: 通常の資源割当・ジョブ実行]
             |
             v
[REMOTE（slurmstepd）]
  slurm_spank_user_init()
    SLURM_CPUS_PER_TASK → SLURM_RSC_C_EFF に転写  ← OMP_* と同パターン
    SLURM_RSC_T, SLURM_RSC_C から OMP_* をセット
```

### 実行順序（salloc.c の行番号付き）

| ステップ | ソース位置 | 説明 |
|---|---|---|
| `slurm_spank_init()` | — | config パスを `_SLURM_RSC_CONFIG` にセット |
| `cli_filter_g_setup_defaults()` | salloc/opt.c:123 | --rsc 未解析 |
| `_opt_args()` getopt ループ | salloc/opt.c:133 | `_rsc_opt_cb()` が spec を `_SLURM_RSC_SPEC` にセット |
| `cli_filter_g_pre_submit(opt)` | salloc/opt.c:321 | 競合チェック・conf 読込・c_eff 計算・全オプション設定 |
| `spank_init_post_opt()` | salloc.c:232 | SLURM_RSC_* をセット（setenv）・OMP_* |
| `slurm_opt_create_job_desc()` | salloc.c:251 | ntasks_set=true → JOB_NTASKS_SET bit |
| `env_array_for_job()` | salloc.c 以降 | SLURM_NTASKS=p, TASKS_PER_NODE=p/node ✓ |
| RPC → slurmctld | — | job_submit_rsc なし（v2.2 で廃止） |

---

## 4. プラグイン構成

### 4-1. ファイル一覧

| プラグイン | ソースファイル | plugin_type | 実行コンテキスト |
|---|---|---|---|
| spank_rsc | `src/plugins/cli_filter/rsc/spank_rsc.c` | —（SPANK） | LOCAL / ALLOCATOR / REMOTE |
| cli_filter_rsc | `src/plugins/cli_filter/rsc/cli_filter_rsc.c` | `cli_filter/rsc` | クライアント側（salloc/sbatch/srun） |

共有コード: `src/plugins/cli_filter/rsc/rsc_common.c` / `rsc_common.h`
（spank_rsc・cli_filter_rsc が同ディレクトリで共有。両プラグインが rsc_common.c をソース直接取り込みでリンクする）

### 4-2. デプロイ設定

```ini
# /etc/slurm/plugstack.conf
required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf

# /etc/slurm/slurm.conf
CliFilterPlugins=rsc
# JobSubmitPlugins は不要（v2.2 で廃止）
```

### 4-3. ビルド

```bash
# 全プラグインをビルド
cd slurm-25.11.4
make -j"$(nproc)" src/plugins/cli_filter/rsc/
# cli_filter/rsc/ に cli_filter_rsc.so と spank_rsc.so の両方がビルドされる
```

---

## 5. SPANK プラグイン実装詳細（spank_rsc）

### 5-1. 主要 API

**ヘッダ**: `slurm/spank.h`  
**参考実装**: `src/plugins/job_submit/pbs/spank_pbs.c`

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

`spank_option_register()` は **`slurm_spank_init()` からのみ有効**。
同時に `av[]`（plugstack.conf の引数）から config パスを取得して bridge env var をセットする。
これにより cli_filter が設定ファイルのパスを知ることができる。

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

getopt 処理中（`init_post_opt` の前）に呼ばれる。
raw 文字列をスタティック変数に退避し、同時に cli_filter_rsc 向けの bridge env var をセットする。

```c
static char *rsc_optarg;  /* モジュールスタティック */

static int _rsc_opt_cb(int val, const char *optarg, int remote)
{
    (void) val;
    (void) remote;
    xfree(rsc_optarg);
    rsc_optarg = xstrdup(optarg);
    /* Bridge for cli_filter_rsc: expose raw spec before pre_submit runs */
    setenv("_SLURM_RSC_SPEC", optarg, 1);
    return ESPANK_SUCCESS;
}
```

`remote=1`（slurmstepd 側）でも呼ばれるが、`init_post_opt` が REMOTE context で早期 return するため問題ない。

bridge env var が 2 本になる:
- `_SLURM_RSC_CONFIG`: config ファイルパス（`slurm_spank_init` でセット。`av[]` から取得可能）
- `_SLURM_RSC_SPEC`: raw spec 文字列（`_rsc_opt_cb` でセット）

`cli_filter_g_pre_submit(opt)` は getopt ループ完了後に実行されるため、
両方の env var が確実にセット済みの状態で cli_filter から読み取ることができる。
`spank_option_getopt()` は `SPANK_INIT_POST_OPT` フェーズで明示的にブロックされているため使用不可。

### 5-4. `slurm_spank_init_post_opt()` での処理

コールバックで退避した `rsc_optarg` を解析・検証し、job control 環境変数にセットする。

**ntasks/cpus-per-task の設定は cli_filter_rsc が担当**（v1 との主要変更点）。
spank_rsc は以下の処理に専念する:
- LOCAL/ALLOCATOR コンテキストで `setenv("SLURM_RSC_*", ...)` をセット
- LOCAL/ALLOCATOR コンテキストで `setenv("OMP_NUM_THREADS", ...)` をセット（salloc 子プロセス向け）

**環境変数の伝搬フロー**

```
[init_post_opt: LOCAL/ALLOCATOR]
  setenv("SLURM_RSC_SPEC", spec, 1)     ← CPU/GPU 共通（情報提供用）
  CPU モード:
    setenv("SLURM_RSC_P", p_str, 1)
    setenv("SLURM_RSC_T", t_str, 1)
    setenv("SLURM_RSC_C", c_str, 1)
    setenv("SLURM_RSC_M", m_str, 1)
  GPU モード:
    setenv("SLURM_RSC_G", g_str, 1)
  setenv("OMP_NUM_THREADS", t_str, 0)   ← salloc 子プロセス向け（既設定なら skip）
        |
        | sbatch: --export=ALL（デフォルト）でジョブ環境に伝搬
        v
[REMOTE (slurmstepd): slurm_spank_user_init]
  spank_getenv(sp, "SLURM_RSC_T", ...)  ← OMP_* 計算に使用
  spank_getenv(sp, "SLURM_RSC_C", ...)
  spank_getenv(sp, "SLURM_CPUS_PER_TASK", ...) → spank_setenv("SLURM_RSC_C_EFF", ...)
  OMP_NUM_THREADS / OMP_PROC_BIND / OMP_PLACES をセット
        |
        v
  [ユーザジョブが参照する変数: SLURM_RSC_* / OMP_*]
```

API コンテキスト制約:

| API | 有効コンテキスト | 無効時エラー |
|---|---|---|
| `spank_setenv()` / `spank_getenv()` | REMOTE のみ | `ESPANK_NOT_REMOTE` |

### 5-5. コンテキスト別動作まとめ

| コンテキスト | 場所 | 動作 |
|---|---|---|
| `S_CTX_LOCAL` | srun | `--rsc` を解析、`setenv("SLURM_RSC_*", ...)` でジョブ環境変数をセット |
| `S_CTX_ALLOCATOR` | sbatch / salloc | 同上 |
| `S_CTX_REMOTE` | slurmstepd | `init_post_opt` は即 return。`user_init` が `SLURM_RSC_C_EFF` と `OMP_*` をジョブ実行環境にセット |

### 5-6. ユーザジョブへの環境変数伝搬

**① `init_post_opt` が `setenv()` でセット（`SLURM_RSC_*`）**  
`--export=ALL`（sbatch/srun のデフォルト）によりクライアントプロセスの環境変数が
ジョブ環境に伝搬する。`spank_job_control_setenv` は使用しない。

**② `slurm_spank_user_init()` が追加（`SLURM_RSC_C_EFF` と `OMP_*`）**

| 変数名 | 内容 | セット元 |
|---|---|---|
| `SLURM_RSC_SPEC` | `--rsc` に渡した未加工の文字列 | init_post_opt（setenv） |
| `SLURM_RSC_P` | プロセス数 | init_post_opt（setenv） |
| `SLURM_RSC_T` | プロセスあたりスレッド数 | init_post_opt（setenv） |
| `SLURM_RSC_C` | 指定コア数（c_eff 適用前） | init_post_opt（setenv） |
| `SLURM_RSC_M` | プロセスあたりメモリ (MiB) | init_post_opt（setenv） |
| `SLURM_RSC_G` | GPU 数（GPU モード時のみ） | init_post_opt（setenv） |
| `SLURM_RSC_C_EFF` | 実効コア数（c_eff 適用後） | user_init（`SLURM_CPUS_PER_TASK` を転写） |
| `OMP_NUM_THREADS` | スレッド数 (= t) | user_init（未設定時のみ） |
| `OMP_PROC_BIND` | スレッドバインドポリシー | user_init（c=t→`close` / c\<t→`close` / c>t→`spread`） |
| `OMP_PLACES` | スレッド配置単位 | user_init（c=t→`cores` / c\<t→`threads` / c>t→`cores`） |

GPU モードの場合は `SLURM_RSC_SPEC` / `SLURM_RSC_G` のみがセットされ、
`SLURM_RSC_P/T/C/M`、`SLURM_RSC_C_EFF` および `OMP_*` はセットされない。

### 5-7. `slurm_spank_user_init()` での RSC_C_EFF セット

REMOTE コンテキストで実行される。`SLURM_CPUS_PER_TASK` は Slurm がジョブ環境に自動セット
（cli_filter が設定した `cpus_per_task` から生成）するため、それを転写するだけでよい:

```c
int slurm_spank_user_init(spank_t sp, int ac, char **av)
{
    char buf[16];

    /* SLURM_RSC_C_EFF: SLURM_CPUS_PER_TASK を転写（OMP_NUM_THREADS と同パターン） */
    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", buf, sizeof(buf)) == ESPANK_SUCCESS)
        spank_setenv(sp, "SLURM_RSC_C_EFF", buf, 1);

    /* OMP_* の設定: SLURM_RSC_T / SLURM_RSC_C を参照 */
    char t_buf[16], c_buf[16];
    spank_getenv(sp, "SLURM_RSC_T", t_buf, sizeof(t_buf));
    spank_getenv(sp, "SLURM_RSC_C", c_buf, sizeof(c_buf));
    /* OMP_NUM_THREADS = t_buf, OMP_PROC_BIND / OMP_PLACES は c vs t で決定 */
    /* ... */
    return ESPANK_SUCCESS;
}
```

### 5-8. `slurm_spank_fini()`

```c
void slurm_spank_fini(spank_t sp, int ac, char **av)
{
    (void) sp; (void) ac; (void) av;
    xfree(rsc_optarg);
    unsetenv("_SLURM_RSC_SPEC");    /* bridge env var の後片付け */
    unsetenv("_SLURM_RSC_CONFIG");  /* bridge env var の後片付け */
}
```

---

## 6. cli_filter プラグイン実装詳細（cli_filter_rsc）

### 6-1. 位置付けと解決する問題

**問題①**: `--rsc p=2:t=2:c=2:m=1G` で `salloc` を実行すると、子プロセスに渡る
`SLURM_TASKS_PER_NODE` が正しくなかった（ノードの全 CPU 数になっていた）。

**根本原因**（`src/common/env.c` の挙動）:
- `desc->num_tasks == NO_VAL` のとき `env_array_for_job()` のフォールバック（env.c:1087-1107）が
  `alloc->cpus_per_node[0]`（ノードの全 CPU 数）を `SLURM_TASKS_PER_NODE` として書き出す
- `JOB_NTASKS_SET` ビット（env.c:1186）は `opt->ntasks_set == true` のときのみセットされる
- サーバ側 `job_submit_rsc` が `num_tasks` を変更しても salloc のクライアント側 `desc` には反映されない

**問題②（v2.0 まで）**: c_eff 計算のためにノード情報（C/M）が必要だったが、
クライアント側では slurmctld 内部 API（`node_record_t.cpus_efctv` 等）が使えないため
job_submit（サーバ側）で計算していた。

**解決**: `rsc_defaults.conf` に `node_cpus` / `node_mem` / `node_gpus` を定義し、
cli_filter が設定ファイルを読んで c_eff を計算・設定する。
すべての Slurm オプション設定が投入前に完結し、エラーも即座にフィードバックされる。

### 6-2. bridge env var の仕組み

cli_filter の `pre_submit()` は `--rsc` の値と config パスに直接アクセスできないため、
spank_rsc がプロセス環境変数に橋渡しする（2 本の bridge env var）:

```
slurm_spank_init(av=[..., "config=/etc/slurm/rsc_defaults.conf", ...])
  → setenv("_SLURM_RSC_CONFIG", "/etc/slurm/rsc_defaults.conf", 1)  ← ①

getopt ループ → _rsc_opt_cb(optarg="p=4:t=2:c=4:m=8G")
  → setenv("_SLURM_RSC_SPEC", "p=4:t=2:c=4:m=8G", 1)               ← ②

cli_filter_g_pre_submit(opt)                                          ← 両方セット済み
  → pre_submit(opt)
      rsc_load_config(getenv("_SLURM_RSC_CONFIG"), cluster, &cfg)
        → cfg.node_cpus=48, cfg.node_mem_mib=196608
      rsc_parse_request(getenv("_SLURM_RSC_SPEC"), &cfg, &req)
        → req.p=4, req.c=4, req.m_mib=8192
      競合チェック（forbidden_with_rsc）
        deny オプションが isset → SLURM_ERROR を返す（ジョブ拒否）
        warn オプションが isset → slurm_info() → 継続
      c_eff = rsc_compute_effective_cores(4, 8192, 48, 196608) → 4
      slurm_option_set(opt, "ntasks",          "4",  false)
      slurm_option_set(opt, "cpus-per-task",   "4",  false)  ← c_eff
      slurm_option_set(opt, "ntasks-per-node", "12", false)  ← floor(48/4)
      slurm_option_set(opt, "nodes",           "1",  false)  ← ceil(4/12)
      unsetenv("_SLURM_RSC_SPEC"), unsetenv("_SLURM_RSC_CONFIG")

slurm_opt_create_job_desc()  ← ntasks_set=true → JOB_NTASKS_SET bit セット
env_array_for_job()          ← SLURM_NTASKS=4, TASKS_PER_NODE=4 ✓
```

### 6-3. `cli_filter_p_pre_submit()` 関数

Slurm の cli_filter プラグインは `cli_filter_p_*` プレフィックスで関数を公開する
（`src/interfaces/cli_filter.c` の `syms[]` 参照）。

```c
extern int cli_filter_p_pre_submit(slurm_opt_t *opt, int offset)
{
    const char *cfg_path = getenv("_SLURM_RSC_CONFIG");
    const char *spec     = getenv("_SLURM_RSC_SPEC");
    const char *cluster  = getenv("SLURM_CLUSTER_NAME");
    rsc_config_t cfg;
    rsc_request_t req;
    char err[256], buf[64];

    if (!cfg_path || !spec)
        return SLURM_SUCCESS;  /* --rsc 未使用 → 何もしない */

    if (rsc_load_config(cfg_path, cluster, &cfg, err, sizeof(err)) < 0) {
        /* エラーは spank_init_post_opt でも報告されるため info に留める */
        info("rsc cli_filter: %s", err);
        return SLURM_SUCCESS;
    }

    if (rsc_parse_request(spec, &cfg, &req, err, sizeof(err)) < 0) {
        info("rsc cli_filter: %s", err);
        rsc_config_free(&cfg);
        return SLURM_SUCCESS;
    }

    /* 競合チェック: --rsc 明示指定時に policy エントリを確認。
     * SLURM_JOB_ID がセット済み ≡ 割当済みジョブ内の srun ステップ。
     * この場合はリソースが既に確保されており他ジョブへの影響がないためスキップする。 */
    if (!getenv("SLURM_JOB_ID")) {
        for (int i = 0; i < cfg.entry_count; i++) {
            rsc_policy_entry_t *e = &cfg.entries[i];
            if (!slurm_option_isset(opt, e->key))
                continue;
            if (e->policy == RSC_POLICY_DENY) {
                error("rsc: option --%s conflicts with --rsc", e->key);
                rsc_config_free(&cfg);
                return SLURM_ERROR;
            }
            info("rsc: warning: --%s specified with --rsc, "
                 "--rsc takes precedence", e->key);
        }
    }

    if (req.mode == RSC_MODE_CPU) {
        uint32_t c_eff = (cfg.node_cpus && cfg.node_mem_mib)
            ? rsc_compute_effective_cores(req.c, req.m_mib,
                                          cfg.node_cpus, cfg.node_mem_mib)
            : req.c;  /* node_cpus/node_mem 未設定時はユーザ指定値をそのまま使用 */

        if (!slurm_option_isset(opt, "ntasks")) {
            snprintf(buf, sizeof(buf), "%u", req.p);
            slurm_option_set(opt, "ntasks", buf, false);
        }
        if (!slurm_option_isset(opt, "cpus-per-task")) {
            snprintf(buf, sizeof(buf), "%u", c_eff);
            slurm_option_set(opt, "cpus-per-task", buf, false);
        }

        /* threads-per-core: t と c が異なる場合のみ設定。
         *   c < t (SMT):         threads_per_core = t / c  (例: t=2, c=1 → 2)
         *   c > t (コア間引き):  threads_per_core = 1      (SMT 無効)
         *   c == t: 変更不要 */
        if (req.t > 0 && req.c > 0 && req.t != req.c &&
            !slurm_option_isset(opt, "threads-per-core")) {
            uint32_t tpc = (req.c < req.t) ? req.t / req.c : 1;
            snprintf(buf, sizeof(buf), "%u", tpc);
            slurm_option_set(opt, "threads-per-core", buf, false);
        }

        /* mem-per-cpu: ceil(m_mib / c_eff) MiB。
         * node_mem_mib が設定されている場合のみ設定する
         * （未設定時は c_eff がメモリ制約を暗黙的に反映済み）。 */
        if (cfg.node_mem_mib && req.m_mib > 0 && c_eff > 0 &&
            !slurm_option_isset(opt, "mem-per-cpu")) {
            uint64_t mem_cpu = rsc_div_ceil_u64(req.m_mib, c_eff);
            snprintf(buf, sizeof(buf), "%" PRIu64, mem_cpu);
            slurm_option_set(opt, "mem-per-cpu", buf, false);
        }

        if (cfg.node_cpus && c_eff) {
            if (req.p * c_eff >= cfg.node_cpus) {
                /* ノード専有モード */
                uint32_t n_tpn  = cfg.node_cpus / c_eff;
                uint32_t n_full = req.p / n_tpn;
                uint32_t p_r    = req.p - n_full * n_tpn;  /* 残りプロセス数 */
                uint32_t n_needed;

                if (!slurm_option_isset(opt, "ntasks-per-node")) {
                    snprintf(buf, sizeof(buf), "%u", n_tpn);
                    slurm_option_set(opt, "ntasks-per-node", buf, false);
                }

                if (p_r == 0) {
                    n_needed = n_full;  /* 端数なし: フルノードのみ */
                } else if (cfg.node_sockets > 0) {
                    uint32_t c_d = cfg.node_cpus / cfg.node_sockets;  /* C_D */
                    if (p_r * c_eff <= c_d) {
                        /* D分ノードケース: 部分プロセスが 1 ソケットに収まる。
                         * het-job の第 2 コンポーネントとして部分ノードを追加する
                         * (Section 12-3 参照)。 */
                        job_desc_msg_t *partial = xmalloc(sizeof(*partial));
                        slurm_init_job_desc_msg(partial);
                        partial->num_tasks     = p_r;
                        partial->min_nodes     = 1;
                        partial->max_nodes     = 1;
                        partial->cpus_per_task = (uint16_t) c_eff;
                        partial->tres_per_node = xstrdup("gres/numa:1");
                        partial->bitflags     |= GRES_ENFORCE_BIND;
                        cli_filter_het_append(partial);

                        /* プライマリコンポーネント: フルノードのみ */
                        snprintf(buf, sizeof(buf), "%u", n_full * n_tpn);
                        slurm_option_set(opt, "ntasks", buf, true);
                        snprintf(buf, sizeof(buf), "%u", n_full);
                        slurm_option_set(opt, "nodes", buf, true);
                        snprintf(buf, sizeof(buf), "gres/numa:%u",
                                 cfg.node_sockets);
                        slurm_option_set(opt, "tres-per-node", buf, false);
                        info("rsc: p_r=%u → D分ノード het "
                             "(p_r*c_eff=%u <= C_D=%u)",
                             p_r, p_r * c_eff, c_d);
                        n_needed = n_full;
                    } else {
                        info("rsc: p_r=%u → フルノード追加 "
                             "(p_r*c_eff=%u > C_D=%u)",
                             p_r, p_r * c_eff, c_d);
                        n_needed = n_full + 1;
                    }
                } else {
                    n_needed = n_full + 1;  /* node_sockets 未設定: 保守的にフルノード */
                }

                if (n_needed > 0 &&
                    !slurm_option_isset(opt, "nodes") &&
                    cfg.node_count_mode != RSC_NODE_COUNT_NONE) {
                    if (cfg.node_count_mode == RSC_NODE_COUNT_EXACT)
                        snprintf(buf, sizeof(buf), "%u", n_needed);
                    else  /* RSC_NODE_COUNT_MIN */
                        snprintf(buf, sizeof(buf), "%u-", n_needed);
                    slurm_option_set(opt, "nodes", buf, false);
                }
            } else {
                /* コア指定モード: 1ノードに全プロセス */
                if (!slurm_option_isset(opt, "nodes"))
                    slurm_option_set(opt, "nodes", "1", false);
            }
        }
    } else {  /* RSC_MODE_GPU */
        if (!slurm_option_isset(opt, "ntasks")) {
            snprintf(buf, sizeof(buf), "%u", req.g);
            slurm_option_set(opt, "ntasks", buf, false);
        }
        if (!slurm_option_isset(opt, "gpus-per-task"))
            slurm_option_set(opt, "gpus-per-task", "1", false);
        if (cfg.node_cpus && cfg.node_gpus &&
            !slurm_option_isset(opt, "cpus-per-task")) {
            snprintf(buf, sizeof(buf), "%u", cfg.node_cpus / cfg.node_gpus);
            slurm_option_set(opt, "cpus-per-task", buf, false);
        }
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

`rsc_div_ceil_u64()` は `rsc_common.h` で宣言・`rsc_common.c` で定義済み（Makefile.am でリンク済み）。
`PRIu64` のために `#include <inttypes.h>` が必要（実装ファイルに追加済み）。

### 6-4. rsc_common.c のリンク

cli_filter_rsc は `rsc_load_config()` / `rsc_parse_request()` /
`rsc_compute_effective_cores()` を使用するため、`rsc_common.c` を SOURCES に追加する:

```makefile
# src/plugins/cli_filter/rsc/Makefile.am
pkglib_LTLIBRARIES = cli_filter_rsc.la spank_rsc.la

cli_filter_rsc_la_SOURCES = cli_filter_rsc.c rsc_common.c
cli_filter_rsc_la_LDFLAGS = $(SO_LDFLAGS) $(PLUGIN_FLAGS)

spank_rsc_la_SOURCES = spank_rsc.c rsc_common.c
spank_rsc_la_LDFLAGS = $(PLUGIN_FLAGS)
```

### 6-5. エラーハンドリング方針

| エラー種別 | 返り値 | 理由 |
|---|---|---|
| 競合 deny | `SLURM_ERROR` | 即座にジョブ拒否（投入前にユーザへフィードバック） |
| 競合 warn | `SLURM_SUCCESS` | 警告のみ、ジョブは通す |
| config 読み込み失敗 | `SLURM_SUCCESS` + `slurm_info()` | `spank_init_post_opt()` でも同エラーを報告 |
| spec パース失敗 | `SLURM_SUCCESS` + `slurm_info()` | 同上 |

---

## 7. デフォルト値・最大値設定ファイル

### 7-1. ファイル配置と指定方法

設定ファイルのパスは `plugstack.conf` のプラグイン引数 `config=` で指定する。

```
# /etc/slurm/plugstack.conf
required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf
```

### 7-2. 設定ファイルフォーマット

INI スタイル。`[default]` セクションにクラスタ共通の設定を記述し、
クラスタ別に上書きしたい場合は `[cluster:CLUSTER_NAME]` セクションを追加する。

```ini
# /etc/slurm/rsc_defaults.conf

[default]
m_default = 4096    # コアあたりのデフォルトメモリ (MiB/コア)
                    # m 省略時は c × m_default を使用
node_count_mode = exact
# node_count_mode:
#   exact: min_nodes = max_nodes = n_needed（ノード数を厳密に固定）
#   min:   min_nodes = n_needed のみ設定（上限フリー）
#   none:  設定しない（Slurm デフォルト挙動）
node_cpus    = 48      # ノードあたり実効コア数（CoreSpec 除外後）
node_mem     = 196608  # ノードあたり利用可能メモリ MiB（MemSpec 除外後）
node_gpus    = 0       # GPU モード用: ノードあたり GPU 数（0 = GPU なし）
node_sockets = 0       # D: ノードあたりのソケット数（0 = D分ノード判定を行わない）

[cluster:clusterA]
m_default = 8192
node_count_mode = exact
node_cpus    = 64
node_mem     = 262144
node_gpus    = 0
node_sockets = 2

[cluster:gpuCluster]
m_default = 8192
node_count_mode = exact
node_cpus    = 64
node_mem     = 262144
node_gpus    = 8
node_sockets = 0

[cluster:clusterB]
m_default = 2048
node_count_mode = min
node_cpus    = 48
node_mem     = 196608

[forbidden_with_rsc]
# --rsc と競合する Slurm 標準オプションの扱いを定義する
# 値: deny (エラーで拒否) または warn (警告のみ、ジョブは通す)
# リストにないオプションは常に許可
# 注意: 割当済みジョブ内の srun ステップ（SLURM_JOB_ID がセット済み）では
#       このチェックをスキップする（リソース確保済みで他ジョブへの影響なし）

# --- グループ A: 資源量の直接競合 (deny 推奨) ---
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

# --- グループ B: トポロジ・配置制約 (warn 推奨) ---
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

# --- グループ C: ノード選択制約 (warn 推奨) ---
constraint         = warn
cluster-constraint = warn
contiguous         = warn
exclude            = warn
nodelist           = warn
nodefile           = warn
switches           = warn
user-min-nodes     = warn

# --- グループ D: 運用ルール上の制御対象 (warn 推奨) ---
batch      = warn
clusters   = warn
qos        = warn
gpu-bind   = warn
mem-bind   = warn
gres-flags = warn

# --rsc 未指定時 (暗黙デフォルト適用時) にも上記チェックを行うか
# strict: 行う / relaxed: 行わない (--rsc 明示指定時のみチェック)
implicit_check = relaxed
```

#### ノード情報フィールドの意味と省略時の扱い

| フィールド | 意味 | 省略・0 の場合 |
|---|---|---|
| `node_cpus` | CoreSpec 除外後の実効コア数（= `node_ptr->cpus_efctv`） | c_eff 計算をスキップ（ユーザ指定の `c` をそのまま使用） |
| `node_mem` | MemSpec 除外後の利用可能メモリ MiB（= `real_memory - mem_spec_limit`） | c_eff のメモリ換算をスキップ |
| `node_gpus` | GPU モード時の `cpus_per_task = node_cpus / node_gpus` 計算に使用 | cpus-per-task の自動設定をスキップ |

`node_cpus` と `node_mem` はいずれか一方でも未設定の場合、c_eff 計算全体をスキップする。  
実際のノードスペックは `sinfo -o "%n %c %m"` や `scontrol show node` で確認する。  
CoreSpec 使用環境では `node_cpus = 物理コア数 - CoreSpec コア数` を設定する。

### 7-3. 設定読み込み方針

1. `SLURM_CLUSTER_NAME` 環境変数を読み取り、`[cluster:CLUSTER_NAME]` セクションを検索
2. 一致するセクションがない場合は `[default]` セクションにフォールバック
3. `[default]` も存在しない場合はエラーとしてジョブを拒否

`[forbidden_with_rsc]` セクションは v2.2 から **cli_filter_rsc が読み込む**。
slurmctld（job_submit）は使用しない。

### 7-4. エラー挙動まとめ

| 状況 | エラーメッセージ例 | 結果 |
|---|---|---|
| 設定ファイルが存在しない | `rsc: cannot open config file: /etc/slurm/rsc_defaults.conf` | ジョブ拒否 |
| クラスタセクションも `[default]` も存在しない | `rsc: no [cluster:NAME] or [default] section in config file` | ジョブ拒否 |
| t が c × 4 を超過 | `rsc: t=N exceeds 4x c (c=M)` | ジョブ拒否 |
| `deny` 設定オプションとの競合 | `rsc: option --ntasks conflicts with --rsc` | ジョブ拒否 |
| `warn` 設定オプションとの競合 | `rsc: warning: --nodes specified with --rsc` | 警告のみ |

---

## 8. job_submit プラグイン（v2.2 で廃止）

v2.2 では `job_submit_rsc.so` は不要となった。すべての処理がクライアント側に移動した:

| 処理 | v2.2 の担当 |
|---|---|
| 資源設定（ntasks / cpus-per-task / nodes 等） | cli_filter_rsc |
| 競合オプション検出（deny/warn） | cli_filter_rsc（投入前に即座にエラー） |
| GPU gres 設定 | `--gpus-per-task=1` を Slurm が `tres_per_task` に自動変換 |
| RSC_C_EFF | spank `user_init()` が `SLURM_CPUS_PER_TASK` から転写 |

`JobSubmitPlugins=rsc` の設定も不要。v2.1 以前の実装詳細は git log を参照。

### 8-1. Slurm パラメータマッピング（最終）

| `--rsc` パラメータ | `job_desc_msg_t` フィールド / 環境変数 | 設定主体 |
|---|---|---|
| `p` | `num_tasks` | cli_filter_rsc |
| `c`（または `c_eff`） | `cpus_per_task` | cli_filter_rsc |
| `floor(C/c_eff)` | `ntasks_per_node` | cli_filter_rsc（ノード専有モード時） |
| `ceil(p/n_tpn)` | `min_nodes` / `max_nodes` | cli_filter_rsc |
| `t` | `OMP_NUM_THREADS` | spank_rsc（init_post_opt: salloc 子プロセス向け setenv / user_init: ジョブ実行環境向け spank_setenv） |
| `g` | `gpus-per-task=1` → Slurm が `tres_per_task="gres/gpu:1"` に変換 | cli_filter_rsc |
| c_eff | `SLURM_RSC_C_EFF` | spank_rsc（user_init で SLURM_CPUS_PER_TASK を転写） |

---

## 9. `--rsc` 解析・判定フローチャート

```
START
  |
  v
[クライアント: getopt ループ]
  |
  +-- --rsc <spec> が指定された?
       YES: _rsc_opt_cb(optarg=spec)
              rsc_optarg = xstrdup(spec)
              setenv("_SLURM_RSC_SPEC", spec, 1)  ← bridge env var

  v
[クライアント: cli_filter_g_pre_submit(opt)]
  |
  +-- getenv("_SLURM_RSC_CONFIG") / getenv("_SLURM_RSC_SPEC") → どちらか NULL?
       YES: 何もしない → 次へ
       NO:  rsc_load_config(cfg_path, cluster, &cfg)
              → cfg.node_cpus=48, cfg.node_mem_mib=196608, cfg.node_gpus=0
            rsc_parse_request(spec, &cfg, &req)
              → req.p=4, req.t=2, req.c=4, req.m_mib=8192, req.mode=RSC_MODE_CPU

            競合チェック（forbidden_with_rsc）
              deny → SLURM_ERROR（ジョブ拒否）
              warn → slurm_info() → 継続

            [CPU モード]
              c_eff = (cfg.node_cpus && cfg.node_mem_mib)
                      ? rsc_compute_effective_cores(req.c, req.m_mib,
                                                    cfg.node_cpus, cfg.node_mem_mib)
                      : req.c        ← node_cpus/node_mem 未設定時はそのまま
              slurm_option_set(opt, "ntasks",         p_str,   false)
              slurm_option_set(opt, "cpus-per-task",  c_eff_str, false)
              p × c_eff ≥ node_cpus?  (ノード専有モード)
                YES: n_tpn  = node_cpus / c_eff
                     n_full = p / n_tpn
                     p_r    = p mod n_tpn          ← 残りプロセス数
                     p_r == 0?
                       YES: n_needed = n_full       ← 端数なし
                       NO (node_sockets > 0):
                            C_D = node_cpus / node_sockets
                            p_r × c_eff ≤ C_D?
                              YES: n_needed = n_full + 1  ← D分ノード（1ソケット内に収まる）
                              NO:  n_needed = n_full + 1  ← フルノード追加
                       NO (node_sockets == 0):
                            n_needed = n_full + 1         ← 保守的フルノード追加
                     slurm_option_set(opt, "ntasks-per-node", n_tpn_str, false)
                     node_count_mode=exact → slurm_option_set(opt, "nodes", n_needed_str, false)
                     node_count_mode=min   → slurm_option_set(opt, "nodes", "n_needed-", false)
                NO:  slurm_option_set(opt, "nodes", "1", false)

            [GPU モード]
              slurm_option_set(opt, "ntasks",        g_str, false)
              slurm_option_set(opt, "gpus-per-task", "1",   false)
              cfg.node_gpus > 0?
                YES: slurm_option_set(opt, "cpus-per-task",
                                      str(node_cpus/node_gpus), false)

            unsetenv("_SLURM_RSC_SPEC"), unsetenv("_SLURM_RSC_CONFIG")

  v
[クライアント: spank_init_post_opt()]
  |
  +-- LOCAL/ALLOCATOR context?
       NO  → return 0 (REMOTE は何もしない)
       YES → 設定ファイル読み込み (config= 引数)
               SLURM_CLUSTER_NAME → [cluster:NAME] or [default] セクション取得
             rsc_parse_request(rsc_optarg, &cfg, &req) → req 構造体
               GPU モード (g=N)?
                 → setenv("SLURM_RSC_G", g)
               CPU モード?
                 → デフォルト補完: p/t/c=1, m=c×m_default
                 → バリデーション: t > c×4 → エラー
                 → setenv("SLURM_RSC_P/T/C/M", ...)
             setenv("SLURM_RSC_SPEC", spec)
             OMP_* をセット (salloc 子プロセス向け)

  v
[クライアント: slurm_opt_create_job_desc()]
  |
  opt->ntasks_set = true → JOB_NTASKS_SET bit セット
  opt->cpus_set   = true → JOB_CPUS_SET bit セット

  v
[クライアント: env_array_for_job()]
  |
  SLURM_NTASKS = p  ✓
  SLURM_TASKS_PER_NODE = p/node  ✓ （フォールバック不使用）
  SLURM_CPUS_PER_TASK = c_eff  ✓ （cli_filter が設定済み）

  v
[RPC: job_submit_request → slurmctld]
  |
  v
[Slurm スケジューラ: 通常の資源割当]  ← job_submit_rsc なし（v2.2 で廃止）

  v
[REMOTE (slurmstepd): slurm_spank_user_init()]
  SLURM_CPUS_PER_TASK → SLURM_RSC_C_EFF に転写  ← OMP_* と同パターン
  SLURM_RSC_T, SLURM_RSC_C → OMP_* をセット

END
```

---

## 10. 実装上の注意点・制約

### 10-1. ノード情報の管理方針（conf 定義）

現在の設計（v2.2）では `node_cpus` / `node_mem` / `node_gpus` を `rsc_defaults.conf` に手動で定義し、
cli_filter がこれを読んで c_eff を計算する。

**設定値の確認方法**:

```bash
# 実効コア数（CoreSpec を除外後）
sinfo -o "%n %c %m" -N | head -5
scontrol show node nodename | grep -E "CfgTRES|CPUTot|RealMemory|MemSpecLimit"

# CoreSpec 使用環境:
#   node_cpus = CfgTRES の cpu 値（= cpus_efctv）
# CoreSpec 未使用環境:
#   node_cpus = 物理コア数（CPUTot または SLURM_CPUS）

# MemSpec 使用環境:
#   node_mem = RealMemory - MemSpecLimit
# 未使用:
#   node_mem = RealMemory
```

**設計上のトレードオフ**:

| 観点 | 旧設計（job_submit で計算） | v2.1（conf + cli_filter） |
|---|---|---|
| ノード情報の正確性 | 常に最新（slurmctld 内部 API） | conf 更新が必要（運用コスト） |
| エラー通知タイミング | 投入後（slurmctld が応答してから） | **投入前（即座）** |
| 実装の複雑さ | job_submit に集中 | cli_filter + conf 管理に分散 |
| 混在ノード対応 | 自動（最初のノードを使用） | conf で明示設定が必要 |
| slurmctld 内部 API 依存 | あり（アップグレード時のリスク） | **なし** |

**混在ノード環境の扱い**:  
パーティション内でノードスペックが混在する場合は `[cluster:NAME]` セクションを分けるか、
最小スペックに合わせた値を設定する。将来的にパーティション別セクションへの拡張も可能。

### 10-2. メモリ上限の委任（MaxMemPerNode）

m の上限チェックはプラグインでは行わず、各パーティションの `slurm.conf` 設定に委任する:

```
PartitionName=compute Nodes=... MaxMemPerNode=<M>
```

CPU をオーバーコミットしない前提では `ntasks_per_node = floor(C/c_eff)` が明示されるため、
slurmctld は per-node memory を正確に計算できる:

```
per-node memory = (m / c_eff) × (c_eff × floor(C / c_eff))
               = m × floor(C / c_eff)
```

`m × floor(C/c_eff) > M` のジョブは投入時に即座に拒否される（PENDING にはならない）。

### 10-3. bridge env var のスコープ

`_SLURM_RSC_SPEC` / `_SLURM_RSC_CONFIG` は一時プロセス環境変数。以下の 2 箇所で後片付けする:
- `cli_filter_rsc: pre_submit()` の正常終了・エラー終了時
- `spank_rsc: slurm_spank_fini()` でも無条件に `unsetenv` することで早期 return 時も確実に消える

`_SLURM_RSC_C_EFF` 用の追加 bridge env var は不要。`SLURM_CPUS_PER_TASK` は Slurm が
自動でジョブ環境にセットするため、spank `user_init()` がそれを直接読んで転写する。

### 10-4. srun 対応

srun も cli_filter を呼ぶ。`--rsc` が指定されていない場合（`_SLURM_RSC_SPEC` が未セット）は何もしない。
`--rsc` が指定された場合、`SLURM_JOB_ID` の有無でコンテキストを判定する:

- **割当済みジョブ内の srun ステップ**（`SLURM_JOB_ID` がセット済み）:
  リソースは既に確保されており他ジョブへの影響がないため、`forbidden_with_rsc`
  チェックをスキップする。オプション設定（ntasks / cpus-per-task 等）は通常通り行う。
- **standalone srun**（`SLURM_JOB_ID` 未セット、直接リソース要求）:
  salloc / sbatch と同様に `forbidden_with_rsc` チェックを行う。

---

## 12. d 分ノード・G 分ノードと gres によるアフィニティ

### 12-1. spec 2-3 の定義（概念要約）

仕様書（spec 2-3）はノード内部を均等分割した割当単位を中心にリソース割当を定義している。

**D 分ノード**（サブシステム B）:
- $C_D = \lfloor C/D \rfloor$ 個のコアと $\lfloor M/D \rfloor$ のメモリを排他的に持つ割当単位
- $D$ = ノードの物理分割数（ソケット数または NUMA ノード数）
- サブシステム B における D 分ノードがサブシステム C の G 分ノードに相当する

**G 分ノード**（サブシステム C）:
- $C_G = \lfloor C/G \rfloor$ 個のコア、$\lfloor M/G \rfloor$ のメモリ、演算加速器 1 個を排他的に持つ割当単位
- $G$ = ノードあたりの GPU 数
- `--rsc g=N` 指定時: g 個の G 分ノードを割当て、各 G 分ノードに 1 プロセス

**D 分ノードが使われる条件**（spec 2-3(1)(a)）:

ノード専有モード（$p \times c \geq C$）の割当後に残りプロセス $p_r = p - n \times \lfloor C/c \rfloor > 0$ が
生じたとき:

| 条件 | 割当内容 |
|---|---|
| $p_r \times c \leq C_D$ | D 分ノード 1 個を追加割当（部分ノード） |
| $p_r \times c > C_D$ | フルノード 1 個を追加割当 |

---

### 12-2. G 分ノードの Slurm 実装（gres によるデバイス-コア紐づけ）

G 分ノードは Slurm の GRES（Generic Resources）機構で実装する。
`gres.conf` の `Cores=` パラメータで GPU 番号ごとに CPU コア範囲を 1 対 1 で対応付け、
これが G 分ノードの実体（GPU-コアのアフィニティマップ）となる。

```
# /etc/slurm/gres.conf（ノードあたり 4 GPU・48 コアの例）
# GPU 0 → コア 0-11、GPU 1 → コア 12-23、GPU 2 → コア 24-35、GPU 3 → コア 36-47
NodeName=gpu[01-08] Name=gpu File=/dev/nvidia[0-3] Cores=0-11,12-23,24-35,36-47
```

**cli_filter_rsc の GPU モード処理と spec 定義の対応**:

| spec 定義 | cli_filter_rsc の設定 | 根拠 |
|---|---|---|
| g 個の G 分ノードを割当て | `ntasks = g` | タスク数 = GPU 数 |
| 各 G 分ノードに 1 プロセス | `gpus-per-task = 1` | タスクと GPU を 1:1 で束ねる |
| G 分ノードのコア数 $C_G = \lfloor C/G \rfloor$ | `cpus-per-task = node_cpus / node_gpus` | タスクに割当てるコア数 |

`gpus-per-task=1` によって Slurm は `gres.conf` の `Cores=` 情報を参照し、
GPU に紐づくコア群をそのタスクに自動的にアフィニティ設定する（GpuAffinity）。
`cpus-per-task = C/G` がこれと一致することで G 分ノードの排他割当が成立する。

rsc_defaults.conf の `node_gpus`（= G）と `node_cpus`（= C）がこの計算の根拠となる:

```ini
node_cpus = 48   # C: ノードあたりコア数
node_gpus = 4    # G: ノードあたり GPU 数
# → cpus-per-task = 48 / 4 = 12 = C_G
```

---

### 12-3. D 分ノードの Slurm 実装

**conf 設定**:

`rsc.conf` の `[default]`（または `[cluster:X]`）セクションに `node_sockets = D`
（D = ソケット数）を追加することで D 分ノード判定が有効になる。
`0` を指定すると判定を行わない。

```ini
[default]
node_cpus    = 48
node_mem     = 196608
node_sockets = 2    # D: ノードあたりのソケット数（0 = D分ノード判定を行わない）
```

`gres.conf` / `slurm.conf` に NUMA gres を設定することで、
D分ノードケースでソケット境界の厳密な強制が有効になる:

```ini
# /etc/slurm/slurm.conf
GresTypes=gpu,numa

# /etc/slurm/gres.conf（48コア・2ソケット、NUMA 0 = コア 0-23、NUMA 1 = 24-47）
NodeName=node[01-32] Name=numa Count=2 Cores=0-23,24-47
```

**p_r 判定ロジック**（Section 6-3 の実装参照）:

ノード専有モード（$p \times c_{eff} \geq C$）のとき、残りプロセス数 $p_r$ を計算し
D 分ノード条件 $p_r \times c_{eff} \leq C_D = \lfloor C/D \rfloor$ を評価する:

| 条件 | 実装方法 | $n_{needed}$ |
|---|---|---|
| $p_r = 0$ | フルノードのみ | $n_{full}$ |
| $p_r > 0$、$p_r \times c_{eff} \leq C_D$、`node_sockets > 0` | **het job（後述）** | $n_{full}$ + 1コンポーネント |
| $p_r > 0$、$p_r \times c_{eff} > C_D$、`node_sockets > 0` | フルノード追加 | $n_{full} + 1$ |
| $p_r > 0$、`node_sockets = 0`（未設定） | 保守的フルノード追加 | $n_{full} + 1$ |

**D分ノードケースの het job 実装**:

ソケット境界を厳密に強制するため、cli_filter_rsc の `pre_submit()` が
`cli_filter_het_append()` を呼び出して **第 2 het コンポーネント**（部分ノード用）を追加する。
salloc/sbatch は `cli_filter_het_ctx_fini()` でこのコンポーネントを回収し、
`job_req_list` に追加する。

```
Component 0（フルノード）:  ntasks=n_full*n_tpn, nodes=n_full, tres-per-node=gres/numa:D
Component 1（D分ノード）:   ntasks=p_r,          nodes=1,      tres-per-node=gres/numa:1
                            GRES_ENFORCE_BIND → ソケット境界を厳密に強制
```

`cli_filter_het_append()` で渡す `job_desc_msg_t` の主要フィールド:

```c
partial->num_tasks     = p_r;
partial->min_nodes     = 1;
partial->max_nodes     = 1;
partial->cpus_per_task = (uint16_t) c_eff;
partial->tres_per_node = xstrdup("gres/numa:1");
partial->bitflags     |= GRES_ENFORCE_BIND;  /* slurm/slurm.h:1199 */
```

Component 0（フルノード）は `slurm_option_set()` で更新する:

```c
slurm_option_set(opt, "ntasks",        n_full * n_tpn, true);
slurm_option_set(opt, "nodes",         n_full,          true);
slurm_option_set(opt, "tres-per-node", "gres/numa:D",   false);
```

**cli_filter het 追加 API**（`src/interfaces/cli_filter.h`）:

| 関数 | 呼び出し元 | 役割 |
|---|---|---|
| `cli_filter_het_ctx_init()` | salloc/sbatch、het ループ前 | スレッドローカルリストを初期化 |
| `cli_filter_het_ctx_fini()` | salloc/sbatch、het ループ後 | 追加コンポーネントのリストを返す |
| `cli_filter_het_append(desc)` | pre_submit() 内 | コンポーネントを登録（所有権移転） |

**SPANK / env vars の継承**:

- Component 0 は SPANK の `init_post_opt` が `SLURM_RSC_*` を `setenv()` → 子シェルに継承 ✓
- Component 1 はプラグインが生成するため SPANK フック未呼び出しだが、
  `setenv()` で設定された環境変数は子プロセスに継承されるため `SLURM_RSC_*` は参照可能 ✓

**資源量計算（spec 3-1(1): $D \times n + n_D$）との対応**:

| ケース | $n$（フルノード数） | $n_D$ | 資源量 |
|---|---|---|---|
| $p_r = 0$ | $n_{full}$ | $0$ | $D \times n_{full}$ |
| D分ノードケース（het job） | $n_{full}$ | $1$ | $D \times n_{full} + 1$ |
| フルノード追加ケース | $n_{full} + 1$ | $0$ | $D \times (n_{full} + 1)$ |

**ユーザ指定 het-job（`:` 構文）との組み合わせ**:

sbatch/salloc で `--rsc` と `:` 区切りのヘテロジョブ構文を同時に使用した場合の挙動をまとめる。

*前提知識*: het-job は `--het-job` オプションではなく `:` セパレータ構文で指定する。
`slurm_opt.c` に `"het-job"` という名前の登録オプションは存在しないため、
`forbidden_with_rsc` ポリシー（`slurm_option_isset` ベース）での制御は不可能。

`forbidden_with_rsc` の競合チェックは `_SLURM_RSC_SPEC` が存在する間のみ実行される。
sbatch/salloc はコンポーネントごとに `cli_filter_p_pre_submit(opt, offset)` を呼び出すが、
offset=0 処理の末尾で `unsetenv("_SLURM_RSC_SPEC")` が実行されるため、
offset=1 以降（ユーザーの `:` コンポーネント）では `ntasks` / `nodes` 等の deny チェックが走らない。

| ケース | 挙動 | 問題の有無 |
|---|---|---|
| `--rsc` のみ（D分ノードなし） | 通常の 1 コンポーネントジョブ | なし |
| `--rsc` のみ（D分ノード発火） | `cli_filter_het_append()` で 2 コンポーネント自動生成 | なし |
| `--rsc … : --ntasks=N` (D分ノードなし) | offset=0 で rsc 処理、offset=1 は `ntasks=N` をそのまま使用 | なし（`:` コンポーネントへの deny 適用なし）|
| `--rsc … : --ntasks=N` (D分ノード発火) | offset=0 で partial を append → 計 3 コンポーネント（ユーザー期待は 2 つ） | **あり** |

**D分ノードケースのみ** ユーザー `:` コンポーネントと rsc-appended コンポーネントが混在し、
意図しないコンポーネント数になる。

**課題: `:` コンポーネントへの forbidden_with_rsc 未適用**

offset=0 末尾で `_SLURM_RSC_SPEC` を unsetenv するため、offset=1+ では
`ntasks` / `nodes` 等の deny/warn チェックが走らない（`:` コンポーネントがポリシー適用外）。

offset=1+ へのリソース割り当ても不可能: `_SLURM_RSC_SPEC` は単一ジョブ全体の仕様（`p=N:c=M`）
であり、`:` コンポーネントが独自のリソース要件を持つ場合に適用できない
（複数コンポーネントに `--rsc` を個別指定する手段がない）。

ポリシーチェックだけで実質リソース指定できない以上、
**`--rsc` と `:` het-job 構文の組み合わせを禁止する**のが最もシンプルかつ正しい方針。

**実装方針（未実装・設計メモ）**

offset=1 で `--rsc` がアクティブであることを検出し `SLURM_ERROR` を返す。
検出方法は 2 案:

| 案 | 方法 | 変更ファイル |
|---|---|---|
| A | `_SLURM_RSC_SPEC` を cli_filter で unsetenv せず残す。offset > 0 かつ SPEC 存在 → エラー。クリーンアップは `spank_fini` に委任 | `cli_filter_rsc.c` のみ |
| B | `_rsc_opt_cb` で `_SLURM_RSC_ACTIVE=1` を追加 setenv。offset > 0 かつ ACTIVE 存在 → エラー。ACTIVE の unsetenv を `spank_fini` に追加 | `spank_rsc.c` + `cli_filter_rsc.c` |

案 A の方が変更箇所が少ない。エラーメッセージ例:

```c
if (offset > 0) {
    error("rsc: --rsc cannot be combined with het-job ':' syntax");
    return SLURM_ERROR;
}
```

**将来拡張：`--rsc` マルチグループ構文**

`:` 構文との混在を禁止する代わりに、`--rsc` 自身が複数 het-job グループを
記述できる拡張構文により het-job との共存を実現する設計案。

*構文案*: `|` 区切りでグループを列挙する。各グループは既存の
`p=N:t=N:c=N:m=N` または `g=N` 構文のまま。

```bash
sbatch --rsc 'p=4:c=4 | p=2:c=8' job.sh
```

代替案と不採用理由:

| 案 | 不採用理由 |
|---|---|
| `--rsc SPEC0 --rsc SPEC1`（複数フラグ） | SPANK の値累積が必要で複雑 |
| `--rsc SPEC0 --rsc-group SPEC1`（別フラグ） | 新規オプション登録・既存との非対称性 |

*処理フロー*: offset=0 の cli_filter がグループ 0 を処理し、
グループ 1 以降を `cli_filter_het_append()` で追加する（sbatch の `:` 構文不要）。

D分ノードケースとの共存:

```
--rsc 'p=4:c=4 | p=2:c=8'、グループ0でD分ノード発火の場合:
  het-group 0 → グループ0 フルノード
  het-group 1 → グループ0 D分ノード partial（自動生成）
  het-group 2 → グループ1（p=2:c=8）
```

D分ノード partial の挿入によりグループ 1+ の het-group index がずれるため、
実際に割り当てられた index を環境変数で提供する。

*環境変数*（Slurm の `SLURM_JOB_ID_HET_GROUP_N` 規則に準拠）:

| 変数 | 意味 | 例（D分ノードなし） | 例（グループ0でD分ノード） |
|---|---|---|---|
| `SLURM_RSC_NUM_GROUPS` | --rsc で指定したグループ数 | 2 | 2 |
| `SLURM_RSC_HET_GROUP_0` | rsc グループ 0 の実際の het-group index | 0 | 0 |
| `SLURM_RSC_HET_GROUP_1` | rsc グループ 1 の実際の het-group index | 1 | 2 |

ユーザーの使い方:

```bash
# job.sh 内（D分ノード有無に関わらず正しいグループを指定できる）
srun --het-group=$SLURM_RSC_HET_GROUP_0 ./app_a &
srun --het-group=$SLURM_RSC_HET_GROUP_1 ./app_b &
wait
```

*実装上の要点（未実装・設計メモ）*:

1. `rsc_parse_request()` で `|` 区切りをパース → 複数 `rsc_request_t` を返す
2. cli_filter offset=0 でグループ 0 を設定、グループ 1..N-1 を `cli_filter_het_append()` で追加
3. append 後に確定した het-group index を追跡し、`SLURM_RSC_HET_GROUP_N` を
   `spank_job_control_setenv()` で設定（spank_rsc の `init_post_opt` で実施）
4. D分ノード partial の append により後続グループの index がずれるため、
   すべての append 完了後に確定 index を記録する順序が重要

---

## 11. 検証方法

```bash
# ビルド（autoconf → configure → make）
cd slurm-25.11.4
autoconf
./configure --prefix=/tmp/slurm
make -j"$(nproc)" -C src/plugins/cli_filter/rsc/ \
                  -C src/salloc/ -C src/sbatch/

# slurm.conf に以下を追加して再起動:
#   CliFilterPlugins=rsc
#   PlugStackConfig=/etc/slurm/plugstack.conf   ← plugstack.conf で config= を指定

# CPU モード確認
salloc --rsc p=2:t=2:c=2:m=1G /bin/env | \
  grep -E "SLURM_NTASKS|SLURM_NPROCS|SLURM_CPUS_PER_TASK|SLURM_TASKS_PER_NODE"
# 期待値:
#   SLURM_NTASKS=2
#   SLURM_NPROCS=2
#   SLURM_CPUS_PER_TASK=2
#   SLURM_TASKS_PER_NODE=2

# GPU モード確認
salloc --rsc g=1 /bin/env | grep -E "SLURM_NTASKS|SLURM_CPUS_PER_TASK"
# 期待値: SLURM_NTASKS=1, SLURM_CPUS_PER_TASK=1

# RSC 環境変数確認
salloc --rsc p=2:t=2:c=2:m=1G /bin/env | grep -E "SLURM_RSC|OMP_"
# 期待値:
#   SLURM_RSC_SPEC=p=2:t=2:c=2:m=1G
#   SLURM_RSC_P=2
#   SLURM_RSC_T=2
#   SLURM_RSC_C=2
#   SLURM_RSC_M=1024
#   SLURM_RSC_C_EFF=2   (または c_eff > c の場合はより大きい値)
#   OMP_NUM_THREADS=2
#   OMP_PROC_BIND=close
#   OMP_PLACES=cores

# D分ノードケース検証（node_cpus=48, node_sockets=2 → C_D=24, n_tpn=12）
# p=14, c=4 → n_full=1, p_r=2, p_r*c_eff=8 <= C_D=24 → D分ノード het job
salloc --rsc p=14:c=4 /bin/env | \
  grep -E "SLURM_HET_JOB|SLURM_RSC|SLURM_JOB_NODELIST"
# 期待値:
#   SLURM_HET_JOB_ID=<jobid>        ← het job として投入されたことを確認
#   SLURM_RSC_P=14, SLURM_RSC_C=4  ← Component 0 の SPANK が設定
# squeue で確認:
squeue -j <jobid> -o "%i %m %C %R"
# 期待値: het job 2コンポーネント（フルノード + D分ノード）
```
