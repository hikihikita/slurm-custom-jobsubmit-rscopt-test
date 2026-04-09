# Slurm サービスコース別 投入制御・account/QOS 運用設計

## 1. 目的と前提

本書は、Slurm サービスにおけるコース別運用について、次を一体で管理するための設計指針を示す。

- ジョブ投入制御（`partition` / `account` / `job_submit`）
- 実行ポリシー制御（`QOS` / `DefaultQOS`）
- フェアシェア配分（`association(account)`）
- 入力簡素化と運用自動化（`cli_filter` / 自動生成設定）

対象コースは次の 5 つとする。

- エントリコース
- パーソナルコース
- グループコース
- 複数課題グループコース
- 大規模ジョブコース

前提:

- 共通前提: account は利用主体（個人または研究課題/研究グループ）を識別する単位とし、フェアシェアは account 単位で管理する。
- 共通前提: account と QOS は 1 対 1 を基本とし、`DefaultQOS` を正として適用する。
- 共通前提: `--qos` の手動指定は原則禁止し、`job_submit` と `cli_filter` で運用ポリシーを担保する。
- エントリコース: 全ユーザが所属する。コース共通ジョブキューを 1 つ持ち、共通 account（`svc_entry_all`）で運用する。シェア値は最小固定値とする。
- パーソナルコース: コース共通ジョブキューを 1 つ持つ。シェア制御は個人単位で行い、利用可能リソース量とシェア配分は固定値とする。
- グループコース: 研究グループごとに専用ジョブキューを持つ。シェア値は契約済み権利（主にノード数）に比例させる。標準運用は「1 グループ : 1 ジョブキュー : 1 account」を基本とする。
- 複数課題グループコース: 複数研究課題が 1 つのジョブキューを共有する。課題ごとに account を分け、ジョブキューのシェア値を課題間に配分する。
- 大規模ジョブコース: 既存コース利用者が期間限定で大規模リソースを追加利用する。専有 partition、または共通 partition の期間専有で運用する。大規模ジョブ向けのシェア値を別途配分し、共通キュー利用時は投入権限を都度制御する。

本書の運用原則:

- サイズ制御は原則 `QOS` で管理し、`partition` は物理/用途境界の制御に限定する。
- 未知・過大・不整合ジョブは、`job_submit` で fail-close で拒否する。
- `group_shared` のみ `-A svc_project_pr*` 明示指定を必須とし、他コースは可能な限り入力省略を許可する。

## 2. account 階層設計

account は次のように階層化する。

```text
root
├─ svc_entry
│  └─ svc_entry_all           ← entry 全ユーザ共通
├─ svc_personal
│  ├─ svc_personal_u1001      ← ユーザ名 u1001 の個人 account
│  ├─ svc_personal_u1002
│  └─ svc_personal_u1003
├─ svc_group
│  ├─ svc_group_gr10001       ← グループ名 gr10001 の account
│  └─ svc_group_gr10002
├─ svc_project                ← 複数課題グループコース
│  ├─ svc_project_pr20001     ← 課題 account（管理採番）
│  └─ svc_project_pr20002
└─ svc_largejob
   ├─ svc_largejob_lj30001    ← 大規模ジョブ申請 account（管理採番）
   └─ svc_largejob_lj30002
```

設計意図:

- `svc_entry`, `svc_personal`, `svc_group`, `svc_project`, `svc_largejob` はサービスコースの親 account。
- 命名規則: 親名を継承して `_` でつなぐ 3 セグメント統一（`svc_<コース>_<ID>`）。
- エントリは共通 account（`svc_entry_all`）で、パーソナルはユーザ名ベースの account（`svc_personal_<username>`）で fairshare を制御。
- グループは、研究グループ account（`svc_group_<groupname>`）を fairshare 制御の主体にし、ユーザはその account に関連付けて運用。
- 複数課題グループは、課題単位 account（`svc_project_pr*`）を fairshare 制御の主体にし、複数課題が同一 partition を共有できる形で運用する。
- 大規模ジョブは、申請単位 account（`svc_largejob_lj*`）を期間限定で作成し、利用期間終了後に無効化または削除する。

## 3. ジョブキュー（partition）と account の対応

対応方針:

- `entry` partition: `svc_entry_all` のみ許可
- `personal` partition: 個人 account（`svc_personal_<username>`）を許可
- `group_<lab>` partition: 対応する研究グループ account（例: `svc_group_<groupname>`）を許可
- `group_shared` partition: 課題 account（例: `svc_project_pr20001`, `svc_project_pr20002`）を許可
- `largejob` partition: 申請 account（例: `svc_largejob_lj30001`）を許可

実装上は、partition 設定と `job_submit` プラグインの両方で整合を担保する。

