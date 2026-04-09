# 2-3(1)(a)/(b) 一本化に向けた Slurm 実装比較（job_submit中心・NUMA前提・25.11.4実コード評価）

対象: `specs/slurm-sched-spec.new.md` の `2-3(1)(a)` と `2-3(1)(b)`  
評価軸: **運用実装コスト（設定量、実装量、保守負荷）**

## 1. 前提の再定義（2分/4分ノード）

- `2分ノード`: 1ノード内の **NUMAノード単位** を割当単位として扱う。
- `4分ノード`: 1ノード内を4つの NUMA 相当単位として扱う（ハード構成依存）。
- Slurm上では NUMA ノードを Socket 相当として扱う意図。

## 2. Slurm 25.11.4 での実装基盤（実コード確認）

### 2.1 NUMAをSocket扱いに寄せる入口
- `src/slurmd/common/xcpuinfo.c`
  - `numa_node_as_socket` 分岐あり（`objtype[SOCKET]` を NUMA 親タイプへマップ）。
  - 既に「NUMAをSocket相当として扱う」導線は存在。

### 2.2 cpuset/cgroup 制御の実装位置
- `src/plugins/task/cgroup/task_cgroup_cpuset.c`
  - `task_cgroup_cpuset_create()` が `allow_cores` / `allow_mems` を `cgroup_g_constrain_set()` へ設定。
- `src/interfaces/cgroup.h`
  - `cgroup_limits_t` に `allow_cores` / `allow_mems` が定義済み。
- `src/plugins/cgroup/v2/cgroup_v2.c` および `src/plugins/cgroup/v1/cgroup_v1.c`
  - `cgroup_p_constrain_set()` 経由で cpuset 制約を反映。

### 2.3 NUMA/ldom バインドの実装位置
- `src/plugins/task/affinity/affinity.c`
  - `_bind_ldom()` と `CPU_BIND_LDMAP/LDMASK/LDRANK` 系処理あり。
- `src/plugins/task/affinity/numa.c`
  - `slurm_get_numa_node()` と `mem_bind` 系処理あり。
- `src/plugins/task/affinity/dist_tasks.c`
  - `CPU_BIND_TO_LDOMS` のマスク整形処理あり。

### 2.4 割当判定の本体
- `src/plugins/select/cons_tres/job_test.c`
  - 割当可否判定の主要処理。
  - NUMAを第一級単位にするにはこの層の拡張が必要。

## 3. 2-3(1)(a)/(b) と実装難易度（具体）

### 要件
- `2-3(1)(a)`（B-T）: `c > C2` ならノード中心、`c <= C2` なら2分ノード中心。
- `2-3(1)(b)`（B-F）: `p*c >= C` ならノード中心、`p*c < C` なら同一ノード内コア割当。

### 実装容易性
- `2-3(1)(b)` は現行 `cons_tres + job_submit` で自然に実装しやすい。
- `2-3(1)(a)` は「2分/4分ノード」を厳密な割当単位にするほど、`task/cgroup` と `select` の改修が必要。

## 4. 本格改修の難易度（Tier別）

## Tier 1: job_submit + affinity運用強化（低侵襲）
- 目的: 既存実装を使い、NUMA寄り運用を強制。
- 改修対象:
  - `job_submit.lua`（サイト実装）
  - 運用テンプレート（`--cpu-bind=ldom`, `--mem-bind=local`）
- コード改修範囲: Slurm本体ほぼ無し。
- 難易度: 低。
- 推定工数: 3-7人日。
- リスク: 厳密保証は弱い（運用依存）。

## Tier 2: task/cgroup + task/affinity の低侵襲改修（中侵襲）
- 目的: NUMA境界を跨がない cpuset/mems 制御を強化。
- 改修対象（本体）:
  - `src/plugins/task/cgroup/task_cgroup_cpuset.c`
  - `src/plugins/task/affinity/{affinity.c,numa.c,dist_tasks.c}`
  - 必要に応じ `src/slurmd/common/xcpuinfo.c`（NUMA情報の露出強化）
- 変更内容:
  - `task_cgroup_cpuset_create()` で step 単位 `allow_cores`/`allow_mems` を NUMA単位で生成。
  - `ldom` 由来のCPU/NUMA対応テーブルを使い、2分/4分ノード境界を強制。
- 難易度: 中。
- 推定工数: 20-40人日。
- リスク:
  - cgroup v1/v2 差分対応。
  - 混在NUMA構成ノードでの検証負荷。

## Tier 3: select/cons_tres に NUMA粒度割当を追加（本格）
- 目的: NUMA（2分/4分）をスケジューラの第一級単位として扱う。
- 改修対象（本体）:
  - `src/plugins/select/cons_tres/job_test.c`
  - `src/plugins/select/cons_tres/{node_data.c,cons_helpers.c,dist_tasks.c}`
  - 必要に応じ `src/common/node_conf.c` 周辺
- 変更内容:
  - `c` または `p*c` 分岐に応じて NUMA粒度割当ポリシーを追加。
  - 2分/4分ノード要求を内部表現へ落とし、可否判定・配置・競合解消まで拡張。
- 難易度: 高。
- 推定工数: 60-120人日。
- リスク:
  - 改修面積が大きく、上流差分追従が重い。
  - 既存 `CR_Core_Memory` 運用への回帰リスク。

## 5. 4分ノード対応の実現可能性

- **可能性はある**。ただし前提条件が厳しい。
- 必要条件:
  - ノードごとの NUMA分割情報が安定して取得できること。
  - 4分単位を `ldom` マッピングへ正確に反映できること。
- 現実的には:
  - Tier 2でPoCを行い、4分境界の cpuset/mems 強制が再現できるか先に確認。
  - 成功した場合のみ Tier 3 を検討。

## 6. 推奨ロードマップ

1. まず Tier 1 で運用評価（短期）。
2. 次に Tier 2 のPoCで「NUMA単位cgroup制御の厳密性」を実証（中期）。
3. Tier 2 で要件未達なら Tier 3 を着手判断（長期）。

## 7. 受け入れ判定（PoC時）

- 2分ノード要求時、`cpuset.cpus` と `cpuset.mems` が同一NUMA単位に収まる。
- 4分ノード要求時、対象NUMA群以外へCPU/memが漏れない。
- `c > C2` / `c <= C2`、および `p*c` 閾値で挙動差が再現できる。

## 8. 参考情報

- slurm.conf (`numa_node_as_socket`, `TaskPlugin`): https://slurm.schedmd.com/slurm.conf.html
- cgroup.conf (`ConstrainCores`): https://slurm.schedmd.com/cgroup.conf.html
- cgroup plugins overview: https://slurm.schedmd.com/cgroups.html
- select/cons_tres: https://slurm.schedmd.com/cons_tres.html
- CPU management: https://slurm.schedmd.com/cpu_management.html
- multi-core/NUMA binding (`ldom`): https://slurm.schedmd.com/mc_support.html
- srun (`--cpu-bind`, `--mem-bind`): https://slurm.schedmd.com/srun.html
