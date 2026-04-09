# `--rsc` オプション実装調査ノート

> 対象: Slurm 25.11.4  
> 根拠仕様: `specs/slurm-sched-spec.new.md` 項目 2-1〜2-4、8-2、8-4

---

## 1. 概要

spec 2-4 は、ジョブのCPU/GPU資源を一括指定するための統合オプション `--rsc` を定義する。  
Slurm は `--rsc` を標準でサポートしないため、**SPANKプラグイン**（クライアント側）と **job_submit プラグイン**（slurmctld 側）の二層構成で実装する。

---

## 2. `--rsc` オプション仕様まとめ

### 2-1. 書式（spec 2-4）

```
CPU資源指定:  --rsc p={procs}:t={threads}:c={cores}:m={memory}
GPU資源指定:  --rsc g={gpus}
```

- (1)と(2)は**排他**（同時指定不可）
- `{...}` はプレースホルダ。ジョブ投入時に実際の数値で置き換える

### 2-2. 各パラメータの意味と制約（spec 2-1、2-2）

| パラメータ | 意味 | 制約 | 省略時デフォルト | 最大値 |
|---|---|---|---|---|
| `p` | プロセス数 | 正整数 | `1` | Slurm パーティション / QOS 設定に委任 |
| `t` | プロセスあたりスレッド数 | 正整数 | `1` | `c × 4`（ハードコーディング） |
| `c` | プロセスあたりコア数 | `1 ≤ c ≤ C`（C = ノードあたりコア数）、かつ c は t の約数または倍数 | `1` | Slurm パーティション / QOS 設定に委任 |
| `m` | プロセスあたりメモリ上限 (MiB) | 正整数（単位サフィックス **M/G/T** 可、省略時は MiB 扱い、1024ベース）。内部値は MiB に変換。M = ノードあたり利用可能メモリとして `c' = ⌈(m/M)×C⌉ > c` なら c' を実効コア数に使用 | `c × m_default`（m_default はコアあたりのデフォルトメモリ MiB/コア） | Slurm パーティション設定 `MaxMemPerNode` に委任 |
| `g` | ジョブあたりGPU数（サブシステムC） | `1 ≤ g ≤ G × N_C`（G = ノードあたりGPU数、N_C = サブシステムCのノード数） | — | — |

### 2-3. spec 8-4 との関係

spec 8-4 は「`--rsc` 指定があった場合、その内容に基づいて項目2-1(3)〜(5) および 2-2(1) のジョブ属性を**自動設定**すること」を要求する。  
つまり `--rsc` は**変換層**であり、最終的には Slurm ネイティブのリソース指定に変換して投入される。

---

## 3. 実装アーキテクチャ

```
ユーザ
  |
  |  sbatch --rsc p=4:t=2:c=4:m=8192 job.sh
  v
[SPANKプラグイン: spank_rsc.so]  ← ログインノード / sbatch コンテキスト
  - --rsc オプションを登録・受付
  - 文字列を解析・検証
  - SPANK環境変数にセット (spank_job_env)
  |
  |  RPC: job_submit_request
  v
[slurmctld]
  |
  v
[job_submitプラグイン: job_submit_rsc.so]  ← slurmctld 側
  - job->spank_job_env から SPANK変数を読み取り
  - job_desc_msg_t フィールドへ変換
  - 制約チェック（メモリ換算コア数 c' の適用など）
  |
  v
[Slurm スケジューラ]
  - 通常の資源割当・ジョブ実行
```

---

## 4. SPANKプラグイン実装詳細

### 4-1. 主要API

**ヘッダ**: `slurm/spank.h`  
**参考実装**: `src/plugins/job_submit/pbs/spank_pbs.c`

```c
// プラグイン宣言
SPANK_PLUGIN(rsc, 1);

// オプション定義
static struct spank_option rsc_options[] = {
    {
        .name    = "rsc",
        .arginfo = "RESOURCE_SPEC",
        .usage   = "Resource specification: p=N:t=N:c=N:m=N or g=N",
        .has_arg = 1,           // 引数必須
        .val     = 1,
        .cb      = rsc_opt_cb,  // コールバック関数
    },
    SPANK_OPTIONS_TABLE_END
};
```

### 4-2. `slurm_spank_init()` でのオプション登録

`spank_option_register()` は **`slurm_spank_init()` からのみ有効**。  
全コンテキストで呼び出すことが必要（`S_CTX_LOCAL`、`S_CTX_REMOTE`、`S_CTX_ALLOCATOR`）。

```c
int slurm_spank_init(spank_t sp, int ac, char **av)
{
    return spank_option_register(sp, &rsc_options[0]);
}
```

### 4-3. コールバック関数

```c
static int rsc_opt_cb(int val, const char *optarg, int remote)
{
    // remote == 1: slurmstepd側 → 解析不要（環境変数から取得済み）
    if (remote)
        return 0;

    if (optarg == NULL)
        return -1;

    // GPU/CPUモードの判定と解析は後述フローチャート参照
    // 解析結果を SPANK環境変数にセット
    // spank_setenv() は slurm_spank_init_post_opt() 内から呼ぶのが安全
    // → optarg をスタティック変数に退避しておく
    rsc_optarg = strdup(optarg);
    return 0;
}
```

### 4-4. `slurm_spank_init_post_opt()` での処理

コールバックで退避した `rsc_optarg` をここで解析・検証し、job control 環境変数にセットする。

**環境変数の 3 層構造**

```
[LOCAL/ALLOCATOR: sbatch / srun / salloc]
  spank_job_control_setenv(sp, "RSC_CONFIG", ...)   ← SPANK_ プレフィックスなし
  spank_job_control_setenv(sp, "RSC_CLUSTER", ...)
  spank_job_control_setenv(sp, "RSC_SPEC", ...)
  spank_job_control_setenv(sp, "RSC_P/T/C/M/G", ...)
        |
        | Slurm が spank_job_env に転記する際に自動で "SPANK_" を付加
        v
[slurmctld: job_submit_rsc.c が spank_job_env から読む]
  SPANK_RSC_CONFIG / SPANK_RSC_CLUSTER / SPANK_RSC_SPEC / SPANK_RSC_P/T/C/M/G
  ※ SPANK_RSC_C_EFF は job_submit が spank_job_env に書き込む
        |
        | slurmd に渡る
        v
[REMOTE: slurm_spank_user_init が SPANK_RSC_* を読んで SLURM_RSC_* にコピー]
  SLURM_RSC_SPEC / SLURM_RSC_P / SLURM_RSC_T / SLURM_RSC_C / SLURM_RSC_M
  SLURM_RSC_C_EFF / SLURM_RSC_G / OMP_NUM_THREADS / OMP_PROC_BIND / OMP_PLACES
        |
        v
  [ユーザジョブが参照する変数]
```

`slurm_spank_init_post_opt()` は LOCAL/ALLOCATOR と REMOTE の両コンテキストで呼ばれる。
`spank_job_control_setenv()` は LOCAL/ALLOCATOR でのみ有効なため、
REMOTE コンテキストでは即 return する。

