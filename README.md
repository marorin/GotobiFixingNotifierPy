# Python版 ゴトー日（五十日仲値）事前通知ツール

## これは何？
五十日（5/10/15/20/25/30日、条件により31日や2月末）について、土日・祝日・年末年始(12/31-1/3)を「非営業日」として前営業日に前倒しした **“実質ゴトー日(F)”** を判定し、通知します。

このツールは **1回起動して判定→必要なら通知→終了** します。定期実行（CRON/タスクスケジューラ等）は利用者側で設定してください。

## できること（要点）
- **祝日CSV（日本/米国）をローカルファイルから読み込み**（UTF-8想定）
- **今日 or 明日** が実質ゴトー日(F)なら通知対象
- 通知ウィンドウ（既定）: **前日10:00(JST) ～ 当日9:55(JST)** の間のみ通知
- 通知方式:
  - 既定: **ntfy へ送信**（`--notify ntfy`）
  - オプション: **端末ローカル通知**（`--notify local`）
- `gotobi-fixing.state.json` に最終通知した F(YYYYMMDD) を保存し、同一Fの重複通知を抑止

---

## ファイル構成（このフォルダに置く）
- `gotobi_notifier.py` : 本体
- `jp_holidays.csv` : 日本祝日CSV
- `fed_bank_holidays.csv` : 米国祝日CSV
- `gotobi-fixing.state.json` : 実行後に自動生成（重複通知抑止用）

---

## 動作環境
- **Python 3.10+ 推奨**（型注釈 `dt.date | None` 等を使用）
- 追加ライブラリ不要（標準ライブラリのみ）

---

## まず動かす（最短）

### ntfy送信（既定）

```bash
python gotobi_notifier.py
```

> 注意: `--notify` を付けない場合は **自動で `ntfy`** です。  
> ただし環境変数 `GOTOBI_NOTIFY_MODE` を設定している場合は、その値が既定になります。

### 端末ローカル通知（サーバー/ntfy不要の“端末内完結”）

```bash
python gotobi_notifier.py --notify local
```

ローカル通知は環境により対応が異なります（見つからない場合は標準出力のみになります）。
- Android (Termux): `termux-notification`
- Linux: `notify-send`
- macOS: `osascript`
- Windows: PowerShell による簡易バルーン通知（環境により出ない場合があります）

---

## おすすめ運用

### 推奨: 外部サーバー + cron + ntfy（安定）
「毎日ほぼ確実に動かしたい」場合、以下を推奨します。

- **常時稼働の外部環境**（VPS / NAS / Raspberry Pi / 常時ON PC など）で cron 実行
- 本スクリプトは **`--notify ntfy`（既定）**で送信
- スマホ側は **ntfyアプリで受信**（端末側の省電力の影響を受けにくい）

### 代替: local運用（端末内完結。ただし不安定になり得る）
`--notify local` を使うと、「サーバー無しで端末内完結」できますが、特にAndroid（Termux活用）では以下の理由で **遅延/未実行**が起こり得ます。

- 省電力（Doze、電池最適化、メーカー独自のバックグラウンド制限）でプロセスが止まる
- 端末再起動/アプリ終了でスケジューラ（cron等）が止まる

運用する場合は、Termux/自動化アプリを **電池最適化の対象外**にする等の調整が必要です。

---

## 祝日CSVフォーマット
UTF-8。行頭 `#` / `//` はコメント扱い。
区切りは `,` / タブ / `;` を許容します。

例（タブ区切り）:

```
#date	name
2020/1/1	元日
2020/1/2	銀行休業日
```

日付は以下の形式を許容します:
- `YYYY/MM/DD`
- `YYYY-MM-DD`
- `YYYY.MM.DD`

---

## オプション一覧（よく使う順）

### 通知方式
- `--notify ntfy|local`
  - 既定: `ntfy`
  - 環境変数でも指定可: `GOTOBI_NOTIFY_MODE=ntfy|local`