- partition 側: 許可 account 範囲を制限
- `job_submit` 側: `partition` と `--account` の組み合わせを検証し、不正投入を拒否（または規程に沿って補正）
- QOS は account に紐づく `DefaultQOS` を正として自動適用し、ユーザの `--qos` 指定は原則不要とする。

### 3.1 job_submit による account 自動補完（`--account` 未指定時）

`--account` 未指定時は、次の優先順で account を補完する。

1. `largejob` partition:
   - 管理定義の明示マップ（`partition -> account`）で申請 account（`svc_largejob_lj*`）を補完
2. `group_*` partition:
   - 管理定義の明示マップ（`partition -> account`）を最優先
   - マップに無い場合のみ機械規則 `group_<groupname> -> svc_group_<groupname>` を試行
3. `group_shared` partition:
   - 複数課題グループでは account が一意に決まらないため、自動補完しない（`-A svc_project_pr*` 明示を要求）
4. `personal` partition:
   - `svc_personal_<username>` を試行
5. `entry` partition:
   - 固定 account（`svc_entry_all`）を使用

補完後は、許可 account と許可 QOS の整合性を検証する。

- 整合すれば投入継続
- 補完不能または整合性不一致なら投入拒否（`--account` 明示指定を要求）
- `--qos` 未指定時は、補完または明示された account に紐づく `DefaultQOS` を自動適用する。

補足:

- `--account` が明示されている場合は、その値を優先し、補完は行わない。
- `DefaultAccount` は管理補助として保持してよいが、本運用では上記補完規則を優先する。
- 投入権限制御の詳細設計と運用手順は、次章「4. ジョブ投入権限制御」に従う。

## 4. ジョブ投入権限制御

### 4.1 制御レイヤと責務分担

- partition 設定: `AllowAccounts` 相当の制限で、投入可能 account 範囲をコース単位で絞る。
- `job_submit` プラグイン: `partition` / `--account` の整合性を検証し、未指定時は規則に従って補完する。QOS は `DefaultQOS` を正として自動適用する。
- accounting 側: `AccountingStorageEnforce=associations,qos` を有効化し、許可外 account/QOS の投入を拒否する。
- 運用原則: 権限制御の主軸は account ベースで行い、UNIX グループ依存の制御は補助用途に留める。

### 4.2 コース別の投入許可ルール

- `entry` partition: `svc_entry_all` のみ許可。
- `personal` partition: `svc_personal` 配下の account（例: `svc_personal_u1001`）のみ許可。
- `group_<lab>` partition: 対応する `svc_group_<groupname>` account（例: `svc_group_gr10001`）のみ許可。
- `group_shared` partition: 課題 account（`svc_project_pr*`、例: `svc_project_pr20001`）のみ許可。`-A svc_project_pr*` を必須とする。
- `largejob` partition: 有効期間中の申請 account（`svc_largejob_lj*`、例: `svc_largejob_lj30001`）のみ許可。
- `--account` 明示時も上記ルールを満たさない場合は reject する。
- `entry` partition では `--account=svc_entry_all` 以外を reject する。
- `--qos` は原則指定不要とし、手動指定された場合は reject する。

### 4.3 期間限定権限（largejob）の運用手順

- 申請承認時:
  - `svc_largejob` 配下に申請単位 account（`svc_largejob_lj*`）を作成する。
  - 対象ユーザへ `svc_largejob_lj*` account を付与する。
  - `largejob` partition 側の許可 account に `svc_largejob_lj*` を追加する。
- 利用期間中:
  - `squeue`/`sacct` で `largejob` 利用ジョブの `account` と `qos` を監査する。
- 期間終了時:
  - `largejob` partition から `svc_largejob_lj*` を除外する。
  - ユーザの `svc_largejob_lj*` 関連付けを解除する。
  - `svc_largejob_lj*` account を無効化または削除する。
- 棚卸し:
  - 期限切れ `svc_largejob_lj*` が残っていないかを定期点検し、失効漏れを防止する。

### 4.4 拒否条件とエラーメッセージ方針

- reject 条件:
  - partition に対して許可されていない account が指定された場合。
  - account と QOS の組み合わせが許可ポリシーに一致しない場合。
  - `--account` 未指定かつ補完不能な場合。
  - `--qos` が明示指定された場合（手動指定禁止ポリシー）。
- 利用者向けエラー方針:
  - どの `partition/account/qos` が不整合かを明示する。
  - `--account` 明示指定で再実行する手順、または管理者への申請導線を案内する。
  - `--qos` 指定が原因の場合は、`--qos` を外して再実行する手順を案内する。

### 4.5 監査と定期チェック

- 日次監査:
  - `squeue -o` で `partition/account/qos` の組み合わせを確認し、コース外投入を検出する。
- 週次監査:
  - `sacct` で `largejob` の期間外利用有無を確認する。
  - `sshare` で account ごとの usage/fairshare を確認し、権限と配分の乖離を検出する。