```c
int slurm_spank_init_post_opt(spank_t sp, int ac, char **av)
{
    // spank_job_control_setenv は LOCAL/ALLOCATOR context でのみ有効。
    // REMOTE (slurmd) では job control env はすでに設定済みで、
    // user_init が SLURM_RSC_* へのコピーを担当する。
    spank_context_t ctx = spank_context();
    if (ctx != S_CTX_LOCAL && ctx != S_CTX_ALLOCATOR)
        return 0;

    // rsc_optarg == NULL は "--rsc 未指定" を意味する。
    // この場合も CPUモードのデフォルト値で処理するためフローを継続する。
    // (GPU モードへは進まない)

    // plugstack.conf の引数 "config=/path/to/file" からファイルパスを取得
    const char *config_path = get_plugin_arg(av, ac, "config");
    if (!config_path) {
        slurm_error("rsc: config= argument not specified in plugstack.conf");
        return -1;
    }

    // 設定ファイルを読み込む（フローチャート参照）
    // LOCAL context ではプロセス環境変数から SLURM_CLUSTER_NAME を読む
    char cluster[128] = "";
    const char *cluster_env = getenv("SLURM_CLUSTER_NAME");
    if (cluster_env)
        snprintf(cluster, sizeof(cluster), "%s", cluster_env);
    rsc_config_t cfg;
    if (load_rsc_config(config_path, cluster, &cfg) != 0)
        return -1;  // エラーメッセージは load_rsc_config 内で出力

    // --rsc 未指定時は GPU モードへ進まず、CPU デフォルト値を直接セット
    if (rsc_optarg == NULL) {
        // p=1, t=1, c=1, m=1 × cfg.m_default を適用
        goto apply_cpu_defaults;
    }

    // GPU/CPUモードを判定して文字列を解析（フローチャート参照）
    // ...

    // m= の単位サフィックス変換（1024ベース、内部値は MiB）:
    //   uint64_t m_mib = parse_m(m_str);
    //     "4G"    → 4 × 1024       = 4096 MiB
    //     "1T"    → 1 × 1024×1024  = 1,048,576 MiB
    //     "4096M" → 4096            = 4096 MiB
    //     "4096"  → 4096            = 4096 MiB (サフィックスなし = MiB)
    //     "4096K" → UINT64_MAX (K サフィックスは非サポート、エラー)

apply_cpu_defaults:
    // 省略されたキーにデフォルト値を補完:
    //   p が未指定 → p = 1
    //   t が未指定 → t = 1
    //   c が未指定 → c = 1  (m 計算の前に確定する)
    //   m が未指定 → m = c × cfg.m_default
    //               (m_default はコアあたりのデフォルトメモリ MiB/コア)
    // 最大値チェック（超過時は RETURN -1）:
    //   t > c * 4    → エラー: "t exceeds 4x c (max threads per core limit)"
    // ※ m の上限は Slurm パーティション MaxMemPerNode に委任
    //   (CPU 非オーバーコミット前提で ntasks_per_node が確定するため正確に判定可能)
    // ※ p, c の上限は Slurm パーティション / QOS 設定に委任

    // 成功時: spank_job_control_setenv で job control env にセット
    // Slurm が spank_job_env に転記する際に自動で "SPANK_" を付加する
    spank_job_control_setenv(sp, "RSC_CONFIG", config_path, 1);
    spank_job_control_setenv(sp, "RSC_CLUSTER", cluster, 1);
    spank_job_control_setenv(sp, "RSC_SPEC", rsc_optarg ? rsc_optarg : "", 1); // 省略時は ""
    spank_job_control_setenv(sp, "RSC_P", p_str, 1);
    spank_job_control_setenv(sp, "RSC_T", t_str, 1);
    spank_job_control_setenv(sp, "RSC_C", c_str, 1);
    spank_job_control_setenv(sp, "RSC_M", m_mib_str, 1);  // 常に MiB 値
    // GPU時のみ:
    spank_job_control_setenv(sp, "RSC_G", g_str, 1);

    return 0;
}
```

**`parse_m()` 変換ロジック（擬似コード）**

```c
// 戻り値: MiB 値。エラー時は UINT64_MAX を返す
uint64_t parse_m(const char *val_str) {
    char *end;
    uint64_t v = strtoull(val_str, &end, 10);
    if (end == val_str) return UINT64_MAX;  // 数字なし
    switch (toupper((unsigned char)*end)) {
        case 'T':  v *= 1024ULL * 1024; break;  // TiB → MiB
        case 'G':  v *= 1024ULL;        break;  // GiB → MiB
        case 'M':  /* v *= 1 */         break;  // MiB → MiB
        case '\0': /* no suffix = MiB */break;
        default:   return UINT64_MAX;           // 不正サフィックス (K 含む)
    }
    return v;
}
```

job control 環境変数は `job_desc_msg_t.spank_job_env[]` に格納されてコントローラへ伝達される。
Slurm は `spank_job_control_setenv(sp, "RSC_*", ...)` で格納した変数に自動で `SPANK_` を付加するため、
slurmctld からは `SPANK_RSC_*` として参照できる（`RSC_ENV_*` マクロはこの名前を定義している）。

### 4-5. コンテキスト別動作まとめ

| コンテキスト | 場所 | 動作 |
|---|---|---|
| `S_CTX_LOCAL` | sbatch / srun | `--rsc` を解析、job control env (`RSC_*`) にセット |
| `S_CTX_ALLOCATOR` | salloc | 同上 |
| `S_CTX_REMOTE` | slurmstepd | `slurm_spank_init_post_opt` は即 return。`slurm_spank_user_init` が `SLURM_RSC_*` をジョブ実行環境にセット |
| `S_CTX_JOB_SCRIPT` | prolog/epilog | OMP_NUM_THREADS 等を環境変数から設定可 |

### 4-6. ユーザジョブへの環境変数伝搬

`slurm_spank_user_init()` が REMOTE コンテキスト（slurmstepd）で実行される際、
`spank_setenv()` によって以下の変数がジョブ実行環境にセットされる。

| 変数名 | 内容 | 備考 |
|---|---|---|
| `SLURM_RSC_SPEC` | `--rsc` に渡した未加工の文字列 | `--rsc` 省略時は `""` |
| `SLURM_RSC_P` | プロセス数 | 省略時は自動補完値 |
| `SLURM_RSC_T` | プロセスあたりスレッド数 | 省略時は自動補完値 |
| `SLURM_RSC_C` | 指定コア数（c' 適用前） | 省略時は自動補完値 |
| `SLURM_RSC_M` | プロセスあたりメモリ (MiB) | 省略時は `c × m_default` |
| `SLURM_RSC_C_EFF` | 実効コア数（c' 適用後） | `SLURM_CPUS_PER_TASK` と同値 |
| `SLURM_RSC_G` | GPU 数 | GPU モード時のみ設定 |
| `OMP_NUM_THREADS` | スレッド数 (= t) | ユーザ設定があれば上書きしない |
| `OMP_PROC_BIND` | スレッドバインドポリシー | c=t→`close` / c\<t→`close` / c>t→`spread`。ユーザ設定があれば上書きしない |
| `OMP_PLACES` | スレッド配置単位 | c=t→`cores` / c\<t→`threads` / c>t→`cores`。ユーザ設定があれば上書きしない |

`SLURM_RSC_C_EFF` は `c' = ⌈(m/M)×C⌉ > c` の場合に `SLURM_RSC_C` より大きい値になる。  
GPU モードの場合は `SLURM_RSC_SPEC` と `SLURM_RSC_G` のみがセットされ、`SLURM_RSC_P/T/C/M/C_EFF` および `OMP_*` はセットされない。

---

## 5. デフォルト値・最大値設定ファイル

### 5-1. ファイル配置と指定方法

設定ファイルのパスは `plugstack.conf` のプラグイン引数 `config=` で指定する。

```
# /etc/slurm/plugstack.conf
required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf
```

プラグインの `slurm_spank_init(ac, av)` で `av[]` を走査し `config=` プレフィックスを持つ引数からパスを取得する。

### 5-2. 設定ファイルフォーマット

INIスタイル。`[default]` セクションにクラスタ共通の設定を記述し、
クラスタ別に上書きしたい場合は `[cluster:CLUSTER_NAME]` セクションを追加する。