### （参考）`Config` の bool 設定値（コード内設定）
`gotobi_notifier.py` 冒頭の `Config` には、判定ルール等を変えるための bool 設定があります。
これらは **現状CLIオプションでは変更できないものもある**ため、使いたい場合は `gotobi_notifier.py` の該当行を編集してください。

- **`include_day31`（既定: True）**: 31日を「ベース日付」の候補に含めるか
  - ただし **年末年始除外ON(`exclude_yearend_bank_closure=True`) かつ 12月** の場合は、互換動作として31日を候補から外します
- **`include_feb_last_day`（既定: True）**: 2月の最終日（28/29日）を候補に含めるか
- **`exclude_yearend_bank_closure`（既定: True）**: 12/31〜1/3 を非営業日扱いにして前倒しするか
- **`enable_holiday_jp`（既定: True）**: 日本祝日CSVを読み込み、祝日として前倒しに使うか
- **`enable_holiday_us`（既定: True）**: 米国祝日CSVを読み込み、祝日として前倒しに使うか
- **`enforce_window`（既定: True）**: 通知ウィンドウ判定を有効にするか
  - CLIでは `--no-window` で無効化できます
- **`enable_ntfy`（既定: True）**: `--notify ntfy` のとき、実際にntfy送信するか
  - CLIでは `--no-ntfy` で無効化できます
- **`enable_state_update`（既定: True）**: stateファイル更新を行うか（重複通知抑止のため）
  - CLIでは `--no-state` で無効化できます

### テスト用：現在時刻を仮指定（判定を再現）
- `--now "<時刻文字列>"`
  - 例: `--now "2026-01-02 12:34"`（TZ無し → JST扱い）
  - 例: `--now "2026-01-02T03:34:56Z"`（UTC）
  - 例: `--now "2026-01-02T12:34:56+09:00"`（UTC+9）
  - 例: `--now "20260109 4:30"`（`YYYYMMDD H:MM` も可）

### 通知ウィンドウ判定を無効化
- `--no-window`

### ntfy送信だけ止める（テスト用）
- `--no-ntfy`

### state更新だけ止める（テスト用）
- `--no-state`
  - 互換: `--no-state-update` も受け付けます（ヘルプには出ません）

### 互換オプション（まとめて無効化）
- `--dry-run`
  - `(互換) --no-ntfy と --no-state を同時に指定` と同じ扱い

### 入力ファイル/通知設定
- `--jp <path>` 日本祝日CSV（既定: `jp_holidays.csv`）
- `--us <path>` 米国祝日CSV（既定: `fed_bank_holidays.csv`）
- `--state <path>` state json（既定: `gotobi-fixing.state.json`）
- `--ntfy-server <url>`（既定: `https://ntfy.sh`）
- `--ntfy-topic <topic>`（既定: `your_topic_here`）
- `--ntfy-title <title>`（既定: `gotobi-fixing`）
- `--ntfy-priority <priority>`（既定: `default`）

---

## 環境変数で設定したい場合（ntfy向け）
環境変数で設定したい場合に使えます。これを設定した場合、Class Config内で設定されているデフォルト値より優先されます。コマンドライン引数（CLI）を指定した場合、CLIの方が優先されます。

- `NTFY_SERVER`
- `NTFY_TOPIC`
- `NTFY_TITLE`
- `NTFY_PRIORITY`
- `GOTOBI_HOLIDAY_JP`
- `GOTOBI_HOLIDAY_US`
- `GOTOBI_STATE`
- `GOTOBI_NOTIFY_MODE`（`ntfy` / `local`）

### Windows（PowerShell）例

```powershell
$env:NTFY_TOPIC="your_topic_here"
python .\gotobi_notifier.py
```

### Linux/macOS（bash）例

```bash
export NTFY_TOPIC="your_topic_here"
python gotobi_notifier.py
```

---

## 重要（ntfy topic の扱い）
`ntfy` の topic は **実質「通知先の鍵」** です。公開リポジトリやブログ等にそのまま貼ると、第三者が勝手に投稿できる可能性があります。topicはいわば共有で、他人と被ると同じtopic設定した人にも通知が届きます。