- 定期レビュー:
  - `partition -> allowed accounts` の管理台帳と実設定の差分を照合する。
  - 失効予定の `svc_largejob_lj*` を一覧化し、終了処理漏れを防ぐ。

## 5. フェアシェア配分ルール

### 5.1 エントリ/パーソナル（固定配分）

- コースごとの総リソース量は固定。
- 各ユーザ account の `Fairshare` を固定値で設定する。
- 配分の例:
  - エントリ: `svc_entry` に `Fairshare=1`
  - パーソナル: 契約プランに応じて `Fairshare=1,2,4` などの固定段階値

### 5.2 グループ（権利比例配分）

- 研究グループのシェア値は、申込済み権利ノード数に比例させる。
- 例: `share_g = k * nodes_g`
  - `nodes_g`: 研究グループ g の利用可能権利ノード数
  - `k`: 全体スケーリング係数（例: 10）

例:

- `svc_group_gr10001`: 4 ノード契約 -> `Fairshare=40`
- `svc_group_gr10002`: 8 ノード契約 -> `Fairshare=80`

## 6. QOS 設計（具体例）

| QOS | 対象コース | 用途 | 例: 優先度/代表制限 |
|---|---|---|---|
| `entry_qos` | エントリ | 低コスト共用 | `Priority` 低め、`MaxWallDurationPerJob` 短め、`GrpTRES=node=固定枠` |
| `personal_qos` | パーソナル | 個人向け共用 | `Priority` 中、`MaxTRESPerJob` 中程度、`GrpTRES=node=固定枠` |
| `group_qos` | グループ | 研究グループ専用 | `Priority` 中〜高、`GrpTRES=node=契約比例`、`MaxWallDurationPerJob` 長め |

補足:

- グループコースは、`association` 側の `Fairshare` と `QOS` 側の `GrpTRES` を併用して、権利比例を二重に担保できる。
- 厳格運用では `AccountingStorageEnforce=associations,qos` を設定し、許可外の account/QOS を拒否する。
- account と QOS は 1 対 1 で運用し、ユーザが QOS を選択しない前提とする。

### 6.1 よく使う QOS 制限項目（実務向け）

| 項目 | 何を制御するか | よく使う目的 |
|---|---|---|
| `Priority` | スケジューラの優先度係数 | コース間の基準優先度を調整する |
| `MaxWallDurationPerJob` | ジョブ1本あたりの最大実行時間 | 長時間ジョブの抑制、キュー滞留の抑制 |
| `MaxTRESPerJob` | ジョブ1本あたりの TRES 上限（`cpu`,`mem`,`node`,`gres/gpu` など） | 過大ジョブの投入防止 |
| `GrpTRES` | その QOS 全体で同時利用可能な TRES 上限 | コース全体の同時利用枠を制御 |
| `MaxJobsPerUser` | ユーザごとの同時実行ジョブ上限 | 1ユーザによる占有を抑制 |
| `MaxSubmitJobsPerUser` | ユーザごとの同時投入（pending+running）上限 | 大量投入によるキュー圧迫を抑制 |
| `MaxJobsPerAccount` | account ごとの同時実行ジョブ上限 | 研究課題/研究グループ単位の占有を抑制 |
| `MaxTRESPerAccount` | account ごとの同時利用 TRES 上限 | account 単位の資源枠を明確に制御 |
| `Flags=DenyOnLimit` | 上限超過ジョブの投入時拒否 | 未知/過大ジョブを即時 reject する |

### 6.2 項目の使い分け

- 時間上限は `MaxWallDurationPerJob` を基本としてコースごとに調整する。
- ジョブサイズ上限は `MaxTRESPerJob`（1ジョブ）で制御し、コース枠は `GrpTRES`（QOS全体）で制御する。
- 瞬間的な混雑は `MaxJobsPerUser` / `MaxSubmitJobsPerUser`（ユーザ単位）と `MaxJobsPerAccount`（account単位）で平準化する。
- account の同時資源利用枠は `MaxTRESPerAccount` を使って制御できる。
- 「投入時に確実に止める」必要があるコースでは `Flags=DenyOnLimit` を有効化する。

### 6.3 コース別の推奨設定パターン

- `entry_qos`
  - `MaxWallDurationPerJob`: 短め（短時間ジョブ中心）
  - `MaxTRESPerJob`: 小さめ
  - `Flags=DenyOnLimit`: 有効
- `personal_qos`
  - `MaxWallDurationPerJob`: 中程度
  - `MaxTRESPerJob`: 中程度
  - `MaxJobsPerUser`: entry より緩め
- `group_qos`
  - `GrpTRES`: 契約比例で管理
  - `MaxWallDurationPerJob`: 研究ジョブ要件に応じて長め
  - `MaxTRESPerJob`: 契約超過を防ぐ値で設定

