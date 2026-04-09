# specs/slurm-sched-spec.new.md 5章（ジョブスケジューリング）に対する Slurm 実現性分析

対象: `specs/slurm-sched-spec.new.md` の `5-1`〜`5-5`  
前提: Slurm 25.11.4（標準機能 + 運用設定 + 必要時job_submit）

## 1. 要約（結論）

- `5-1`（`w_t`, `w_f`）: **実現可**。`priority/multifactor` の重み設定で対応可能。
- `5-2`（FCFS / 追い越し許容 / バックフィル）: **実現可**。`SchedulerType` の選択で対応可能。
- `5-3`（優先度式）: **近似可**。multifactorでAge/Fairshareを使えるが、式の完全一致は難しい。加えて、ポリシーの適用対象範囲を運用で明確化する必要がある。
- `5-4`（キュー間優先制御）: **可（運用設計依存）**。研究グループを association（account）として管理し、Fairshareでグループ間優先を制御しつつ、QOS Priority / Partition PriorityTierで基準優先度を補正できる。
- `5-5`（最優先実行）: **可**。優先度制御（`scontrol top`、nice/QOS調整）を中心に実現できる。

## 2. 要件別の実現方法と難易度

## 2.1 5-1: キューごとの重み `w_t(q)`, `w_f(q)`

要件意図:
- 各サブシステムで「待ち時間寄与」と「フェアシェア寄与」の重みを定義する。

Slurmでの対応:
- `PriorityType=priority/multifactor`
- `PriorityWeightAge`, `PriorityWeightFairshare` で重み設定

難易度:
- **低〜中**

ギャップ:
- なし（各サブシステム単位で充足可能）。

実装方針:
- `PriorityType=priority/multifactor` と `PriorityWeightAge` / `PriorityWeightFairshare` を調整する。
- 必要に応じて `PriorityWeightQOS` / `PriorityWeightPartition` を併用して運用最適化する。

## 2.2 5-2: FCFS / 追い越し許容 / バックフィルの選択

要件意図:
- 各サブシステムで3ポリシー（FCFS / 追い越し許容 / バックフィル）のいずれかを選択可能。

Slurmでの対応:
- `SchedulerType=sched/builtin`（FIFO寄り）
- `SchedulerType=sched/backfill`（追い越し・バックフィル）

難易度:
- **低〜中**

ギャップ:
- なし（各サブシステム単位で充足可能）。

実装方針:
- `SchedulerType=sched/builtin`（FCFS寄り）または `sched/backfill`（追い越し許容/バックフィル）を選択する。
- 要件運用上は `sched/backfill` を採用し、資源利用効率を確保する構成が現実的。

## 2.3 5-3: 優先度式 `P(j)=W(j)*w_t + F(j)*w_f`

要件意図:
- 待ち時間とフェアシェアを掛け合わせ重み付き合成して優先度化。
- 加えて、FCFS/追い越し許容/バックフィルの判定を適用するジョブ集合（適用対象範囲）を前提に実行可否を決める。

Slurmでの対応:
- multifactor優先度で Age/Fairshare を寄与要素にできる。
- `sprio` で内訳（AGE, FAIRSHARE, PARTITION, QOS など）可視化可能。

難易度:
- **中**

ギャップ:
- Slurm内部式は要件式と同一ではない（係数・正規化・他因子が入る）。
- 「要件式そのまま」の完全一致は標準では困難。
- 適用対象範囲（単一ジョブキュー内 / 複数ジョブキュー横断）を、運用規程として明示しないと解釈差が残る。

現実的代替:
- 要件式を「概念上の設計式」として扱い、Slurmでは multifactor パラメータ同定で近似。
- 適用対象範囲は、ポリシー設定と運用手順（対象キュー群・対象条件）で固定し、監視時に同範囲内での順序妥当性を確認する。

## 2.4 5-4: キュー間の実行優先

要件意図:
- ジョブキューを研究グループに割り当て、研究グループ間で過去利用量に基づく公平制御を行う。
- その上で、キュー標準資源量と過去利用積分値に基づき、実行集合を選ぶ。

Slurmでの対応:
- 研究グループを account（association）として定義し、Fairshareでグループ間の優先度を制御する。
- QOS Priority（`sacctmgr`）または Partition PriorityTier でキューごとの基準優先度を付与する。
- `job_submit` プラグインで、`--account` 未指定時に partition から account を自動補完する。
  - `group_*`: 明示マップ優先、未定義時のみ `group_<lab> -> g_<lab>` 規則を適用
  - `personal`: `u_<username>` を優先し、なければ `<username>` を試行
  - 補完後に partition/account/QOS の整合を検証し、不一致は reject

難易度:
- **中**

ギャップ:
- `U(q)` をキュー単位の積分値として厳密に扱う場合、Slurm標準の利用量管理（association単位）と一致しない場合がある。
- グループコースで非標準 account 名を扱う場合、`partition -> account` 明示マップの保守が必要。
- パーソナルコースで username 由来補完を使う場合、account 命名規則（`u_<username>` / `<username>`）の一意性維持が必要。