```ini
# /etc/slurm/rsc_defaults.conf

[default]
m_default = 4096    # コアあたりのデフォルトメモリ (MiB/コア); m 省略時は c × m_default を使用
                    # パーティションごとのメモリ上限は slurm.conf の MaxMemPerNode に委任
# ノード専有モード (p×c ≥ C) 時のノード数制御方式
# exact: min_nodes = max_nodes = n_needed を設定（ノード数を厳密に固定、散らばり防止）
# min:   min_nodes = n_needed のみ設定（下限保証、上限フリー）
# none:  設定しない（Slurm デフォルト挙動）
# 省略時のデフォルト: exact
node_count_mode = exact
# p, c の上限は Slurm パーティション / QOS 設定に委任
# t の上限は c × 4 としてプラグインにハードコーディング

[cluster:clusterA]
m_default = 8192    # clusterA のノードはメモリリッチ
node_count_mode = exact

[cluster:clusterB]
m_default = 2048    # clusterB のノードはメモリ少
node_count_mode = min

[forbidden_with_rsc]
# --rsc と競合する Slurm 標準オプションの扱いを定義する
# 値: deny (エラーで拒否) または warn (警告のみ、ジョブは通す)
# リストにないオプションは常に許可

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

# --- グループ D: 運用ルール上の制御対象 (warn 推奨、必要なら deny) ---
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

### 5-3. 設定読み込み方針

`m_default`、`node_count_mode` 等の解決順序（共通）:
1. `SLURM_CLUSTER_NAME` 環境変数を読み取り、`[cluster:CLUSTER_NAME]` セクションを検索
2. 一致するセクションがない場合は `[default]` セクションにフォールバック
3. `[default]` も存在しない場合はエラーとしてジョブを拒否

`SLURM_CLUSTER_NAME` は sbatch 実行ノード（ログインノード）の所属クラスタ名を示す。
`--clusters=xxx` でクロスクラスタ投入する場合は投入ノードのクラスタ名が使われるため、
クラスタごとに専用ログインノードを用意することを推奨する。

`[forbidden_with_rsc]` セクションはクラスタに依存しないクラスタ全体の設定である。
パーティションごとにデフォルトメモリを変えたい場合はユーザが `m=` を明示して指定する。

### 5-4. 最大値チェックのタイミング

最大値チェックは **SPANKプラグイン側（`slurm_spank_init_post_opt()`）** で実施する。  
これにより、slurmctld に到達する前にユーザへエラーを返すことができ、  
不正なジョブが投入キューに入ることを防ぐ。

### 5-5. エラー挙動まとめ

| 状況 | エラーメッセージ例 | 結果 |
|---|---|---|
| 設定ファイルが存在しない | `rsc: cannot open config file: /etc/slurm/rsc_defaults.conf` | ジョブ拒否 |
| クラスタセクションも `[default]` も存在しない | `rsc: no [cluster:NAME] or [default] section in config file` | ジョブ拒否 |
| t が c × 4 を超過 | `rsc: t=N exceeds 4x c (c=M)` | ジョブ拒否 |
| p, c の上限超過 | Slurm パーティション / QOS 設定によりジョブ拒否（プラグインは関与しない） | — |
| m × floor(C/c_eff) が M を超過 | Slurm パーティション `MaxMemPerNode` によりジョブ拒否（プラグインは関与しない） | — |
| `deny` 設定オプションとの競合 | `rsc: option --ntasks conflicts with --rsc` | ジョブ拒否 |
| `warn` 設定オプションとの競合 | `rsc: warning: --nodes specified with --rsc, --rsc takes precedence` | 警告のみ |

---

## 6. job_submit プラグイン実装詳細

### 6-1. 主要データ構造

**ヘッダ**: `slurm/slurm.h`（`job_descriptor` 構造体）

```c
// 資源関連フィールド（抜粋）
uint32_t num_tasks;        // プロセス数 (= p)
uint16_t cpus_per_task;    // プロセスあたりCPU数 (= c または c')
uint64_t pn_min_memory;    // ノードあたりメモリ下限 (MB)
                           // MEM_PER_CPU フラグを立てるとCPUあたり解釈
uint16_t ntasks_per_node;  // ノードあたりプロセス数 (= floor(C/c))
uint32_t min_nodes;        // 最小ノード数（node_count_mode = min/exact 時に設定）
uint32_t max_nodes;        // 最大ノード数（node_count_mode = exact 時に設定）
char    *tres_per_job;     // GPU指定: "gres/gpu:N"
char   **spank_job_env;    // SPANKプラグインがセットした環境変数配列
uint32_t spank_job_env_size;
```

### 6-2. `spank_job_env` からの読み取り

```c
int job_submit(struct job_descriptor *job, uint32_t submit_uid, char **err_msg)
{
    char *rsc_p = NULL, *rsc_t = NULL, *rsc_c = NULL, *rsc_m = NULL;
    char *rsc_g = NULL;

    for (int i = 0; i < job->spank_job_env_size; i++) {
        if (strncmp(job->spank_job_env[i], "SPANK_RSC_P=", 12) == 0)
            rsc_p = job->spank_job_env[i] + 12;
        // ... 同様に T, C, M, G
    }

    if (rsc_g) {
        // GPU モード (spec 2-3(1)(b)): g 個の G分ノードに各1プロセス
        uint32_t g = atoi(rsc_g);
        // tres_per_task でタスクと GPU を1対1に束ねる（tres_per_job では保証不可）
        xstrfmtcat(job->tres_per_task, "gres/gpu:1");
        job->num_tasks     = g;       // GPU総数 = タスク総数
        job->cpus_per_task = C / G;   // G分ノードあたりのコア数
        // Slurm が自動的に ceil(g/G) ノードを割当て、各ノードに最大 G タスクを配置
    } else if (rsc_p) {
        // CPU モード → フローチャート参照
        uint32_t p = atoi(rsc_p);
        uint32_t t = atoi(rsc_t);
        uint32_t c = atoi(rsc_c);
        uint64_t m = atoi(rsc_m);

        // メモリ換算コア数 c' の適用
        uint32_t c_eff = compute_effective_cores(c, m, M, C);

        job->num_tasks     = p;
        job->cpus_per_task = c_eff;
        // メモリ: プロセスあたり m MB → CPUあたり m/c_eff MB
        job->pn_min_memory = (m / c_eff) | MEM_PER_CPU;

        if (p * c_eff >= C) {
            // ノード専有モード (spec 2-3(1)(a) 前半)
            uint32_t n_tpn    = C / c_eff;                        // = floor(C/c_eff)
            uint32_t p_r      = p % n_tpn;                        // 端数プロセス数
            uint32_t n_needed = p / n_tpn + (p_r > 0 ? 1 : 0);   // 必要ノード数

            job->ntasks_per_node = n_tpn;

            if (cfg.node_count_mode == NODE_COUNT_EXACT ||
                cfg.node_count_mode == NODE_COUNT_MIN) {
                job->min_nodes = n_needed;
            }
            if (cfg.node_count_mode == NODE_COUNT_EXACT) {
                job->max_nodes = n_needed;
            }
        } else {
            // コア指定モード (spec 2-3(1)(a) 後半)
            // 1ノードに全プロセスを収める（キュー排他はパーティション設定に委任）
            job->max_nodes = 1;
            // ntasks_per_node は未設定: Slurm が p タスクを1ノードに自動配置
        }
    }
    return SLURM_SUCCESS;
}
```

### 6-3. 競合オプション検出

#### 設計方針

競合チェックは **job_submit プラグイン（slurmctld 側）** で行う。  
sbatch/salloc/srun のどの経路でも、`#SBATCH` ディレクティブ経由でも、  
`job_descriptor` フィールドが確定した状態で検出できるため確実。

**設定と実装の分離:**
- **設定ファイル** = 「何を禁止するか」（管理者が変更可能）
- **プラグインコード** = 「どのフィールドで検出するか」（再コンパイルが必要だが変わらない）

#### オプション分類

競合対象オプションを以下の4グループに分類する。