## 7. partition / account / QOS の関係と設定項目

### 7.1 関係整理（責務分離）

- `partition`: ジョブ投入先キューを決める入口。どの account を許可するかを制御する。
- `account`（association）: 利用主体（個人/研究課題/研究グループ）を識別する単位。権限制御と fairshare 配分の主体となる。
- `QOS`: 優先度やリソース制限を定義する実行ポリシー。`DefaultQOS` を通じて account に紐づける。
- `job_submit`: `partition -> account` 補完と `DefaultQOS` 自動適用、整合性検証（不整合時 reject）を担う。

### 7.2 関係マトリクス

| 利用区分 | 主入力（ユーザ） | 決定される account | 決定される QOS | 主な reject 条件 |
|---|---|---|---|---|
| `entry` | `-p entry`（`-A`省略可） | `svc_entry_all` | `DefaultQOS(svc_entry_all)` | `--account` が `svc_entry_all` 以外、`--qos` 手動指定 |
| `personal` | `-p personal`（`-A`省略可） | `svc_personal_<username>`（存在時） | `DefaultQOS(svc_personal_<username>)` | account 未作成、許可外 account、`--qos` 手動指定 |
| `group_<lab>` | `-p group_<lab>`（`-A`省略可） | `svc_group_<groupname>`（明示マップ優先） | `DefaultQOS(svc_group_<groupname>)` | 許可外 account、`--qos` 手動指定 |
| `group_shared` | `-p group_shared -A svc_project_pr*`（`-A`必須） | 指定された `svc_project_pr*` | `DefaultQOS(svc_project_pr*)` | `-A` 未指定、許可外 `svc_project_pr*`、`--qos` 手動指定 |
| `largejob` | `-p largejob`（条件により `-A` 要） | 有効期間中の `svc_largejob_lj*` | `DefaultQOS(svc_largejob_lj*)` | 期限切れ/未許可 `svc_largejob_lj*`、`--qos` 手動指定 |

### 7.3 設定項目一覧（オブジェクト別）

| オブジェクト | 主な設定項目 | 目的/効果 | 主な設定場所・手段 |
|---|---|---|---|
| `partition` | 許可 account 範囲（`AllowAccounts` 相当） | 投入可能な account の入口制御 | `slurm.conf` の partition 定義 |
| `account` | 階層（親子）、`Fairshare`、`DefaultQOS` | 配分主体・優先度基盤・既定QOSの決定 | `sacctmgr add/modify account` |
| `user association` | `DefaultAccount`、所属 account | `-A` 省略時の補助、利用可能 account の制御 | `sacctmgr add/modify user` |
| `QOS` | `Priority`, `GrpTRES` など | 実行優先度・資源上限の定義 | `sacctmgr add/modify qos` |
| `enforce` | `AccountingStorageEnforce=associations,qos` | 許可外 account/QOS の実行拒否 | `slurm.conf` |
| `cli_filter` | `partition_to_account`, `user_allowed_accounts` | 入力補助、曖昧入力の早期検出 | `/etc/slurm/accounting_policy_data.lua`（自動生成） |

### 7.4 最小設定チェックリスト

- 新しい partition を追加したら、対応する許可 account 範囲を定義する。
- 追加した account に `DefaultQOS` を必ず設定する。
- 必要な `QOS`（`Priority`, `GrpTRES`）を作成・更新する。
- `job_submit` 補完規則（`partition -> account`）を更新する。
- `group_shared` を使う場合は、`user_allowed_accounts`（`svc_project_pr*` 候補）を更新する。
- `sshare` / `squeue -o` / `sacct` で `partition-account-qos` の整合を監査する。

### 7.5 ジョブサイズ制御ポリシー（どこで制御するか）

方針:

- サイズ制御の主担当は `QOS` とする（CPU/ノード/メモリ/時間上限）。
- `partition` は物理・用途の粗い境界（ノード群、専有用途、GPU有無など）に限定する。
- `job_submit` は最終ガードとして、未知入力や不整合を fail-close で reject する。

#### 7.5.1 制御責務

| 制御対象 | 主担当 | 理由 |
|---|---|---|
| ジョブサイズ上限（CPU/ノード/メモリ/時間） | `QOS` | `sacctmgr` で更新でき、`slurm.conf` 配布より運用負荷が低い |
| キュー境界（どのノード群で実行可能か） | `partition` | 物理境界は partition が最も明確 |
| 例外・未知入力の拒否 | `job_submit`（slurmctld側） | 補完不能・ポリシー外を一元的に reject できる |

#### 7.5.2 reject と pending の違い

- `QOS` に `Flags=DenyOnLimit` を設定した場合:
  - `Max/Min` 系上限を超えるジョブは投入時に reject される。
