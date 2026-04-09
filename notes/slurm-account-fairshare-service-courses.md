# Slurm サービスコース別 association(account) フェアシェア運用設計

## 1. 目的と前提

本書は、Slurm を用いたサービス提供において、次の 3 コースを `association(account)` と `QOS` で運用するための具体設計を示す。

- エントリコース
- パーソナルコース
- グループコース

前提:

- エントリコースとパーソナルコースは、コースごとに共通ジョブキューを持つ。
- エントリコースとパーソナルコースのシェア制御は個人単位で行う（個人を 1 グループとして扱ってよい）。
- エントリコースとパーソナルコースは、利用可能リソース量が固定であり、シェア配分も固定値とする。
- グループコースは、研究グループごとに専用ジョブキューを持つ。
- グループコースのシェア値は、申し込み済みの利用可能権利（主にノード数）に比例して増加する。

## 2. account 階層設計

account は次のように階層化する。

```text
root
├─ svc_entry
│  ├─ u_alice
│  ├─ u_bob
│  └─ ...
├─ svc_personal
│  ├─ u_carol
│  ├─ u_dave
│  └─ ...
└─ svc_group
   ├─ g_labA
   │  ├─ u_eve
   │  └─ u_frank
   ├─ g_labB
   │  ├─ u_grace
   │  └─ ...
   └─ ...
```

設計意図:

- `svc_entry`, `svc_personal`, `svc_group` はサービスコースの親 account。
- エントリ/パーソナルは、ユーザ単位 account（`u_*`）で fairshare を制御。
- グループは、研究グループ account（`g_*`）を fairshare 制御の主体にし、ユーザはその配下で運用。

## 3. ジョブキュー（partition）と account の対応

対応方針:

- `entry` partition: `svc_entry` 系 account を許可
- `personal` partition: 個人 account（`u_<username>` または `<username>`）を許可
- `group_<lab>` partition: 対応する研究グループ account（例: `g_<lab>`）を許可

実装上は、partition 設定と `job_submit` プラグインの両方で整合を担保する。

- partition 側: 許可 account 範囲を制限
- `job_submit` 側: `partition` と `--account` の組み合わせを検証し、不正投入を拒否（または規程に沿って補正）

### 3.1 job_submit による account 自動補完（`--account` 未指定時）

`--account` 未指定時は、次の優先順で account を補完する。

1. `group_*` partition:
   - 管理定義の明示マップ（`partition -> account`）を最優先
   - マップに無い場合のみ機械規則 `group_<lab> -> g_<lab>` を試行
2. `personal` partition:
   - `u_<username>` を試行
   - 見つからない場合は `<username>` を試行
3. `entry` partition:
   - 明示マップで定義した account（例: `svc_entry`）を使用

補完後は、許可 account と許可 QOS の整合性を検証する。

- 整合すれば投入継続
- 補完不能または整合性不一致なら投入拒否（`--account` 明示指定を要求）

補足:

- `--account` が明示されている場合は、その値を優先し、補完は行わない。
- `DefaultAccount` は管理補助として保持してよいが、本運用では上記補完規則を優先する。

## 4. フェアシェア配分ルール

### 4.1 エントリ/パーソナル（固定配分）

- コースごとの総リソース量は固定。
- 各ユーザ account の `Fairshare` を固定値で設定する。
- 配分の例:
  - エントリ: 全ユーザ `Fairshare=1`
  - パーソナル: 契約プランに応じて `Fairshare=1,2,4` などの固定段階値

### 4.2 グループ（権利比例配分）

- 研究グループのシェア値は、申込済み権利ノード数に比例させる。
- 例: `share_g = k * nodes_g`
  - `nodes_g`: 研究グループ g の利用可能権利ノード数
  - `k`: 全体スケーリング係数（例: 10）

例:

- `g_labA`: 4 ノード契約 -> `Fairshare=40`
- `g_labB`: 8 ノード契約 -> `Fairshare=80`

## 5. QOS 設計（具体例）

