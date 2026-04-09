# slurm-custom-jobsubmit-rscopt-test

Slurm に対する独自 `--rsc` オプション拡張の設計検討と、実クラスタ上での動作検証を行うためのリポジトリ。

`--rsc` は Slurm 標準機能ではないため、このリポジトリでは主に以下を扱う。

- SPANK プラグインと `job_submit` プラグインによる実装方針の整理
- 仕様メモと実装ノートの蓄積
- 実際にジョブを投入して同じ検証を何度も回すための検証ツール

## 構成

- [notes/slurm-rsc-option-impl.md](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/notes/slurm-rsc-option-impl.md)
  `--rsc` 実装仕様と挙動の主要ノート。最初に読む前提資料。
- [tools/rsc_verify.py](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/tools/rsc_verify.py)
  実クラスタに対して反復実行できる検証 CLI。
- [tools/README.md](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/tools/README.md)
  検証ツールの詳細な使い方。
- [cases/rsc](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/cases/rsc)
  `rsc_verify` で実行するケース定義。
- [tests/test_rsc_verify.py](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/tests/test_rsc_verify.py)
  ツールのローカル単体テスト。

## 使い方の概要

前提:

- Python 3.6 以上
- `sbatch` / `srun` / `salloc` が使える Slurm 環境
- 可能なら `scontrol` と `sacct` も利用可能

全ケースを実行:

```bash
python3 tools/rsc_verify.py run
```

個別ケースを実行:

```bash
python3 tools/rsc_verify.py run --case gpu-mode
```

過去の実行結果を再表示:

```bash
python3 tools/rsc_verify.py report --run <run_id>
```

実行結果は `artifacts/runs/<timestamp>-<run_id>/` に保存される。  
各ケースごとに submit 結果、ジョブ出力、環境変数、`scontrol` / `sacct` の取得結果、期待値判定結果が残る。

## このリポジトリの進め方

- 机上の推測だけで結論を出さず、可能な限り実際にジョブを投入して確認する。
- 新しい知見や実環境依存の挙動は `notes/` に追記する。
- `--rsc` の解釈、SPANK / `job_submit` の責務分担、競合オプション方針は [notes/slurm-rsc-option-impl.md](/local/w550380/repos/slurm-custom-jobsubmit-rscopt-test/notes/slurm-rsc-option-impl.md) を基準に扱う。