- `Flags=DenyOnLimit` を設定しない場合:
  - 上限超過ジョブが pending になる場合があり、即 reject にならないことがある。
- 本運用では「未知/過大ジョブを投入しない」を優先し、サイズ上限を持つ QOS は `DenyOnLimit` を原則有効化する。

#### 7.5.3 運用コマンド例

```bash
# 例: entry_qos にジョブサイズ上限を設定
sacctmgr modify qos where name=entry_qos \
  set maxtresperjob=cpu=64,node=2,mem=256G
sacctmgr modify qos where name=entry_qos \
  set maxwalldurationperjob=12:00:00

# 上限超過を投入時 reject にする
sacctmgr modify qos where name=entry_qos set flags+=DenyOnLimit

# 設定確認
sacctmgr show qos format=Name,Flags,MaxTRESPerJob,MaxWallDurationPerJob,GrpTRES
```

#### 7.5.4 監査ポイント

- `sacctmgr show qos` で `Flags` と上限値の欠落がないことを確認する。
- `squeue` / `sacct` の reason を確認し、`QOSMax*` / `AssocMax*` が意図どおりに発生しているか監査する。
- サイズ変更は原則 `QOS` 更新で実施し、`partition` 変更は境界変更時のみ行う。

## 8. sbatch/salloc 投入時の初期値設計（accounting と QOS）

### 8.1 初期 `accounting` 値

`sbatch` で `-A/--account` が未指定の場合に備え、ユーザに `DefaultAccount` を設定できる。ただし本運用では、`job_submit` による partition ベース補完を優先する。

- エントリ利用者: `DefaultAccount=svc_entry_all`
- パーソナル利用者: `DefaultAccount=svc_personal_<username>`
- グループ利用者: 主所属の `DefaultAccount=svc_group_<groupname>`（複数所属時は運用規程で主所属を定義）

### 8.2 初期 QOS 値

association または user に `DefaultQOS` を設定する。

- エントリ: `DefaultQOS=entry_qos`
- パーソナル: `DefaultQOS=personal_qos`
- グループ: `DefaultQOS=group_qos`

### 8.3 sbatch/salloc 実行例（`--qos` 原則不要）

明示指定:

```bash
sbatch -p entry -A svc_entry_all job.sh
sbatch -p personal -A svc_personal_u1001 job.sh
sbatch -p group_gr10001 -A svc_group_gr10001 job.sh
salloc -p personal -A svc_personal_u1001
```

省略指定（既定値を利用）:

```bash
sbatch -p entry job.sh
sbatch -p group_gr10001 job.sh
salloc -p entry
```

複数課題グループ（`group_shared`）:

```bash
sbatch -p group_shared -A svc_project_pr20001 job.sh
salloc -p group_shared -A svc_project_pr20002
```

この場合、まず `job_submit` の補完規則が適用される。補完成功時は補完された `account` から QOS が自動決定される。補完失敗時は投入拒否となる。`group_shared` は `-A svc_project_pr*` の明示が必須である。

### 8.4 cli_filter による補助方針

#### 8.4.1 役割と適用範囲

- 対象コマンドは `sbatch` と `salloc` とする。
- `cli_filter` は入力補助と事前検証を担当し、最終的な許可/拒否は `job_submit` が担保する。
- 目的は「`-A/--qos` の入力負担削減」「明らかな不正入力の早期検出」とする。

#### 8.4.2 設定ファイル仕様（自動生成）

`cli_filter` は accounting 用設定ファイル（例: `/etc/slurm/accounting_policy_data.lua`）を参照する。本ファイルは手作業ではなく、次の生成コマンドで自動生成することを原則とする。

```bash
# slurmctld または管理ノード上で実行
scripts/generate-cli-filter-config.sh --out /etc/slurm/accounting_policy_data.lua
```

オフライン生成（事前取得済みデータを利用）:

```bash
scripts/generate-cli-filter-config.sh \
  --assoc-file ./assoc.txt \
  --partitions-file ./partitions.txt \
  --out /etc/slurm/accounting_policy_data.lua
```

生成される設定スキーマ例（Lua テーブル形式、`loadfile()` で読み込む）:

```lua
-- /etc/slurm/accounting_policy_data.lua
-- 自動生成: generate-cli-filter-config.sh
return {
  partition_rules = {
    entry        = { mode = "fixed",    account = "svc_entry_all" },
    personal     = { mode = "template", account_template = "svc_personal_${USER}" },
    group_pattern = {
      mode             = "template_from_partition_suffix",
      partition_regex  = "^group_(.+)$",
      account_template = "svc_group_${MATCH_1}",
    },
    group_shared = { mode = "require_explicit_account" },
    largejob     = { mode = "require_explicit_account" },
  },
  user_allowed_accounts = {
    u1001 = { "svc_project_pr20001", "svc_project_pr20002" },
    u1002 = { "svc_project_pr20001" },
  },
  policy = {
    qos_source        = "default_qos",
    reject_manual_qos = true,
  },
}
```