| QOS | 対象コース | 用途 | 例: 優先度/制限 |
|---|---|---|---|
| `entry_qos` | エントリ | 低コスト共用 | `Priority` 低め、`GrpTRES=node=固定枠` |
| `personal_qos` | パーソナル | 個人向け共用 | `Priority` 中、`GrpTRES=node=固定枠` |
| `group_qos` | グループ | 研究グループ専用 | `Priority` 中〜高、`GrpTRES=node=契約比例` |

補足:

- グループコースは、`association` 側の `Fairshare` と `QOS` 側の `GrpTRES` を併用して、権利比例を二重に担保できる。
- 厳格運用では `AccountingStorageEnforce=associations,qos` を設定し、許可外の account/QOS を拒否する。

## 6. sbatch 投入時の初期値設計（accounting と QOS）

### 6.1 初期 `accounting` 値

`sbatch` で `-A/--account` が未指定の場合に備え、ユーザに `DefaultAccount` を設定できる。ただし本運用では、`job_submit` による partition ベース補完を優先する。

- エントリ利用者: `DefaultAccount=u_<user>`
- パーソナル利用者: `DefaultAccount=u_<user>`
- グループ利用者: 主所属の `DefaultAccount=g_<lab>`（複数所属時は運用規程で主所属を定義）

### 6.2 初期 QOS 値

association または user に `DefaultQOS` を設定する。

- エントリ: `DefaultQOS=entry_qos`
- パーソナル: `DefaultQOS=personal_qos`
- グループ: `DefaultQOS=group_qos`

### 6.3 sbatch 実行例

明示指定:

```bash
sbatch -p entry -A u_alice --qos=entry_qos job.sh
sbatch -p personal -A u_carol --qos=personal_qos job.sh
sbatch -p group_labA -A g_labA --qos=group_qos job.sh
```

省略指定（既定値を利用）:

```bash
sbatch -p entry job.sh
sbatch -p group_labA job.sh
```

この場合、まず `job_submit` の補完規則が適用される。補完成功時は補完された `account` と `DefaultQOS`（または許可QOS）で処理される。補完失敗時は投入拒否となる。

## 7. sacctmgr 設定例

### 7.1 account 作成

```bash
sacctmgr add account svc_entry
sacctmgr add account svc_personal
sacctmgr add account svc_group

sacctmgr add account u_alice parent=svc_entry
sacctmgr add account u_carol parent=svc_personal

sacctmgr add account g_labA parent=svc_group
sacctmgr add account g_labB parent=svc_group
```

### 7.2 ユーザ追加と DefaultAccount

```bash
sacctmgr add user alice account=u_alice
sacctmgr modify user where name=alice set defaultaccount=u_alice

sacctmgr add user carol account=u_carol
sacctmgr modify user where name=carol set defaultaccount=u_carol

sacctmgr add user eve account=g_labA
sacctmgr modify user where name=eve set defaultaccount=g_labA
```

### 7.3 fairshare と DefaultQOS

```bash
# 固定配分の例
sacctmgr modify account where name=u_alice set fairshare=1
sacctmgr modify account where name=u_carol set fairshare=2

# 権利比例の例（k=10）
sacctmgr modify account where name=g_labA set fairshare=40
sacctmgr modify account where name=g_labB set fairshare=80

# DefaultQOS 設定
sacctmgr modify account where name=svc_entry set defaultqos=entry_qos
sacctmgr modify account where name=svc_personal set defaultqos=personal_qos
sacctmgr modify account where name=svc_group set defaultqos=group_qos
```

### 7.4 QOS 作成例

```bash
sacctmgr add qos entry_qos priority=100 grptres=node=20
sacctmgr add qos personal_qos priority=200 grptres=node=40
sacctmgr add qos group_qos priority=300 grptres=node=200
```

## 8. 運用チェック

- `sshare` で account ごとの usage と fairshare を確認し、配分意図どおりか検証する。
- `sprio` で pending ジョブの優先度内訳（fairshare 寄与）を確認する。
- `squeue -o` で partition/account/QOS の組み合わせを監査し、コース外投入を検出する。
- 研究グループの契約変更時は、`Fairshare` と `GrpTRES` を同時更新して整合を保つ。