| グループ | 性質 | 設定ファイルデフォルト |
|---|---|---|
| **A: 資源量の直接競合** | `--rsc` の変換先フィールドと同一フィールドを設定する | `deny` 推奨 |
| **B: トポロジ・配置制約** | 資源数は変えないがノード割当方式に干渉する可能性がある | `warn` 推奨 |
| **C: ノード選択制約** | 使用ノードを制限し `--rsc` のノード選択と矛盾する可能性がある | `warn` 推奨 |
| **D: 運用ルール上の制御対象** | 資源量とは直接競合しないが運用ポリシー上制御したい場合がある | `warn` 推奨 |

#### `job_descriptor` フィールドと NO_VAL センチネル

| グループ | 設定キー | フィールド | 型 | 未設定値（センチネル） |
|---|---|---|---|---|
| A | `ntasks` | `num_tasks` | `uint32_t` | `NO_VAL` (0xfffffffe) |
| A | `cpus-per-task` | `cpus_per_task` | `uint16_t` | `NO_VAL16` (0xfffe) |
| A | `mem` / `mem-per-cpu` / `mem-per-task` | `pn_min_memory` | `uint64_t` | `NO_VAL64` (0xfffffffffffffffe) |
| A | `nodes` | `min_nodes` | `uint32_t` | `NO_VAL` |
| A | `ntasks-per-node` | `ntasks_per_node` | `uint16_t` | `NO_VAL16` |
| A | `gres` / `gpus-per-node` | `tres_per_node` | `char *` | `NULL` |
| A | `gpus` | `tres_per_job` | `char *` | `NULL` |
| A | `gpus-per-socket` | `tres_per_socket` | `char *` | `NULL` |
| A | `gpus-per-task` | `tres_per_task` | `char *` | `NULL` |
| A | `cpus-per-gpu` | `cpus_per_tres` | `char *` | `NULL` |
| A | `mem-per-gpu` | `mem_per_tres` | `char *` | `NULL` |
| A | `mincpus` | `pn_min_cpus` | `uint16_t` | `NO_VAL16` |
| A | `ntasks-per-gpu` | `ntasks_per_tres` | `uint16_t` | `NO_VAL16` |
| A | `threads-per-core` | `threads_per_core` | `uint16_t` | `NO_VAL16` |
| B | `cores-per-socket` | `cores_per_socket` | `uint16_t` | `NO_VAL16` |
| B | `sockets-per-node` | `sockets_per_node` | `uint16_t` | `NO_VAL16` |
| B | `ntasks-per-core` | `ntasks_per_core` | `uint16_t` | `NO_VAL16` |
| B | `ntasks-per-socket` | `ntasks_per_socket` | `uint16_t` | `NO_VAL16` |
| B | `core-spec` / `thread-spec` | `core_spec` | `uint16_t` | `NO_VAL16` |
| B | `exclusive` / `oversubscribe` | `shared` | `uint16_t` | `NO_VAL16` |
| B | `overcommit` | `overcommit` | `uint8_t` | `NO_VAL8` |
| B | `distribution` | `task_dist` | `uint32_t` | `NO_VAL` |
| B | `spread-job` | `bitflags` (SPREAD_JOB ビット) | `uint64_t` | `0` |
| C | `constraint` | `features` | `char *` | `NULL` |
| C | `cluster-constraint` | `cluster_features` | `char *` | `NULL` |
| C | `contiguous` | `contiguous` | `uint16_t` | `NO_VAL16` |
| C | `exclude` | `exc_nodes` | `char *` | `NULL` |
| C | `nodelist` / `nodefile` | `req_nodes` | `char *` | `NULL` |
| C | `switches` | `req_switch` | `uint32_t` | `NO_VAL` |
| C | `user-min-nodes` | `bitflags` (USE_MIN_NODES ビット) | `uint64_t` | `0` |
| D | `batch` | `batch_features` | `char *` | `NULL` |
| D | `clusters` | `clusters` | `char *` | `NULL` |
| D | `qos` | `qos` | `char *` | `NULL` |
| D | `gpu-bind` | `tres_bind` | `char *` | `NULL` |
| D | `mem-bind` | `mem_bind` | `char *` | `NULL` |
| D | `gres-flags` | `bitflags` (GRES関連ビット) | `uint64_t` | `0` |

#### プラグイン内部テーブルと処理フロー

```c
// プラグイン内部の検出関数テーブル（固定）
// 同じ job_descriptor フィールドを参照するオプションは同じ検出関数を共有する
static const struct {
    const char *name;
    bool (*is_set)(const job_descriptor *);
} known_checks[] = {
    // グループ A: 資源量の直接競合
    { "ntasks",            [](j){ return j->num_tasks != NO_VAL; } },
    { "cpus-per-task",     [](j){ return j->cpus_per_task != NO_VAL16; } },
    { "mem",               [](j){ return j->pn_min_memory != NO_VAL64; } },
    { "mem-per-cpu",       [](j){ return j->pn_min_memory != NO_VAL64; } },
    { "mem-per-task",      [](j){ return j->pn_min_memory != NO_VAL64; } },
    { "nodes",             [](j){ return j->min_nodes != NO_VAL; } },
    { "ntasks-per-node",   [](j){ return j->ntasks_per_node != NO_VAL16; } },
    { "gres",              [](j){ return j->tres_per_node != NULL; } },
    { "gpus",              [](j){ return j->tres_per_job != NULL; } },
    { "gpus-per-node",     [](j){ return j->tres_per_node != NULL; } },
    { "gpus-per-socket",   [](j){ return j->tres_per_socket != NULL; } },
    { "gpus-per-task",     [](j){ return j->tres_per_task != NULL; } },
    { "cpus-per-gpu",      [](j){ return j->cpus_per_tres != NULL; } },
    { "mem-per-gpu",       [](j){ return j->mem_per_tres != NULL; } },
    { "mincpus",           [](j){ return j->pn_min_cpus != NO_VAL16; } },
    { "ntasks-per-gpu",    [](j){ return j->ntasks_per_tres != NO_VAL16; } },
    { "threads-per-core",  [](j){ return j->threads_per_core != NO_VAL16; } },
    // グループ B: トポロジ・配置制約
    { "cores-per-socket",  [](j){ return j->cores_per_socket != NO_VAL16; } },
    { "sockets-per-node",  [](j){ return j->sockets_per_node != NO_VAL16; } },
    { "ntasks-per-core",   [](j){ return j->ntasks_per_core != NO_VAL16; } },
    { "ntasks-per-socket", [](j){ return j->ntasks_per_socket != NO_VAL16; } },
    { "core-spec",         [](j){ return j->core_spec != NO_VAL16; } },
    { "thread-spec",       [](j){ return j->core_spec != NO_VAL16; } },  // 同フィールド
    { "exclusive",         [](j){ return j->shared != NO_VAL16; } },
    { "oversubscribe",     [](j){ return j->shared != NO_VAL16; } },     // 同フィールド
    { "overcommit",        [](j){ return j->overcommit != NO_VAL8; } },
    { "distribution",      [](j){ return j->task_dist != NO_VAL; } },
    { "spread-job",        [](j){ return (j->bitflags & SPREAD_JOB) != 0; } },
    // グループ C: ノード選択制約
    { "constraint",        [](j){ return j->features != NULL; } },
    { "cluster-constraint",[](j){ return j->cluster_features != NULL; } },
    { "contiguous",        [](j){ return j->contiguous != NO_VAL16; } },
    { "exclude",           [](j){ return j->exc_nodes != NULL; } },
    { "nodelist",          [](j){ return j->req_nodes != NULL; } },
    { "nodefile",          [](j){ return j->req_nodes != NULL; } },      // 同フィールド
    { "switches",          [](j){ return j->req_switch != NO_VAL; } },
    { "user-min-nodes",    [](j){ return (j->bitflags & USE_MIN_NODES) != 0; } },
    // グループ D: 運用ルール上の制御対象
    { "batch",             [](j){ return j->batch_features != NULL; } },
    { "clusters",          [](j){ return j->clusters != NULL; } },
    { "qos",               [](j){ return j->qos != NULL; } },
    { "gpu-bind",          [](j){ return j->tres_bind != NULL; } },
    { "mem-bind",          [](j){ return j->mem_bind != NULL; } },
    { "gres-flags",        [](j){ return (j->bitflags & GRES_CONF_ALLOW_MULTI_TYPES) != 0; } },
};
```

