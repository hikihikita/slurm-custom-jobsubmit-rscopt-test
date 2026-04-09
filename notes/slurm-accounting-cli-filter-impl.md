# Slurm accounting CLI filter 実装仕様（impl）

## 1. 目的とスコープ

本書は、`notes/slurm-service-course-accounting-policy.md` に定義した `cli_filter` 方針を、実装可能な仕様に落とし込むための実装設計書である。

対象:

- `sbatch` / `salloc` 実行時の `cli_filter` 実装
- `/etc/slurm/accounting_policy_data.lua` の生成ロジック
- `scripts/generate-cli-filter-config.sh` の入出力契約

非対象:

- `job_submit` プラグイン実装本体
- `slurm.conf` / `slurmdbd` のクラスタ全体設計

前提:

- `account` と `QOS` は 1 対 1 運用
- `DefaultQOS` を唯一の正とする
- `--qos` 手動指定は禁止
- `group_shared` では `-A p_*` を必須とする

## 2. 全体アーキテクチャ

```text
User (sbatch/salloc を実行)
  -> [sbatch/salloc 内] slurm_cli_pre_submit() フック
     └─ cli_filter_accounting.lua: 入力補助 + 事前検証
        （エラー時は slurm.user_msg() でメッセージ表示後、終了コード 1 で終了）
  -> オプション補完後、slurmctld へ submit
     -> slurmctld/job_submit (最終検証・拒否判定)
```

### ファイル構成

| ファイル | 種別 | 管理方法 | 内容 |
|---|---|---|---|
| `/etc/slurm/cli_filter_accounting.lua` | プラグインコード | 手動（バージョン管理） | 判定ロジック・`loadfile()` 呼び出し |
| `/etc/slurm/accounting_policy_data.lua` | 設定データ | 自動生成（`generate-cli-filter-config.sh`） | account/partition マッピングテーブル |

`generate-cli-filter-config.sh` が書き換えるのは `accounting_policy_data.lua` のみ。  
`cli_filter_accounting.lua` は generate スクリプトが触れないコードファイルであり、  
`slurm.conf` の `CliFilterParameters=cli_filter_lua_path=/etc/slurm/cli_filter_accounting.lua` で登録する。

### 責務分担

- `cli_filter_accounting.lua`: ユーザ入力の簡素化、曖昧入力の早期検出
- `job_submit`: 最終的な許可/拒否を担保（fail-close）

## 3. cli_filter 入出力仕様

### 3.1 入力

- 実行コマンド: `sbatch` または `salloc`
- ユーザ入力引数: `-p/--partition`, `-A/--account`, `--qos` など
- 実行ユーザ名: `$USER`
- 設定ファイル: `/etc/slurm/accounting_policy_data.lua`

### 3.2 出力

- 実行継続時:
  - 補完済み引数で Slurm コマンドを実行
  - 終了コード `0`
- 停止時:
  - エラーメッセージ（原因 + 再実行例）を `slurm.user_msg()` で出力
  - 終了コード `1`（sbatch/salloc の `error_exit` にハードコード）

### 3.3 終了コード契約

- `0`: 有効入力（補完あり/なし）
- `1`: エラー（入力不足・ポリシー違反）— エラー種別は `slurm.user_msg()` のメッセージで確認

> **注**: cli_filter Lua プラグインは `slurm.ERROR` を返した場合、sbatch/salloc の終了コードは
> 常に `1` になる（`src/sbatch/opt.c` の `error_exit` にハードコード）。
> 入力不足とポリシー違反の区別はメッセージ内容で行う。

## 4. 設定ファイル仕様

パス:

- `/etc/slurm/accounting_policy_data.lua`

`generate-cli-filter-config.sh` が自動生成する Lua テーブル形式。
cli_filter_accounting.lua 内で `loadfile()` により読み込む。

必須キー:

- `policy`
- `partition_rules`
- `user_allowed_accounts`

スキーマ:

```lua
-- /etc/slurm/accounting_policy_data.lua
-- 自動生成: generate-cli-filter-config.sh
-- generated_at: 2026-01-01T00:00:00Z

return {
  version = 1,
  policy = {
    qos_source        = "default_qos",
    reject_manual_qos = true,
  },
  partition_rules = {
    entry = { mode = "fixed", account = "svc_entry_all" },
    personal = { mode = "template", account_template = "svc_personal_${USER}" },
    group_pattern = {
      mode              = "template_from_partition_suffix",
      partition_regex   = "^group_(.+)$",
      account_template  = "svc_group_${MATCH_1}",
    },
    group_shared = { mode = "require_explicit_account" },
    largejob     = { mode = "require_explicit_account" },
  },
  known_largejob_accounts = { "svc_largejob_lj30001" },
  user_allowed_accounts = {
    u1001 = { "svc_project_pr20001", "svc_project_pr20002" },
    u1002 = { "svc_project_pr20001" },
  },
}
```

cli_filter_accounting.lua 内での読み込み方法:

```lua
local cfg = loadfile("/etc/slurm/accounting_policy_data.lua")()
```

バリデーション規則:

- `policy.qos_source` は `default_qos` 固定
- `policy.reject_manual_qos` は `true` 固定
- `partition_rules.entry.account` は空不可
- `group_shared` 利用者は `user_allowed_accounts` に候補が存在すること
- `known_largejob_accounts` は重複不可

## 5. 判定アルゴリズム（疑似コード）