- `partition_rules`: `partition -> account` の補完規則（固定/テンプレート/明示要求）。
- `user_allowed_accounts`: `group_shared` で `-A` 未指定時に提示する候補。
- `policy.qos_source=default_qos`: QOS決定は `DefaultQOS` を唯一の正とする宣言。
- 更新手順は「生成 -> 構文検証（`lua -e "dofile(...)"`) -> 原子的配置（`mv`）」とする。

#### 8.4.3 判定アルゴリズム

1. `-p/--partition` の指定有無を確認する（未指定はエラー）。
2. `-A/--account` 未指定時:
   - `entry/personal/group/largejob`: 一意補完できる場合のみ補完して継続。
   - `group_shared`: 自動補完せず、`-A p_*` 明示を要求して停止。
3. `--qos` 指定時:
   - 手動指定禁止として停止し、`--qos` を外した再実行を案内する。
4. `--qos` 未指定時:
   - `job_submit` 側で `DefaultQOS` が自動適用される前提で実行継続。

#### 8.4.4 終了コードとエラーメッセージ方針

- 終了コード:
  - `0`: 補完成功または入力が有効で、そのまま実行継続。
  - `1`: エラー（入力不足/曖昧・ポリシー違反）— エラー種別は `slurm.user_msg()` のメッセージで確認。
  - ※ cli_filter Lua プラグインは `slurm.ERROR` を返した場合、sbatch/salloc の終了コードは常に `1`（ハードコード）。
- メッセージ形式:
  - 原因（何が不足/不正か）
  - 補完または修正後の実行例（1行）
  - 必要に応じて候補 account 一覧（`group_shared` のみ）

#### 8.4.5 実行例

成功例:

```bash
sbatch -p entry job.sh
salloc -p group_labA
```

失敗例（`group_shared` で `-A` 未指定）:

```bash
sbatch -p group_shared job.sh
# exit=1
# message: group_shared では -A svc_project_pr* が必須です
# hint: sbatch -p group_shared -A svc_project_pr20001 job.sh
```

失敗例（`--qos` 手動指定）:

```bash
sbatch -p entry -A svc_entry_all --qos=group_qos job.sh
# exit=1
# message: --qos の手動指定は禁止です（DefaultQOS を使用してください）
# hint: sbatch -p entry -A svc_entry_all job.sh
```

#### 8.4.6 運用手順

- 生成・配布:
  - `scripts/generate-cli-filter-config.sh --out /etc/slurm/accounting_policy_data.lua.new` を実行する。
  - 構文検証後に `mv /etc/slurm/accounting_policy_data.lua.new /etc/slurm/accounting_policy_data.lua` で切り替える。
- 設定配布前検証:
  - `partition_to_account` の参照先 account が存在すること。
  - 参照先 account に `DefaultQOS` が設定されていること（`sacctmgr show assoc format=Account,User,DefaultQOS` で確認）。
  - `group_shared` 利用者に `user_allowed_accounts`（`svc_project_pr*` 候補）が定義されていること。
- 日次確認:
  - `cli_filter` の終了コード `1` 件数（slurmctld ログ）を集計し、運用ルール改善に反映する。
- 障害時:
  - `cli_filter` 無効化時も `job_submit` の検証で安全側に失敗することを確認する。

## 9. sacctmgr 設定例

### 9.1 account 作成

```bash
sacctmgr add account svc_entry
sacctmgr add account svc_personal
sacctmgr add account svc_group
sacctmgr add account svc_project
sacctmgr add account svc_largejob

sacctmgr add account svc_entry_all parent=svc_entry

sacctmgr add account svc_personal_u1001 parent=svc_personal
sacctmgr add account svc_personal_u1002 parent=svc_personal
sacctmgr add account svc_personal_u1003 parent=svc_personal

sacctmgr add account svc_group_gr10001 parent=svc_group
sacctmgr add account svc_group_gr10002 parent=svc_group
sacctmgr add account svc_project_pr20001 parent=svc_project
sacctmgr add account svc_project_pr20002 parent=svc_project
sacctmgr add account svc_largejob_lj30001 parent=svc_largejob
sacctmgr add account svc_largejob_lj30002 parent=svc_largejob
```

### 9.2 ユーザ追加と DefaultAccount