```
job_submit() 内の競合チェックフロー:

[SPANK_RSC_* が spank_job_env に存在する?]
  |
  +-- NO → rsc_explicit = false
  +-- YES → rsc_explicit = true
  |
  v
[implicit_check の設定値を確認]
  rsc_explicit = false かつ implicit_check = relaxed?
  → 競合チェック全体をスキップ
  |
  v
[forbidden_with_rsc の各エントリを処理]
  |
  v
  [known_checks テーブルで対応する is_set() を検索]
    見つからない → スキップ (設定ファイルのタイポ等)
    見つかった  → is_set(job) を呼び出す
                    false → 次のエントリへ
                    true  → アクションを適用:
                              deny → *err_msg = "option --XXX conflicts with --rsc"
                                     return SLURM_ERROR
                              warn → slurm_info("rsc: warning: --XXX specified with --rsc")
                                     次のエントリへ
```

### 6-4. Slurm パラメータマッピング

| `--rsc` パラメータ | `job_desc_msg_t` フィールド | 備考 |
|---|---|---|
| `p` | `num_tasks` | プロセス数 |
| `c`（または `c'`） | `cpus_per_task` | 実効コア数 |
| `m` | `pn_min_memory` + `MEM_PER_CPU` フラグ | `m/c` MB per CPU |
| `t` | 環境変数 `OMP_NUM_THREADS` | スレッド数（Slurm直接管理外） |
| `floor(C/c)` | `ntasks_per_node` | ノードあたりプロセス数 |
| `g` | `tres_per_task = "gres/gpu:1"` + `num_tasks = g` | タスクと GPU を1対1に束ねる |

`t` は Slurm の `cpus_per_task` の内部スレッド配置に関わるため、`OMP_NUM_THREADS` 環境変数として  
SPANK `slurm_spank_user_init()` または prolog で設定し、アフィニティ制御と組み合わせる（spec 6-10）。

---

## 7. `--rsc` 解析・判定フローチャート

```
START
  |
  +-- ユーザが --rsc <arg> を指定してジョブを投入
  |
  v
[SPANK: slurm_spank_init_post_opt()]
  |
  +-- rsc_optarg が NULL? --> YES --> [--rsc 未指定]
  |                                     設定ファイル読み込み → CPUデフォルト適用フローへ
  |                                     ↓ (GPU モードには進まない)
  |                                    goto [デフォルト値補完] --------+
  |                                                                     |
  v                                                                     |
[文字列に 'g=' が含まれるか?]                                          |
  |
  +--YES--> [GPUモード] -------------------------------------------+
  |           |                                                      |
  |           v                                                      |
  |         ['g={N}' を解析]                                        |
  |           |                                                      |
  |           v                                                      |
  |         [g が正整数? かつ 1 ≤ g ≤ G×N_C?]                     |
  |           |                                                      |
  |          FAIL --> エラー: "invalid g value" --> RETURN -1       |
  |           |                                                      |
  |          PASS                                                    |
  |           |                                                      |
  |           v                                                      |
  |         [spank_setenv("SPANK_RSC_G", g)]                        |
  |           |                                                      |
  |           v                                                      |
  |         GOTO [job_submit側: GPU処理] ---------------------------->+
  |                                                                  |
  +--NO --> [CPUモード] ------------------------------------------>  |
              |                                                       |
              v                                                       |
            [設定ファイルを読み込む (config= 引数で指定されたパス)]    |
              |                                                       |
             FAIL --> エラー: "cannot open config file" --> RETURN -1 |
              |                                                       |
             PASS                                                     |
              |                                                       |
              v                                                       |
            [SLURM_CLUSTER_NAME を読み取る]                           |
              |                                                       |
              v                                                       |
            [[cluster:NAME] セクションを検索]                        |
              一致あり → そのセクションの m_default を使用           |
              一致なし → [default] セクションにフォールバック         |
                           あり → [default] の m_default を使用      |
                           なし → エラー:                             |
                                  "no [cluster:NAME] or [default]    |
                                   section in config file"           |
                                  --> RETURN -1                      |
              |                                                       |
              v                                                       |
            ['p=', 't=', 'c=', 'm=' の各トークンを ':' で分割して解析] |
              |                                                       |
              v                                                       |
            [デフォルト値補完]                                        |
              p= なし → p = 1                                        |
              t= なし → t = 1                                        |
              c= なし → c = 1  (m 計算の前に確定)                    |
              m= なし → m = c × cfg.m_default                        |
                        (m_default はコアあたりのデフォルトメモリ)   |
              |                                                       |
              v                                                       |
            [p が正整数?]                                            |
              |                                                       |
             FAIL --> エラー: "invalid p" --> RETURN -1              |
              |                                                       |
             PASS                                                     |
              |                                                       |
              v                                                       |
            [t が正整数?]                                            |
              |                                                       |
             FAIL --> エラー: "invalid t" --> RETURN -1              |
              |                                                       |
             PASS                                                     |
              |                                                       |
              v                                                       |
            [c が正整数かつ 1 ≤ c ≤ C?]                             |
              |                                                       |
             FAIL --> エラー: "c out of range [1, C]" --> RETURN -1  |
              |                                                       |
             PASS                                                     |
              |                                                       |
              v                                                       |
            [c % t == 0 または t % c == 0?]                         |
              |  (c は t の約数または倍数)                            |
              |                                                       |
             FAIL --> エラー: "c must be divisor or multiple of t"   |
              |       --> RETURN -1                                   |
              |                                                       |
             PASS                                                     |
              |                                                       |
              v                                                       |
            [m= の文字列を解析・単位変換 (parse_m)]                 |
              数字 + サフィックス (T/G/M/なし) を受け付ける          |
              変換テーブル (1024ベース):                              |
                T → × 1,048,576 MiB                                 |
                G → × 1024 MiB                                       |
                M → × 1 MiB                                          |
                なし → MiB そのまま                                   |
              K サフィックスは非サポート (エラー扱い)                 |
              |                                                       |
             FAIL (不正文字/サフィックス) -->                        |
              エラー: "invalid m" --> RETURN -1                      |
              |                                                       |
             PASS (m_mib に MiB 値が入る)                            |
              |                                                       |
              v                                                       |
            [最大値チェック]                                          |
              t > c * 4?         --> エラー: "t exceeds 4x c"        |
                                     --> RETURN -1                    |
              (m の上限チェックは Slurm パーティション MaxMemPerNode に委任) |
              (p, c の上限チェックは Slurm パーティション/QOS に委任)  |
              |                                                       |
              v                                                       |
            [spank_setenv("SPANK_RSC_P", p)]                         |
            [spank_setenv("SPANK_RSC_T", t)]                         |
            [spank_setenv("SPANK_RSC_C", c)]                         |
            [spank_setenv("SPANK_RSC_M", m)]                         |
              |                                                       |
              v                                                       |
            RETURN 0 (SPANK側完了)                                   |
              |                                                       |
<-----------[slurmctld RPC受信]------------------------------------- +
  |
  v
[job_submit プラグイン: job_submit()]
  |
  +-- SPANK_RSC_G が存在? --> YES --> [GPU処理 (spec 2-3(1)(b))]
  |                                     |
  |                                     v
  |                                   [tres_per_task = "gres/gpu:1"]
  |                                   [num_tasks     = g]
  |                                   [cpus_per_task = C/G (G分ノードのコア数)]
  |                                   → Slurm が ceil(g/G) ノードを自動割当
  |                                   → 各タスクが1GPU を専有
  |                                     |
  |                                   DONE
  |
  +-- NO --> [CPU処理]
               |
               v
             [p, t, c, m を spank_job_env から取得]
               |
               v
             [c' = ⌈(m / M) × C⌉ を計算]
               |
               v
             [c' > c ?]
               |
              YES --> [c_eff = c' (メモリ換算コア数を優先)]
               |
              NO  --> [c_eff = c (指定コア数を使用)]
               |
               v
             [num_tasks     = p]
             [cpus_per_task = c_eff]
             [pn_min_memory = (m / c_eff) MB (per-CPU フラグ付き)]
               |
               v
             [p × c_eff ≥ C ? (ノード専有モード判定)]
               |
              YES --> [ノード専有モード (spec 2-3(1)(a) 前半)]
               |        n_tpn    = floor(C / c_eff)
               |        p_r      = p mod n_tpn
               |        n_needed = floor(p / n_tpn) + (p_r > 0 ? 1 : 0)
               |        [ntasks_per_node = n_tpn]
               |        [node_count_mode=exact/min → min_nodes = n_needed]
               |        [node_count_mode=exact     → max_nodes = n_needed]
               |        ※ D分ノード: p_r×c_eff≤C_D (= C/D) なら tres_per_node="gres/socket:1"
               |          + GRES_ENFORCE_BIND で実現可能（詳細は 8-5 参照）
               |
              NO  --> [コア指定モード (spec 2-3(1)(a) 後半)]
                       [max_nodes = 1]
                       1ノードに全 p タスクを収める
                       ※ キュー排他はパーティション OverSubscribe=NO に委任
               |
               v
             DONE → Slurm スケジューラへ

END
```