実現方法:
- 研究グループごとに account を作成し、キューの利用対象と対応付ける。
- Fairshareを有効化し、研究グループ間で利用実績に応じた優先度調整を行う。
- QOS Priority / Partition PriorityTier で基準優先度（`R_std` 相当）を補正する。
- `job_submit` に補完ルール（group: 明示マップ優先、personal: username フォールバック）を実装し、`--account` 未指定投入を簡素化する。
- `sprio` / `sshare` で研究グループ間の優先度内訳と利用量偏りを監視する。

## 2.5 5-5: 最優先スケジューリング（障害影響ジョブ・管理者任意ジョブ）

要件意図:
- 項目5-3/5-4より優先して、対象ジョブを最優先で実行する。
- 必要資源が利用可能であれば直ちに実行し、利用不能時は利用可能になり次第直ちに実行できるよう優先度を制御する。

Slurmでの対応:
- `scontrol top` による優先順位調整（同一ユーザ/partition/account/QOS条件）
- `scontrol update ... Nice=` や QOS変更による優先度昇格
- 必要時のみ preempt（`PreemptType=preempt/qos`, `PreemptMode`）を併用
- 運用管理者による対象ジョブ指定（障害影響ジョブ／任意ジョブ）

難易度:
- **中**

注意:
- preemptionは必須ではなく、優先度制御が主手段。
- preemptionを併用する場合は方式（SUSPEND/REQUEUE/CANCEL）と影響を運用規程として固定する必要がある。

## 3. 実装構成案（現実的）

## 3.1 基本設定（例）

```ini
# slurm.conf
PriorityType=priority/multifactor
PriorityWeightAge=1000
PriorityWeightFairshare=1000
PriorityWeightQOS=500
PriorityWeightPartition=200

SchedulerType=sched/backfill

PreemptType=preempt/qos
PreemptMode=REQUEUE
```

## 3.2 QOS設計（例）

- `normal`: 通常キュー
- `urgent`: 高優先 + preempt許可
- `recovery`: 障害復旧専用（最優先）

`sacctmgr` 例:
- `QOS Priority` を段階付け
- `Preempt` 関係を明示
- 必要に応じ `UsageThreshold`, `GrpTRESMins` を併用

## 3.3 運用補助

- `scontrol top` は管理者運用手順に限定して明文化。
- `sprio` / `squeue` を使い、優先度内訳・待ち理由を日常監視。
- `sshare` を併用し、研究グループ（account）間の利用量とフェアシェア配分を継続監視する。
- 要件ギャップは主に `5-3` の式一致性・適用対象範囲の明文化と、`5-4` の `U(q)` 定義整合に限定される。`5-1`, `5-2` は各サブシステム単位で標準設定により充足可能。

## 4. ギャップ一覧（要件 vs Slurm標準）

| 要件 | Slurm標準の到達 | ギャップ | 対応方針 |
|---|---|---|---|
| 5-1 `w_t`,`w_f` | 対応可能 | なし | multifactor重み設定で実装 |
| 5-2 ポリシー選択 | 対応可能 | なし | SchedulerType設定で実装 |
| 5-3 数式一致 | 近似対応 | 内部式は非同一、適用対象範囲の明文化が必要 | multifactor同定で近似し、対象範囲を運用規程で固定 |
| 5-4 キュー間優先（研究グループ間公平） | 対応可能 | `U(q)`厳密一致、group明示マップ保守、personal命名規則維持に差分 | association Fairshare + QOS/Partition補正 + `job_submit` 補完/検証で実現 |
| 5-5 最優先実行 | 対応可能 | 優先度昇格手順の運用整備が必要 | `scontrol top`/nice/QOS制御を標準化（必要時のみpreempt併用） |

## 5. 総合難易度

- **短期（設定中心）**: 低〜中
- **中期（運用安定化）**: 中
- **要件完全一致（追加開発）**: 高

推奨:
- まずは Slurm 標準設定（multifactor + SchedulerType）で `5-1`, `5-2` を実装。
- `5-3` は式一致性だけでなく適用対象範囲の定義を先に固定する。
- `5-4` はキュー-研究グループ対応ルールと `U(q)` の運用上の定義を先に固定し、Fairshare/QOS/Partition/`job_submit` の役割分担を明文化する。
- `job_submit` の補完ルールは、group は明示マップ優先、personal は `u_<username> -> <username>` フォールバックを標準として固定する。
- その上で残存ギャップが顕在化した項目のみ追加開発を検討する。

## 6. 参照（Slurm 25.11.4 同梱 man）

- `doc/man/man5/slurm.conf.5`
  - `SchedulerType`（`sched/backfill`, `sched/builtin`）
  - `PriorityType=priority/multifactor`
  - `PriorityWeightAge`, `PriorityWeightFairshare`, `PriorityWeightQOS`, `PriorityWeightPartition`
  - `SchedulerParameters=enable_user_top`
- `doc/man/man1/scontrol.1`
  - `scontrol top`（nice調整による順序変更）
- `doc/man/man1/sprio.1`
  - 優先度内訳（AGE, FAIRSHARE, QOS, PARTITION 等）
- `doc/man/man1/sacctmgr.1`
  - QOS `Priority`, `Preempt`, `PreemptMode`、Usage関連設定