```text
input: argv, user, config

if cmd not in {sbatch, salloc}:
  passthrough

parse partition/account/qos from argv
if partition missing:
  slurm.user_msg("ERROR: -p を指定してください\nHINT: ...")
  return slurm.ERROR  -- sbatch/salloc は終了コード 1 で終了

if qos is specified and policy.reject_manual_qos:
  slurm.user_msg("ERROR: --qos の手動指定は禁止です\nHINT: --qos を削除して再実行")
  return slurm.ERROR

if account missing:
  switch partition rule:
    - fixed: set account to fixed value
    - template: substitute ${USER}
    - template_from_partition_suffix: extract suffix and build account
    - require_explicit_account:
        if partition == group_shared:
          slurm.user_msg("ERROR: group_shared では -A svc_project_pr* が必須です\nCANDIDATES: " .. ...)
        else
          slurm.user_msg("ERROR: -A を明示してください\nHINT: ...")
        return slurm.ERROR

if account exists but violates partition policy:
  slurm.user_msg("ERROR: 許可外の account です\nHINT: 許可 account を指定")
  return slurm.ERROR

exec original command with normalized args
```

## 6. エラーメッセージ仕様

形式:

- 1行目: `ERROR: <原因>`
- 2行目: `HINT: <再実行例>`
- 3行目（必要時）: `CANDIDATES: <a,b,c>`

例1（`group_shared` で `-A` 未指定）:

```text
ERROR: group_shared では -A svc_project_pr* が必須です
HINT: sbatch -p group_shared -A svc_project_pr20001 job.sh
CANDIDATES: svc_project_pr20001,svc_project_pr20002
```

例2（`--qos` 指定）:

```text
ERROR: --qos の手動指定は禁止です（DefaultQOS を使用）
HINT: sbatch -p entry -A svc_entry_all job.sh
```

## 7. generate-cli-filter-config.sh 仕様

### 7.1 目的

- `sacctmgr` / `scontrol` から `accounting_policy_data.lua` を自動生成する。
- `cli_filter_accounting.lua`（プラグインコード本体）は生成対象外であり、このスクリプトでは変更しない。

### 7.2 インターフェース

- `--out <path>`: 出力先（省略時 stdout）
- `--assoc-file <path>`: assoc 入力の差し替え
- `--partitions-file <path>`: partition 入力の差し替え

期待入力（assoc）:

- `User|Account|Partition|DefaultQOS`（parsable2）

### 7.3 生成規則

- `user_allowed_accounts`:
  - `Account` が `svc_project_*` の行を `User` ごとに集約
- `known_largejob_accounts`:
  - `Account` が `svc_largejob_*` の行を重複排除して列挙
- `partition_rules`:
  - `entry`, `personal`, `group_pattern`, `group_shared`, `largejob` を固定テンプレートとして出力
- `policy`:
  - `qos_source: default_qos`
  - `reject_manual_qos: true`

### 7.4 失敗時挙動

- 入力ファイル不正・必須列不足: 終了コード `2`
- 実行環境不足（`sacctmgr` 未実行可など）: 終了コード `3`
- エラー時は出力ファイルを更新しない

## 8. 実装ステップ

1. `cli_filter` 引数パーサ実装（`sbatch` / `salloc` 限定）
2. 設定ファイルロードとスキーマ検証実装
3. 判定アルゴリズム実装（`-A` 補完、`--qos` 禁止、`group_shared` 必須）
4. エラーメッセージ/終了コード実装
5. `generate-cli-filter-config.sh` の出力契約に合わせて統合
6. 受け入れテスト追加

## 9. テスト計画

### 9.1 正常系

- `sbatch -p entry job.sh` -> `-A svc_entry_all` 補完で実行
- `salloc -p group_gr10001` -> `-A svc_group_gr10001` 補完で実行
- `sbatch -p group_shared -A svc_project_pr20001 job.sh` -> 実行

### 9.2 異常系

- `sbatch -p group_shared job.sh` -> 終了コード `1`、ERROR + CANDIDATES（`svc_project_pr*`）メッセージ
- `sbatch -p entry --qos=entry_qos job.sh` -> 終了コード `1`、`--qos` 禁止メッセージ
- 許可外 `-A` -> 終了コード `1`、許可外 account メッセージ

### 9.3 生成系

- オンライン生成（`sacctmgr/scontrol`）が成功
- オフライン生成（`--assoc-file` / `--partitions-file`）が成功
- `svc_project_*`/`svc_largejob_*` の重複が除去される

## 10. 導入・運用手順

1. 生成:

```bash
scripts/generate-cli-filter-config.sh --out /etc/slurm/accounting_policy_data.lua.new
```

2. 検証（Lua 構文・必須キー）:

```bash
lua -e "dofile('/etc/slurm/accounting_policy_data.lua.new')" && echo OK
```

3. 反映:

```bash
mv /etc/slurm/accounting_policy_data.lua.new /etc/slurm/accounting_policy_data.lua
```

4. 監査:

- `cli_filter` 終了コード `1` の件数（slurmctld ログの cli_filter エラー行）
- `group_shared` の候補定義漏れ
- `DefaultQOS` 未設定 association の有無

## 11. 既知の制約

- `cli_filter` は補助層であり、最終的な安全性は `job_submit` 側の reject に依存する。
- `group_shared` の自動選択は行わない（誤投入防止のため）。
- `--qos` を使った例外運用は本仕様では扱わない。