---

## 8. 実装上の注意点・制約

### 8-1. C と M の取得方法

`c' = ⌈(m/M)×C⌉` の計算に必要な C（ノードあたりコア数）と M（ノードあたりメモリ）は、  
job_submit プラグインが slurmctld プロセス内で動作するため、内部 API を直接使って取得する。

```c
/* job_submit プラグインは slurmctld プロセス内で動作するため、
 * 内部 API を直接呼び出すことで RPC オーバーヘッドを回避できる */
#include "src/common/node_conf.h"
#include "src/common/part_record.h"
#include "src/interfaces/gres.h"
#include "src/slurmctld/slurmctld.h"

part_record_t *part_ptr = find_part_record(job_desc->partition);
if (!part_ptr) part_ptr = default_part_loc;

int node_inx = 0;
node_record_t *node_ptr = next_node_bitmap(part_ptr->node_bitmap, &node_inx);

uint32_t node_cpus = node_ptr->cpus_efctv   /* CoreSpec 除外後の実効コア数 */
                   ? node_ptr->cpus_efctv
                   : node_ptr->cpus;
uint64_t node_mem  = node_ptr->real_memory  /* MiB */
                   - node_ptr->mem_spec_limit;
uint32_t gpus      = gres_node_config_cnt(node_ptr->gres_list, "gpu");
uint32_t sockets   = node_ptr->tot_sockets;
```

取得する値と理由:

| フィールド | 説明 |
|---|---|
| `cpus_efctv` | CoreSpec/MemSpec で予約済みコアを除いた実効コア数。公開 API の `node_info_t.cpus` は論理 CPU 数であり `c_eff` 計算には不十分 |
| `real_memory - mem_spec_limit` | MemSpec 予約分を差し引いたジョブが利用可能なメモリ量 (MiB) |
| `gres_node_config_cnt(gres_list, "gpu")` | GPU 数を型安全に取得。公開 API では文字列 (`"gpu:8,nvme:2"` 等) をパースする必要がある |
| `tot_sockets` | D分ノード判定（余りタスクが1ソケットに収まるかの確認）に使用 |

公開 API (`slurm_load_node`) を使わない理由:

- slurmctld 内からの呼び出しは自プロセスへの RPC になり不要なレイテンシが生じる
- `node_info_t` には `cpus_efctv` および型付き `gres_list` がない
- パーティション認識のために追加処理が必要になる

パーティション内にノード種別が混在する場合、`next_node_bitmap()` が返す最初のノードを代表値として使う。  
均一構成を前提とするか、種別ごとにパーティションを分けること（運用要件）。

### 8-2. メモリ上限の委任（MaxMemPerNode）

m の上限チェックはプラグインでは行わず、各パーティションの `slurm.conf` 設定に委任する。

```
PartitionName=compute Nodes=... MaxMemPerNode=<M>
```

CPU をオーバーコミットしない前提では、job_submit プラグインが
`ntasks_per_node = floor(C/c_eff)` を明示的にセットするため、
slurmctld は投入時に per-node memory を正確に計算できる:

```
per-node memory = mem_per_cpu × (cpus_per_task × ntasks_per_node)
               = (m / c_eff) × (c_eff × floor(C / c_eff))
               = m × floor(C / c_eff)
```

`MaxMemPerNode = M`（ノードの実利用可能メモリ）を設定すると、
`m × floor(C/c_eff) > M` のジョブは投入時に即座に拒否される（PENDING にはならない）。

端数ノード（`p mod ntasks_per_node` の余りタスクが乗るノード）はタスク数が少ないため
per-node memory < M となり、常に通過する。

ノードのメモリ容量が不均一なパーティションでは `MaxMemPerNode` を
最小ノードメモリに設定するか、ノード種別ごとにパーティションを分けること。

### 8-3. SPANK プラグインの配置

```
/etc/slurm/plugstack.conf に追記:
  required /usr/lib64/slurm/spank_rsc.so config=/etc/slurm/rsc_defaults.conf
```

- `required` 指定: プラグインがない場合はジョブ拒否（本番環境推奨）
- `config=` 引数: 設定ファイルパスを指定（省略不可）

### 8-4. spec 8-2 対応（不正指定の抑止）

job_submit プラグインで以下を検証・拒否する:
- `--rsc` と `--ntasks`/`--cpus-per-task` 等の併用
- `--rsc` 形式が (1)(2) いずれにも合致しない入力
- GPU指定 (`g=`) と CPU指定 (`p=t=c=m=`) の同時指定

### 8-5. spec 2-3 アロケーションロジックとの関係

#### Slurm 標準機能との対応

| spec 条件 | job_submit でセットするフィールド | Slurm の動作 | 実現性 |
|---|---|---|---|
| 2-3(1)(a) p×c≥C ノード専有 | `ntasks_per_node = floor(C/c_eff)` | Slurm が floor(p/ntasks_per_node) ノードを自動割当 | ✅ |
| 2-3(1)(a) 残り p_r（フルノード追加）| 上記の自動処理 | 最後のノードに p_r タスクを自動配置 | ✅ |
| 2-3(1)(a) 残り p_r（D分ノード）| `tres_per_node="gres/socket:1"` + `GRES_ENFORCE_BIND` + `mem_bind_type\|=MEM_BIND_LOCAL` | 1ソケット分のコアに制約（メモリ量制限は cgroup 委任） | ✅ socket GRES + systemd 管理の疑似デバイス/`gres.conf` 自動生成で実現可能（詳細は下記） |
| 2-3(1)(a) p×c<C コア指定 | `max_nodes = 1` | 1ノードに全プロセスを収める | ✅ |
| 2-3(1)(a) キュー排他ノード | パーティション `OverSubscribe=NO` | 他キューのジョブと共用しないノードを割当 | ✅ パーティション設定に委任 |
| 2-3(1)(b) g 個のG分ノードに各1プロセス | `tres_per_task="gres/gpu:1"`, `cpus_per_task=C/G` | ceil(g/G) ノードを割当、各タスクが1GPU を専有 | ✅ GRES で実現可能 |
| 2-3(1)(c) サブシステムC CPU-only | 2-3(1)(a) と同じフィールド | 2-3(1)(a) と同じ | ✅ |
| 2-3(2)(a) c≤t SMT スレッド割付 | `threads_per_core = t/c`（c==t のとき未設定）+ `OMP_NUM_THREADS` | SMT スレッドをコアに割付 | ✅ 標準 + SPANK |
| 2-3(2)(b) c>t コア間引き割付 | `threads_per_core=1` + `OMP_PROC_BIND=spread` | コアを間引いてスレッドを分散配置 | ✅ 標準 + SPANK |