```bash
# entry ユーザ
sacctmgr add user alice account=svc_entry_all
sacctmgr modify user where name=alice set defaultaccount=svc_entry_all

# personal ユーザ（ユーザ名 u1001 の場合）
sacctmgr add user u1001 account=svc_personal_u1001
sacctmgr modify user where name=u1001 set defaultaccount=svc_personal_u1001

# group ユーザ（グループ名 gr10001 の場合）
sacctmgr add user u1004 account=svc_group_gr10001
sacctmgr modify user where name=u1004 set defaultaccount=svc_group_gr10001

# project ユーザ
sacctmgr add user u1001 account=svc_project_pr20001
sacctmgr add user u1002 account=svc_project_pr20001
sacctmgr modify user where name=u1001 set defaultaccount=svc_project_pr20001

# largejob ユーザ
sacctmgr add user u1004 account=svc_largejob_lj30002
sacctmgr modify user where name=u1004 set defaultaccount=svc_largejob_lj30002
```

### 9.3 fairshare と DefaultQOS

```bash
# 固定配分の例
sacctmgr modify account where name=svc_entry_all set fairshare=1
sacctmgr modify account where name=svc_personal_u1001 set fairshare=2

# 権利比例の例（k=10）
sacctmgr modify account where name=svc_group_gr10001 set fairshare=40
sacctmgr modify account where name=svc_group_gr10002 set fairshare=80

# DefaultQOS 設定
sacctmgr modify account where name=svc_entry_all set defaultqos=entry_qos
sacctmgr modify account where name=svc_personal set defaultqos=personal_qos
sacctmgr modify account where name=svc_group set defaultqos=group_qos
sacctmgr modify account where name=svc_project set defaultqos=group_qos
sacctmgr modify account where name=svc_largejob set defaultqos=group_qos
```

### 9.4 QOS 作成例

```bash
sacctmgr add qos entry_qos priority=100 \
  maxwalldurationperjob=12:00:00 maxtresperjob=cpu=64,mem=256G,node=2 \
  maxjobsperuser=10 maxsubmitjobsperuser=50 grptres=node=20 flags=DenyOnLimit

sacctmgr add qos personal_qos priority=200 \
  maxwalldurationperjob=48:00:00 maxtresperjob=cpu=128,mem=512G,node=4 \
  maxjobsperuser=20 maxsubmitjobsperuser=100 grptres=node=40 flags=DenyOnLimit

sacctmgr add qos group_qos priority=300 \
  maxwalldurationperjob=168:00:00 maxtresperjob=cpu=512,mem=2T,node=16 \
  grptres=node=200 flags=DenyOnLimit
```

## 10. 運用チェック

- `sshare` で account ごとの usage と fairshare を確認し、配分意図どおりか検証する。
- `sprio` で pending ジョブの優先度内訳（fairshare 寄与）を確認する。
- `squeue -o` で partition/account/QOS の組み合わせを監査し、コース外投入を検出する。
- 研究グループの契約変更時は、`Fairshare` と `GrpTRES` を同時更新して整合を保つ。
- 投入権限制御の監査運用は、章4.5の手順（日次/週次/定期レビュー）を基準に実施する。

## 11. sacct によるアカウンティングデータ分析

### 11.1 分析に使用する主要フィールド

`sacct` で取得できるフィールドのうち、投入制御・利用分析に関連するものを用途別に整理する。

#### 実行主体（誰が）

| フィールド | 内容 |
|---|---|
| `User` | ジョブを投入した OS ユーザ名 |
| `UID` | ユーザ数値 ID |
| `Group` | ユーザのプライマリグループ名 |
| `GID` | グループ数値 ID |

#### 実行権限（どの権限・コースで）

| フィールド | 内容 |
|---|---|
| `Account` | 投入時に使用した account 名（`-A` 指定または DefaultAccount）|
| `QOS` | 実際に適用された QOS 名（DefaultQOS が自動付与される）|
| `Partition` | 投入先パーティション名 |
| `AssocID` | Association の内部 ID（account + cluster + partition の組合せを一意に識別）|

> `Account` の値と本文書の account 階層の対応:
>
> | Account 値 | コース | 分析上の意味 |
> |---|---|---|
> | `svc_entry_all` | Entry | エントリコース利用 |
> | `svc_personal_*` | Personal | 個人コース利用（ユーザ専用 account）|
> | `svc_group_*` | Group | グループコース利用（研究室単位）|
> | `svc_project_*` | Project（shared）| 共有コース利用（課題単位）|
> | `svc_largejob_*` | Large-Job | 大規模申請枠利用（期間限定）|
>
> 3セグメント統一命名（`svc_<コース>_<ID>`）のため、`Account` 値の第2セグメントでコース種別が判断できる。

#### リソース量

| フィールド | 内容 |
|---|---|
| `AllocCPUS` | 割り当て済み CPU 数 |
| `NNodes` | 割り当てノード数 |
| `NodeList` | 割り当てノード名一覧 |
| `ReqTRES` | ユーザが要求した TRES（cpu=N,mem=NM,node=N,gres/gpu=N 等）|
| `AllocTRES` | 実際に割り当てられた TRES |
| `ReqMem` | 要求メモリ（`--mem` または `--mem-per-cpu` 由来）|
| `MaxRSS` | ジョブ全ステップ中の最大 RSS（最も重いノードのタスク）|