- **topicは公開しない**: GitHubの公開リポジトリ、スクショ、ブログ、動画等に topic を載せないでください（第三者投稿＝スパム/なりすましの原因になります）
- **各ユーザーで別topicにする**: 1つのtopicを複数人で共有すると、通知が混ざったり、誰かに漏れた時点で全員が影響を受けます
- **推測されにくい文字列にする**: `topic_name_abcdef0ghijk987opqr654stu321vwxyz` のように **複雑でランダムなサフィックス**を付けることを推奨します（他人と被らない/推測されにくい）
- **安全な文字だけ使う**: topic名は **英数/ハイフン/アンダースコア**推奨（URLに載るため、記号や日本語は避けるのが安全）
- **設定は環境変数/引数で渡す**:
  - 環境変数: `NTFY_TOPIC`
  - 引数: `--ntfy-topic <your_topic>`

補足: **topicが漏れた/被って通知が荒れた場合**は、topicを新しいものに変更して運用を継続できます。
- 新topicを作成（推測されにくい文字列推奨）
- 本スクリプトの設定を新topicに変更（`NTFY_TOPIC` または `--ntfy-topic`）
- スマホ側は旧topicの購読を解除（旧topicは使わない）


---

## 定期実行の例

### Linux（cron）例
例: 7:00/JST に実行（CRON環境がJSTの場合）
```cron
0 7 * * * /usr/bin/python3 /path/to/gotobi_notifier.py >> /path/to/gotobi.log 2>&1
```
例: 7:00/JST に実行（CRON環境がUTCの場合）
※(UTC+0)22:00 = (JST)7:00
```cron
0 22 * * * /usr/bin/python3 /path/to/gotobi_notifier.py >> /path/to/gotobi.log 2>&1
```

### Windows（タスクスケジューラ）方針
- 「**ログオンしているかどうかにかかわらず実行**」を使うと、起動中ならログオン無しでも動きます
- 「**タスクを実行するためにコンピューターをスリープ解除する**」をONにできる場合があります
- 「**開始予定時刻にタスクを開始できなかった場合、できるだけ早く実行する**」をON推奨

### Android（Tasker/CRON + Termux）方針（localモード）
端末内完結を狙うなら `--notify local` を使います。
- TaskerやCRONで「指定時刻 → Termuxでコマンド実行」
- 省電力（Doze/電池最適化）で遅延/未実行が起こり得るため、Tasker/Termuxの電池最適化除外など調整が必要です

---

## よくあるトラブル

### Windowsで日本語が文字化けする
コンソールのコードページ/フォントにより、ヘルプやログが文字化けすることがあります。
対策例:
- PowerShellで `chcp 65001`（UTF-8）に切り替える
- Windows TerminalのUTF-8設定を使う

### `--now` の形式エラーになる
おすすめは以下です:
- `2026-01-02 12:34`（TZ無し → JST）
- `2026-01-02T12:34:56+09:00`
- `2026-01-02T03:34:56Z`

### 時刻が意図とズレる（タイムゾーンの扱いが不安）
このツールの判定は **常にJST基準**です。

- `--now` を **指定しない**場合: スクリプト内部で `datetime.now(tz=JST)` を使うため、**実行環境（サーバー/PC）のローカルTZがJST以外でも、常にJSTの現在時刻で判定**します。ただし本スクリプトをCRON等で動かす場合、そのCRON側の設定自体はサーバー／ローカル側のタイムゾーンに依存しますのでご注意下さい。

- `--now` に **タイムゾーン付き**（例: `...Z` や `...+HH:MM`）を渡した場合: そのTZとして解釈して **JSTへ変換してから**判定します
  - 例: `2026-01-02T03:34:56Z`（UTC）→ JSTに変換して判定
- `--now` に **タイムゾーン無し**を渡した場合: **JSTとして扱います**（ローカルPCのTZは使いません）
  - 例: `2026-01-02 12:34` は JST 12:34 として判定

---

## ライセンス/免責
このツールはサンプルとして提供されます。利用に伴う損害等について作者は責任を負いません。必要に応じて各自で検証の上ご利用ください。