#### GPU モード: `tres_per_task` と `tres_per_job` の違い

`tres_per_job = "gres/gpu:g"` はジョブ全体の GPU 総数を指定するだけで、
各プロセスに1GPU を割付けることは保証されない。

`tres_per_task = "gres/gpu:1"` を使うとタスクと GPU が1対1に束ねられ、
spec 2-3(1)(b) の「各 G 分ノードに1プロセス」が Slurm 標準機能で実現できる。

```
tres_per_task = "gres/gpu:1"  ← タスクあたり1GPU
num_tasks     = g             ← タスク総数 = GPU 総数
cpus_per_task = C/G           ← G分ノードあたりのコア数

→ Slurm が自動的に ceil(g/G) ノードを割当て
→ 各ノードに最大 G タスク（最後のノードは g mod G タスク）
→ 各タスクが1GPU を専有
```

#### D分ノード (D=2 または D=4): socket/NUMA GRES による実現

spec 2-3(1)(a) の残りプロセス（p_r × c_eff ≤ C/D (= C_D) の場合）を1ソケット分のコア/メモリに
制約するため、socket（または numa）を GRES 疑似デバイスとして定義する。

**前提条件: systemd による topology 依存資材の自動生成**

Slurm の `_set_gres_device_desc()` が `S_ISCHR()` / `S_ISBLK()` でデバイス種別を強制するため、
`/sys` パスや通常ファイルは `File=` に使えない。このため、手作業で `/dev/socket0` などを作るのではなく、
boot 時に systemd の oneshot service で疑似デバイスと `gres.conf` 断片を自動生成する。

固定 major `244` のような未保証の番号を前提にするのは避ける。major は host-local に安全な値を
動的確保し、確保できない場合は fail-closed で生成を中止する。運用上は `/dev/slurm-rsc/` 以下に
専用名前空間を切り、`/dev/slurm-rsc/socket*` と `/dev/slurm-rsc/numa*` を生成する。

補足として、Linux の `Documentation/admin-guide/devices.txt` では character device の
local/experimental 用として `60-63` および `120-127` が挙げられている。ただし本設計では、
それらの範囲の具体値を固定前提にはせず、host-local に安全な番号を選べない場合は fail-closed とする。

**topology の正データ源**

- socket 情報: `/sys/devices/system/cpu/cpu*/topology/physical_package_id`
- NUMA 情報: `/sys/devices/system/node/node*/cpulist`
- `numactl --hardware` や `lscpu` は確認用であり、生成処理の主入力にはしない

**systemd による生成フロー**

1. `slurm-rsc-gres-setup.service` を `slurmd.service` より前に実行する
2. helper script（例: `rsc-gres-topology-gen`）が sysfs を走査する
3. socket ごと・NUMA node ごとに CPU 集合を算出する
4. `/dev/slurm-rsc/socketN` / `/dev/slurm-rsc/numaN` を生成する
5. `/etc/slurm/gres.conf.d/rsc-topology.conf` を再生成する
6. stale な疑似デバイス・旧生成断片は先に削除してから再作成する

helper は topology が欠損・矛盾・major 競合時に非 0 で終了し、partial な `gres.conf` を出さない。

**生成される `gres.conf` 断片の例**

2 ソケット、1 ノードあたり C=64 の場合:

```ini
# /etc/slurm/gres.conf.d/rsc-topology.conf
NodeName=node[01-N] Name=socket File=/dev/slurm-rsc/socket0 Cores=0-31
NodeName=node[01-N] Name=socket File=/dev/slurm-rsc/socket1 Cores=32-63
```

4 NUMA node、C=64 の場合:

```ini
NodeName=node[01-N] Name=numa File=/dev/slurm-rsc/numa0 Cores=0-15
NodeName=node[01-N] Name=numa File=/dev/slurm-rsc/numa1 Cores=16-31
NodeName=node[01-N] Name=numa File=/dev/slurm-rsc/numa2 Cores=32-47
NodeName=node[01-N] Name=numa File=/dev/slurm-rsc/numa3 Cores=48-63
```

site 設定として `slurm.conf` に `GresTypes=socket,numa` あるいは利用する側だけを追加する。
生成処理自体は socket/numa の両方を出せるようにしておき、どちらを有効化するかは site の方針で選ぶ。

将来的には `slurm-jobsubmit-rsc` に以下を同梱する前提とする。

- helper script: `rsc-gres-topology-gen`
- systemd unit: `slurm-rsc-gres-setup.service`

ただし、生成済みの `/etc/slurm/gres.conf.d/rsc-topology.conf` 自体は package payload ではなく runtime state として扱う。

**job_submit でのD分ノード割当（p_r プロセス末尾ノード処理）**

```c
uint32_t n_tpn = node_cpus / c_eff;
uint32_t p_r   = req->p % n_tpn;             // 余りプロセス数
if (sockets_per_node > 1) {
    uint32_t cpus_per_socket = node_cpus / sockets_per_node;  // C/D
    if (p_r > 0 && (uint64_t) p_r * c_eff <= cpus_per_socket) {
        // D分ノード: 1ソケット（NUMA ドメイン）分のコアに制約
        xfree(job->tres_per_node);
        job->tres_per_node = xstrdup("gres/socket:1");
        job->bitflags |= GRES_ENFORCE_BIND;   // Cores= cpuset を強制
        job->mem_bind_type |= MEM_BIND_LOCAL;  // NUMA ローカルメモリ優先ヒント
        // pn_min_memory は変更しない:
        //   M/D 換算すると tres_per_node がジョブ全体に適用されるため
        //   フルノードでもメモリ要求超過 → スケジュール不能になる
        //   メモリ量の強制制限は cgroup ConstrainRAMSpace=yes に委ねる
    }
}
```

**動作**

- `gres/socket:1` を要求 → 空きソケット GRES が1つあるノードが選択される
- `GRES_ENFORCE_BIND` により、自動生成された `Cores=` cpus_bitmap がジョブの cpuset に適用される
- `MEM_BIND_LOCAL` フラグにより、Slurm が cpuset と同 NUMA ドメインのメモリを優先使用するヒントを設定する
- `pn_min_memory` は変更しない（M/D 換算すると `tres_per_node` がジョブ全体に適用されるためフルノードでもメモリ要求超過 → スケジュール不能）
- メモリ量の強制制限は `cgroup.conf` の `ConstrainRAMSpace=yes` に委ねる（ベストエフォート）

**制約**

- 各ノードで systemd による疑似デバイス/`gres.conf` 自動生成が必要
- メモリを NUMA ドメインに厳密に制限するには cgroup の `ConstrainRAMSpace=yes` が別途必要
- `CountOnly` フラグでは `Cores=` バインドが機能しないため必ず `File=` を指定すること
- sysfs から socket/NUMA 拓扑が正しく読めないノードでは helper を fail させ、`slurmd` 起動を止める方が安全

#### 具体例: --rsc パターン別 job_descriptor フィールド値

以下のシステム前提でパターンごとの設定値を示す（node_count_mode = exact）。