#### 実行時間・期間

| フィールド | 内容 |
|---|---|
| `Submit` | 投入日時 |
| `Start` | 実行開始日時 |
| `End` | 実行終了日時 |
| `Elapsed` | 経過時間（HH:MM:SS）|
| `ElapsedRaw` | 経過時間（秒数）|
| `Timelimit` | ジョブに設定された制限時間 |
| `CPUTime` | AllocCPUS × Elapsed の積（課金時間に相当）|
| `CPUTimeRAW` | CPUTime の秒数版 |

#### ジョブ状態・識別

| フィールド | 内容 |
|---|---|
| `JobID` | ジョブ ID（ステップ含む形式）|
| `JobIDRaw` | 親ジョブ ID のみ（集計時に使用）|
| `JobName` | ジョブ名（`--job-name` またはスクリプトファイル名）|
| `State` | 終了状態（COMPLETED / FAILED / TIMEOUT / CANCELLED 等）|
| `ExitCode` | 終了コード（signal 番号含む）|

---

### 11.2 分析パターン別クエリ例

以下では `-S`（開始日）`-E`（終了日）を省略しているが、実運用では期間を指定すること。

#### パターン A: コース別ジョブ件数と CPU 時間の集計

```bash
sacct -a --noheader -X \
  -o Account,Partition,User,JobID,Elapsed,CPUTimeRAW,AllocCPUS,NNodes,State \
  -S 2026-04-01 -E 2026-04-30 \
  | awk -F'|' '{count[$1"/"$2]++; cputime[$1"/"$2]+=$6} END {for(k in count) print k, count[k], cputime[k]}'
```

`Account` 値の第2セグメント（`svc_personal_*`, `svc_group_*`, `svc_project_*`, `svc_largejob_*`）でグループ化するとコース別集計になる。

#### パターン B: ユーザ別・account 別リソース利用確認

```bash
sacct -a --noheader -X \
  -o User,Account,QOS,Partition,AllocCPUS,NNodes,AllocTRES,Elapsed,State \
  -S 2026-04-01
```

#### パターン C: largejob account の期間外利用監査

```bash
sacct -a --noheader -X \
  -o User,Account,Partition,Submit,Start,End,Elapsed,AllocCPUS,State \
  --accounts=svc_largejob_lj30001 \
  -S 2026-07-01
# 申請期間を超えた Start のジョブが存在しないことを確認
```

#### パターン D: QOS TIMEOUT ジョブの検出（制限超過の傾向把握）

```bash
sacct -a --noheader -X \
  -o User,Account,QOS,Partition,Timelimit,Elapsed,AllocCPUS,State \
  --state=TIMEOUT \
  -S 2026-04-01
```

#### パターン E: group_shared における project account 別利用量

```bash
sacct -a --noheader -X \
  -o Account,User,AllocCPUS,NNodes,CPUTimeRAW,Elapsed,State \
  --partition=group_shared \
  -S 2026-04-01 \
  | sort -t'|' -k1,1
```

---

### 11.3 account ツリー分析との組み合わせ

sacct の `Account` は投入時の末端 account を示すため、
親 account（`svc_*`）を単位とした集計は sacctmgr の association ツリーを使って結合する。

```bash
# 親 account ごとに集計したい場合:
# 1. sacctmgr で account の parent を確認
sacctmgr show assoc format=Account,ParentName,User -P --noheader

# 2. sacct 出力と結合して集計（スクリプト等で処理）
```

`sshare` コマンドは account 階層に沿って fairshare / usage を表示するため、
コース単位での利用傾向把握には sacct より sshare の方が簡便な場合がある:

```bash
sshare -a -l -o Account,User,RawShares,NormShares,RawUsage,NormUsage,FairShare
```

---

### 11.4 分析設計上の注意

- `-X` フラグ: ジョブ全体（JobStep ではなく BatchStep 含む親ジョブ）のみを集計する。省略すると step ごとに行が展開され二重集計になる。
- `AllocTRES` vs `ReqTRES`: 要求値と実割当値が異なる場合（メモリのバックフィル等）は `AllocTRES` を正とする。
- `CPUTime` の解釈: AllocCPUS × Elapsed の積であり、実際の CPU 使用率は反映しない。実使用量は `TRESUsageInTot` の `cpu=` 値を参照する。
- GPU 利用量: `AllocTRES` の `gres/gpu=N` フィールドで確認する。`MaxRSS` は GPU メモリを含まない。
- 削除済みジョブ: デフォルトでは `sacctmgr` の expunge 設定に従い古いレコードが消える。長期分析には `slurmdbd` の `PurgeJobAfter` 設定を確認すること。
