# rsc_verify

`notes/slurm-rsc-option-impl-v2.md` の v2.2 中核仕様に沿って、`--rsc` の挙動を実クラスタで繰り返し検証するための CLI ツール。

対象は以下。

- `SLURM_RSC_*` / `SLURM_RSC_C_EFF` / `OMP_*` の観測
- cli_filter による native resource shape 変換の観測
- `forbidden_with_rsc` による reject / warn の観測
- 実行結果と Codex 改善依頼メモの Markdown 生成

## 前提

- Python 3.6 以上
- `sbatch` / `srun` / `salloc` を実行できる環境
- 可能なら `scontrol` と `sacct` も利用可能であること

## 使い方

全ケース実行:

```bash
python3 tools/rsc_verify.py run
```

個別ケース実行:

```bash
python3 tools/rsc_verify.py run --case gpu-mode
```

glob 指定実行:

```bash
python3 tools/rsc_verify.py run --glob 'cases/rsc/*.json'
```

保存済み run の再表示:

```bash
python3 tools/rsc_verify.py report --run 20260409T010203Z-abcd1234
```

実行結果 Markdown の生成:

```bash
python3 tools/rsc_verify.py markdown-report --run 20260409T010203Z-abcd1234
```

Codex 改善依頼用 Markdown の生成:

```bash
python3 tools/rsc_verify.py markdown-issues --run 20260409T010203Z-abcd1234
```

ファイルへ保存:

```bash
python3 tools/rsc_verify.py markdown-issues --run 20260409T010203Z-abcd1234 --output notes/rsc-codex-handoff.md
```

## 結果保存

各 run は `artifacts/runs/<timestamp>-<run_id>/` に保存される。

- `summary.json`: run 全体の要約
- `<case_id>/case.json`: 実行したケース定義
- `<case_id>/submit.json`: submit コマンドの argv, exit code, stdout, stderr
- `<case_id>/job.json`: 収集したジョブ情報、環境変数、`scontrol`/`sacct` 結果
- `<case_id>/stdout.txt`, `stderr.txt`: ジョブ本体の出力
- `<case_id>/assertions.json`: 自動判定結果

## ケース定義

ケースは `cases/rsc/*.json` に 1 ファイル 1 ケースで置く。

最小構成:

```json
{
  "id": "example",
  "description": "Example case",
  "submit_mode": "sbatch",
  "submit_args": ["--rsc", "p=4:t=2:c=4:m=8192"],
  "script": "echo hello",
  "expect": {
    "submit_result": "accepted",
    "stdout_contains": ["hello"]
  }
}
```

## サポートしている期待値

- `submit_result`: `accepted` / `rejected`
- `submit_result_in`: 許容する submit 結果配列
- `submit_stdout_contains`: submit stdout に含まれるべき文字列配列
- `submit_stderr_contains`: submit stderr に含まれるべき文字列配列
- `submit_stdout_contains_any`: submit stdout にいずれか 1 つ含まれるべき文字列配列
- `submit_stderr_contains_any`: submit stderr にいずれか 1 つ含まれるべき文字列配列
- `stdout_contains`: ジョブ stdout に含まれるべき文字列配列
- `stderr_contains`: ジョブ stderr に含まれるべき文字列配列
- `env`: ジョブ内で観測した `SLURM_RSC_*` / `OMP_*` / 一部 Slurm env の期待値
- `env_absent`: ジョブ内で観測されてはいけない環境変数名配列
- `job`: `scontrol show job -o` と `sacct` の主要値に対する部分一致
- `job_normalized`: `ntasks` / `cpus_per_task` / `nodes` / `req_tres` などの正規化ビューに対する部分一致
- `job_state_in`: 許容する `JobState` 配列

## 補足

- `sbatch` ではツールが `--output` / `--error` を自動付与する。
- ジョブ内では `SLURM_RSC_*`、`OMP_*`、`SLURM_CPUS_PER_TASK`、`SLURM_NTASKS` などを自動採取する。
- GPU モードでは `SLURM_RSC_SPEC` / `SLURM_RSC_G` を主に確認し、CPU 系 `SLURM_RSC_*` と `OMP_*` は `env_absent` で未存在を検証できる。
- `SLURM_RSC_C_EFF` は user_init と実行経路に依存するため、必要なケースだけで期待値に入れる。
- `markdown-report` は run 全体を人間向けに整形する。
- `markdown-issues` は失敗ケースだけを抜き出し、Codex へ再現・修正依頼を渡すための Markdown を生成する。