| パラメータ | 値 |
|---|---|
| C（コア/ノード） | 64 |
| M（メモリ/ノード） | 262144 MiB（256 GiB） |
| G（GPU/ノード） | 4 |
| D（ソケット/ノード） | 2、C_D = 32 |
| m_default | 4096 MiB/コア |

m は省略（c × m_default を使用）、c_eff = c とする。

---

**パターン A: `--rsc p=128,c=1`**（ノード専有、端数なし）  
p×c = 128 ≥ C=64、p_r = 0 → フルノード × 2

| フィールド | 値 |
|---|---|
| `num_tasks` | 128 |
| `cpus_per_task` | 1 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU |
| `ntasks_per_node` | 64 |
| `min_nodes` | 2 |
| `max_nodes` | 2 |

---

**パターン B: `--rsc p=100,c=1`**（ノード専有、フルノード端数）  
p×c = 100 ≥ C=64、p_r = 36 > C_D=32 → 2ノード目に p_r=36 タスクを配置

| フィールド | 値 |
|---|---|
| `num_tasks` | 100 |
| `cpus_per_task` | 1 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU |
| `ntasks_per_node` | 64 |
| `min_nodes` | 2 |
| `max_nodes` | 2 |

---

**パターン C: `--rsc p=80,c=1`**（ノード専有、D分ノード端数）  
p×c = 80 ≥ C=64、p_r = 16 ≤ C_D=32 → 2ノード目に socket GRES で制約

| フィールド | 値 |
|---|---|
| `num_tasks` | 80 |
| `cpus_per_task` | 1 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU（標準値のまま） |
| `ntasks_per_node` | 64 |
| `min_nodes` | 2 |
| `max_nodes` | 2 |
| `tres_per_node` | `"gres/socket:1"` |
| `bitflags` | `\| GRES_ENFORCE_BIND` |

> ⚠️ `tres_per_node` はジョブ全体に適用されるため、1ノード目（フルノード）にも
> socket GRES が要求される既知の設計制約がある。  
> `pn_min_memory` を M/D 換算値（131072 MiB/CPU）に変更すると
> フルノードのメモリ要求が超過してスケジュール不能になるため、
> D分ノードのメモリ制限は `cgroup.conf` の `ConstrainRAMSpace=yes` に委ねる（ベストエフォート）。

---

**パターン D: `--rsc p=16,c=2`**（コア指定モード）  
p×c = 32 < C=64 → 1ノードに収める

| フィールド | 値 |
|---|---|
| `num_tasks` | 16 |
| `cpus_per_task` | 2 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU（= m/c_eff = 8192/2） |
| `max_nodes` | 1 |

---

**パターン E: `--rsc g=8`**（GPU モード）  
G=4、g=8 → ceil(8/4)=2 ノード、各タスクが1GPU を専有

| フィールド | 値 |
|---|---|
| `num_tasks` | 8 |
| `cpus_per_task` | 16（= C/G = 64/4） |
| `tres_per_task` | `"gres/gpu:1"` |

---

**パターン F: `--rsc p=16,c=1,t=2`**（SMT、c≤t）  
p×c = 16 < C=64 → コア指定モード、各コア2スレッド

| フィールド | 値 |
|---|---|
| `num_tasks` | 16 |
| `cpus_per_task` | 1 |
| `threads_per_core` | 2 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU |
| `max_nodes` | 1 |
| `OMP_NUM_THREADS`（SPANK） | 2 |
| `OMP_PROC_BIND`（SPANK） | `close` |
| `OMP_PLACES`（SPANK） | `threads` |

---

**パターン G: `--rsc p=8,c=4,t=1`**（コア間引き、c>t）  
p×c = 32 < C=64 → コア指定モード、各コア1スレッドのみ使用

| フィールド | 値 |
|---|---|
| `num_tasks` | 8 |
| `cpus_per_task` | 4 |
| `threads_per_core` | 1 |
| `pn_min_memory` | 4096 \| MEM_PER_CPU（= m/c_eff = 16384/4） |
| `max_nodes` | 1 |
| `OMP_NUM_THREADS`（SPANK） | 1 |
| `OMP_PROC_BIND`（SPANK） | `spread` |
| `OMP_PLACES`（SPANK） | `cores` |

### 8-6. スレッドアフィニティ（spec 6-10）

`t` の値は `OMP_NUM_THREADS` として job prolog または  
SPANK `slurm_spank_user_init()` でセットする。

REMOTE コンテキスト（slurmstepd）で実行される。
`SPANK_RSC_*`（job control env 由来、Slurm が SPANK_ を付加した名前）を
`spank_getenv()` で読み取り、`spank_setenv()` でユーザジョブ環境変数 `SLURM_RSC_*` にコピーする。

```c
int slurm_spank_user_init(spank_t sp, int ac, char **av)
{
    char gbuf[32];
    char c_eff_buf[32];

    // GPU モードか確認。SPANK_RSC_G が存在すれば GPU モード
    _copy_control_to_job_env(sp, RSC_ENV_SPEC, "SLURM_RSC_SPEC", 1);
    if (spank_getenv(sp, RSC_ENV_G, gbuf, sizeof(gbuf)) == ESPANK_SUCCESS) {
        _copy_control_to_job_env(sp, RSC_ENV_G, "SLURM_RSC_G", 1);
        return 0;  // GPU モード: OMP_* と SLURM_RSC_P/T/C/M は不要
    }

    // CPU モード: 全変数をジョブ環境にコピー
    _copy_control_to_job_env(sp, RSC_ENV_P, "SLURM_RSC_P", 1);
    _copy_control_to_job_env(sp, RSC_ENV_T, "SLURM_RSC_T", 1);
    _copy_control_to_job_env(sp, RSC_ENV_C, "SLURM_RSC_C", 1);
    _copy_control_to_job_env(sp, RSC_ENV_M, "SLURM_RSC_M", 1);

    // SLURM_RSC_C_EFF: job_submit が cpus_per_task = c_eff をセット済み
    // → Slurm が SLURM_CPUS_PER_TASK に自動反映するのでそのまま転写
    if (spank_getenv(sp, "SLURM_CPUS_PER_TASK", c_eff_buf, sizeof(c_eff_buf)) == ESPANK_SUCCESS)
        spank_setenv(sp, "SLURM_RSC_C_EFF", c_eff_buf, 1);

    // OMP デフォルト設定（ユーザが設定済みの場合は上書きしない）
    // t を OMP_NUM_THREADS に、c と t の関係に応じて OMP_PROC_BIND / OMP_PLACES をセット
    // GPU モード時はこのブロック全体をスキップ済み（上で return）
    _set_omp_defaults(sp);

    return 0;
}
```

`_copy_control_to_job_env(sp, src, dst, overwrite)` は
`spank_getenv(sp, src, ...)` で SPANK_RSC_* を読み取り
`spank_setenv(sp, dst, ...)` で SLURM_RSC_* にコピーするヘルパー。

---

## 9. 参考ファイル一覧

| 用途 | ファイルパス |
|---|---|
| SPANK公開ヘッダ | `slurm-25.11.4/slurm/spank.h` |
| SPANK内部実装 | `slurm-25.11.4/src/common/spank.c` |
| SPANKプラグイン例 | `slurm-25.11.4/src/plugins/job_submit/pbs/spank_pbs.c` |
| job_desc構造体 | `slurm-25.11.4/slurm/slurm.h`（`job_descriptor`） |
| sbatchオプション解析 | `slurm-25.11.4/src/sbatch/opt.c` |
| Luaジョブ投入例 | `slurm-25.11.4/contribs/lua/job_submit.lua` |
| SPANK Webドキュメント | https://slurm.schedmd.com/spank.html |
| 仕様書 | `specs/slurm-sched-spec.new.md`（項目 2-1〜2-4、8-2、8-4） |
| 関連調査ノート | `notes/slurm-impl-compare-2-3-1ab.md` |
