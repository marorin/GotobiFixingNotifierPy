"""
Gotobi Fixing Notifier (Python)

- 祝日CSV（日本/米国）をローカルから読み込む（UTF-8）
- 土日/祝日/年末年始(12/31-1/3)を非営業日として「前営業日に前倒し」した実質ゴトー日(F)を判定
- 判定結果が「通知対象ウィンドウ内」なら、ntfy.sh に事前通知
- stateファイルに「最終通知したF(YYYYMMDD)」を保存し、同一Fの重複通知を抑止

想定: CRONで定刻起動（スクリプト内部で「通知開始時刻」は持たない）
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


JST = dt.timezone(dt.timedelta(hours=9), name="JST")
SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_path(p: Path) -> Path:
    return p if p.is_absolute() else (SCRIPT_DIR / p)


@dataclasses.dataclass(frozen=True)
class Config:
    # gotobi rules
    include_day31: bool = True ## 31日を候補にするかどうか
    include_feb_last_day: bool = True ## 2月最終日を候補にするかどうか
    exclude_yearend_bank_closure: bool = True  ## 12/31-1/3 を非営業日扱い

    # holiday CSV files (local)
    enable_holiday_jp: bool = True ## 日本祝日を有効にするかどうか
    enable_holiday_us: bool = True ## 米国祝日を有効にするかどうか
    holiday_csv_jp: Path = Path("jp_holidays.csv") ## 日本祝日CSVパス
    holiday_csv_us: Path = Path("fed_bank_holidays.csv") ## 米国祝日CSVパス

    # notify window (JST)
    # 通知ウィンドウ: 前日10:00(JST) ～ 当日9:55(JST) の間のみ通知可
    enforce_window: bool = True ## 通知ウィンドウフィルタを有効にするかどうか
    window_prev_day_start_hhmm: tuple[int, int] = (10, 0) ## 通知ウィンドウ開始時刻
    window_fixing_end_hhmm: tuple[int, int] = (9, 55) ## 通知ウィンドウ終了時刻

    # ntfy
    ntfy_server: str = "https://ntfy.sh" ## サーバー
    ntfy_topic_raw: str = "your_topic_here" ## トピック名(他人と被らないように複雑なもの推奨)
    ntfy_title: str = "gotobi-fixing" ## タイトル(送信する通知のタイトル)
    ntfy_priority: str = "default" ## 優先度(送信する通知の優先度)

    # state
    state_file: Path = Path("gotobi-fixing.state.json") ## 最終通知したF(YYYYMMDD)をJSON形式で保存

    # notify mode
    notify_mode: str = "ntfy"  # "ntfy"(default) / "local"

    # test/runtime
    # `--now` 指定時: この時刻(JST)を「現在時刻」として判定する（未指定なら実時刻）
    test_now_jst: dt.datetime | None = None
    # テスト/運用用の個別スイッチ
    enable_ntfy: bool = True  # Falseなら送信しない
    enable_state_update: bool = True  # Falseならstate更新しない


def _date_key(d: dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def _is_leap_year(year: int) -> bool:
    return year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap_year(year) else 28
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    return 30


def _is_yearend_closure_day(d: dt.date) -> bool:
    return (d.month == 12 and d.day == 31) or (d.month == 1 and 1 <= d.day <= 3)


def _try_parse_holiday_token(token: str) -> dt.date | None:
    s = token.strip().replace("\r", "").strip()
    if not s:
        return None

    # Normalize separators, then validate 8 digits
    for ch in ("-", "/", "."):
        s = s.replace(ch, "")
    s = s.strip()
    if len(s) != 8 or not s.isdigit():
        return None

    year = int(s[0:4])
    month = int(s[4:6])
    day = int(s[6:8])
    if year <= 1900 or month < 1 or month > 12:
        return None
    dim = _days_in_month(year, month)
    if day < 1 or day > dim:
        return None
    return dt.date(year, month, day)


def load_holiday_keys(csv_path: Path) -> set[int]:
    """
    読み取り:
    - 空行スキップ
    - 行頭 # / // コメントスキップ、行中の # / // 以降も除去
    - 区切り: ',' / '\\t' / ';'（全て ',' に寄せる）
    - 行内の全トークンを日付として解釈できるものだけ採用
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Holiday CSV not found: {csv_path}")

    keys: set[int] = set()
    with csv_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.replace("\r", "").strip()
            if not line:
                continue

            # line comments
            if line.startswith("#") or line.startswith("//"):
                continue

            # strip inline comments
            hash_pos = line.find("#")
            if hash_pos >= 0:
                line = line[:hash_pos].strip()
            slash_pos = line.find("//")
            if slash_pos >= 0:
                line = line[:slash_pos].strip()
            if not line:
                continue

            line = line.replace("\t", ",").replace(";", ",")
            tokens = [t.strip() for t in line.split(",") if t.strip()]
            if not tokens:
                continue

            for t in tokens:
                d = _try_parse_holiday_token(t)
                if d is not None:
                    keys.add(_date_key(d))

    if not keys:
        raise ValueError(f"No valid holiday dates found in: {csv_path}")
    return keys


def _is_holiday(d: dt.date, holiday_keys: set[int]) -> bool:
    return _date_key(d) in holiday_keys


def normalize_biz_day(
    d: dt.date,
    *,
    holiday_keys_jp: set[int] | None,
    holiday_keys_us: set[int] | None,
    cfg: Config,
) -> dt.date:
    """
    土日・祝日・年末年始なら前営業日に前倒し（最大60日遡り）
    """
    cur = d
    for _ in range(60):
        is_weekend = cur.weekday() >= 5  # 5=Sat,6=Sun
        is_holiday = False
        if cfg.enable_holiday_jp and holiday_keys_jp is not None:
            is_holiday = is_holiday or _is_holiday(cur, holiday_keys_jp)
        if cfg.enable_holiday_us and holiday_keys_us is not None:
            is_holiday = is_holiday or _is_holiday(cur, holiday_keys_us)
        is_yearend = cfg.exclude_yearend_bank_closure and _is_yearend_closure_day(cur)

        if (not is_weekend) and (not is_holiday) and (not is_yearend):
            return cur
        cur = cur - dt.timedelta(days=1)
    return cur


def build_gotobi_base_days(year: int, month: int, cfg: Config) -> list[int]:
    dim = _days_in_month(year, month)
    days: list[int] = []

    for d in (5, 10, 15, 20, 25, 30):
        if d <= dim:
            days.append(d)

    if cfg.include_day31 and dim >= 31:
        # EA互換: 年末年始除外ONのとき12月の31日は候補にしない
        if not (cfg.exclude_yearend_bank_closure and month == 12):
            days.append(31)

    if cfg.include_feb_last_day and month == 2:
        days.append(dim)

    return days


def is_fixing_day(
    target: dt.date,
    *,
    holiday_keys_jp: set[int] | None,
    holiday_keys_us: set[int] | None,
    cfg: Config,
) -> tuple[bool, int]:
    """
    target（日付だけ）が「実質ゴトー日(F)」か？
    戻り値: (True/False, base_day)
    base_day は元の日付（5/10/.., 31, 2月最終日など）
    """
    for base_day in build_gotobi_base_days(target.year, target.month, cfg):
        base_date = dt.date(target.year, target.month, base_day)
        normalized = normalize_biz_day(
            base_date,
            holiday_keys_jp=holiday_keys_jp,
            holiday_keys_us=holiday_keys_us,
            cfg=cfg,
        )

        # EA互換: 正規化後が年末年始なら候補から除外
        if cfg.exclude_yearend_bank_closure and _is_yearend_closure_day(normalized):
            continue

        if normalized == target:
            return True, base_day
    return False, 0


def choose_fixing_date(now_jst: dt.datetime, *, holiday_keys_jp: set[int] | None, holiday_keys_us: set[int] | None, cfg: Config) -> tuple[dt.date | None, int]:
    """
    JST日付で「今日 or 明日」がFなら採用。それ以外は通知対象外。
    """
    today = now_jst.date()
    tomorrow = today + dt.timedelta(days=1)

    ok_today, base_today = is_fixing_day(today, holiday_keys_jp=holiday_keys_jp, holiday_keys_us=holiday_keys_us, cfg=cfg)
    if ok_today:
        return today, base_today

    ok_tom, base_tom = is_fixing_day(tomorrow, holiday_keys_jp=holiday_keys_jp, holiday_keys_us=holiday_keys_us, cfg=cfg)
    if ok_tom:
        return tomorrow, base_tom

    return None, 0


def in_notify_window(now_jst: dt.datetime, fixing_date: dt.date, cfg: Config) -> bool:
    prev_date = fixing_date - dt.timedelta(days=1)
    sh, sm = cfg.window_prev_day_start_hhmm
    eh, em = cfg.window_fixing_end_hhmm
    window_start = dt.datetime(prev_date.year, prev_date.month, prev_date.day, sh, sm, tzinfo=JST)
    window_end = dt.datetime(fixing_date.year, fixing_date.month, fixing_date.day, eh, em, tzinfo=JST)
    return window_start <= now_jst < window_end


def ntfy_publish(*, server: str, topic: str, title: str, priority: str, message: str, timeout_sec: int = 15) -> None:
    server = server.rstrip("/")
    topic = topic.strip().lstrip("/")
    url = f"{server}/{topic}"

    data = message.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    # ntfy documented headers: Title / Priority
    req.add_header("Title", title)
    req.add_header("Priority", priority)

    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        # 2xx expected; urllib raises for HTTPError otherwise
        _ = resp.read()


def _escape_applescript_string(s: str) -> str:
    # Escape backslash and double quotes for AppleScript string literal
    return s.replace("\\", "\\\\").replace('"', '\\"')


def local_notify(*, title: str, message: str) -> bool:
    """
    端末ローカル通知（可能ならOS/環境に応じて通知）。
    - Android (Termux): termux-notification
    - Linux: notify-send
    - macOS: osascript (display notification)
    - Windows: PowerShell NotifyIcon balloon (フォールバック的)
    見つからない/失敗した場合は False を返す。
    """
    # Termux on Android
    if shutil.which("termux-notification"):
        try:
            subprocess.run(
                ["termux-notification", "--title", title, "--content", message],
                check=False,
                capture_output=True,
                text=True,
            )
            return True
        except Exception:
            return False

    # Linux desktop (if available)
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                text=True,
            )
            return True
        except Exception:
            return False

    # macOS
    if sys.platform == "darwin" and shutil.which("osascript"):
        try:
            t = _escape_applescript_string(title)
            m = _escape_applescript_string(message)
            script = f'display notification "{m}" with title "{t}"'
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
            )
            return True
        except Exception:
            return False

    # Windows (best-effort): balloon tip via NotifyIcon
    if sys.platform.startswith("win") and shutil.which("powershell"):
        try:
            # Avoid breaking the command with quotes/newlines; keep it simple.
            safe_title = title.replace("\r", " ").replace("\n", " ").replace("'", "''")
            safe_msg = message.replace("\r", " ").replace("\n", " ").replace("'", "''")
            ps = (
                "[void][reflection.assembly]::LoadWithPartialName('System.Windows.Forms');"
                "[void][reflection.assembly]::LoadWithPartialName('System.Drawing');"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f"$n.BalloonTipTitle='{safe_title}';"
                f"$n.BalloonTipText='{safe_msg}';"
                "$n.Visible=$true;"
                "$n.ShowBalloonTip(10000);"
                "Start-Sleep -Seconds 10;"
                "$n.Dispose();"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Sta", "-Command", ps],
                check=False,
                capture_output=True,
                text=True,
            )
            return True
        except Exception:
            return False

    return False


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        with state_file.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def save_state(state_file: Path, obj: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(state_file)


def build_message(now_jst: dt.datetime, fixing_date: dt.date, base_day: int) -> str:
    fixing955 = dt.datetime(fixing_date.year, fixing_date.month, fixing_date.day, 9, 55, tzinfo=JST)
    remaining_minutes = max(0, int((fixing955 - now_jst).total_seconds() // 60))
    rem_hours, rem_mins = divmod(remaining_minutes, 60)
    return (
        f"【五十日仲値アラート】JST {fixing_date.year:04d}/{fixing_date.month:02d}/{fixing_date.day:02d}"
        f"（ベース日付={base_day}日）の仲値日9:55まで残り{rem_hours}時間{rem_mins:02d}分です。"
    )


def _parse_now_arg_to_jst(s: str) -> dt.datetime:
    """
    `--now` 用のパーサ。
    受け付ける例:
      - 2026-01-02 12:34
      - 2026-01-02T12:34
      - 2026-01-02T12:34:56
      - 2026-01-02T12:34:56+09:00
      - 20260102T1234
      - 20260109 4:30
    タイムゾーン未指定の場合は JST として扱う。
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("--now is empty")

    # Compact format: YYYYMMDDTHHMM / YYYYMMDDHHMM
    t = raw.replace(" ", "T")

    # Accept: YYYYMMDDTH:MM / YYYYMMDDTHH:MM (hour may be 1 digit), with optional seconds/tz
    # Example: 20260109 4:30  -> 20260109T4:30 -> 2026-01-09T04:30
    if len(t) >= 10 and t[:8].isdigit() and t[8] == "T" and (":" in t[9:]):
        date8 = t[:8]
        rest = t[9:]

        # Handle 'Z' suffix (UTC)
        tz_part = ""
        if rest.endswith("Z"):
            rest = rest[:-1]
            tz_part = "+00:00"

        # Split timezone part (+HH:MM / -HH:MM) if present
        time_part = rest
        for i in range(1, len(rest)):
            if rest[i] in "+-" and rest[i - 1].isdigit():
                time_part = rest[:i]
                tz_part = rest[i:]
                break

        parts = time_part.split(":")
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f"Invalid --now time part: {raw}")
        hh_s, mm_s = parts[0], parts[1]
        ss_s = parts[2] if len(parts) == 3 else None
        if (not hh_s.isdigit()) or (not mm_s.isdigit()) or (ss_s is not None and (not ss_s.isdigit())):
            raise ValueError(f"Invalid --now time digits: {raw}")
        hh = int(hh_s)
        mm = int(mm_s)
        ss = int(ss_s) if ss_s is not None else None
        if hh < 0 or hh > 23 or mm < 0 or mm > 59 or (ss is not None and (ss < 0 or ss > 59)):
            raise ValueError(f"Invalid --now time range: {raw}")

        yyyy = date8[:4]
        mo = date8[4:6]
        dd = date8[6:8]
        if ss is None:
            t = f"{yyyy}-{mo}-{dd}T{hh:02d}:{mm:02d}{tz_part}"
        else:
            t = f"{yyyy}-{mo}-{dd}T{hh:02d}:{mm:02d}:{ss:02d}{tz_part}"

    if len(t) == 13 and t[8] == "T" and t[:8].isdigit() and t[9:].isdigit():
        t = f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[9:11]}:{t[11:13]}"
    elif len(t) == 12 and t[:12].isdigit():
        t = f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}"

    try:
        parsed = dt.datetime.fromisoformat(t)
    except Exception as e:
        raise ValueError(f"Invalid --now format: {raw}") from e

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def run_once(cfg: Config) -> int:
    now_jst = cfg.test_now_jst or dt.datetime.now(tz=JST)

    # holidays
    holiday_keys_jp = None
    holiday_keys_us = None
    if cfg.enable_holiday_jp:
        holiday_keys_jp = load_holiday_keys(cfg.holiday_csv_jp)
    if cfg.enable_holiday_us:
        holiday_keys_us = load_holiday_keys(cfg.holiday_csv_us)

    fixing_date, base_day = choose_fixing_date(now_jst, holiday_keys_jp=holiday_keys_jp, holiday_keys_us=holiday_keys_us, cfg=cfg)
    if fixing_date is None:
        print(f"[{now_jst.isoformat()}] Fixingなし（今日/明日）: 通知なし")
        return 0

    if cfg.enforce_window and not in_notify_window(now_jst, fixing_date, cfg):
        print(
            f"[{now_jst.isoformat()}] Fixing={fixing_date} は検出したが、通知ウィンドウ外: 通知なし"
        )
        return 0

    fixing_key = _date_key(fixing_date)
    state = load_state(cfg.state_file)
    last_key = int(state.get("last_notified_fixing_yyyymmdd") or 0)
    if last_key == fixing_key:
        print(f"[{now_jst.isoformat()}] 既に通知済み(F={fixing_key}): スキップ")
        return 0

    msg = build_message(now_jst, fixing_date, base_day)
    print(f"[{now_jst.isoformat()}] 通知: {msg}")

    if cfg.notify_mode == "local":
        ok = local_notify(title=cfg.ntfy_title, message=msg)
        if not ok:
            print(f"[{now_jst.isoformat()}] warn: ローカル通知に失敗/未対応のため標準出力のみ")
    else:
        # default: ntfy
        topic = cfg.ntfy_topic_raw
        if cfg.enable_ntfy:
            ntfy_publish(
                server=cfg.ntfy_server,
                topic=topic,
                title=cfg.ntfy_title,
                priority=cfg.ntfy_priority,
                message=msg,
            )
        else:
            print(f"[{now_jst.isoformat()}] skip: ntfy送信は無効です（--no-ntfy）")

    if cfg.enable_state_update:
        state_payload: dict = {
            "last_notified_fixing_yyyymmdd": fixing_key,
            "last_notified_at_jst": now_jst.isoformat(),
            "notify_mode": cfg.notify_mode,
        }
        if cfg.notify_mode == "ntfy":
            state_payload.update(
                {
                    "ntfy_server": cfg.ntfy_server,
                    "ntfy_topic": cfg.ntfy_topic_raw,
                }
            )
        state.update(
            state_payload
        )
        save_state(cfg.state_file, state)
        print(f"[{now_jst.isoformat()}] state更新: {cfg.state_file}")
    else:
        print(f"[{now_jst.isoformat()}] skip: state更新は無効です（--no-state）")
    return 0


def parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser()
    p.add_argument("--jp", default=os.getenv("GOTOBI_HOLIDAY_JP", "jp_holidays.csv"), help="日本祝日CSVパス")
    p.add_argument("--us", default=os.getenv("GOTOBI_HOLIDAY_US", "fed_bank_holidays.csv"), help="米国祝日CSVパス")
    p.add_argument("--state", default=os.getenv("GOTOBI_STATE", "gotobi-fixing.state.json"), help="state jsonパス")
    p.add_argument("--ntfy-server", default=os.getenv("NTFY_SERVER", "https://ntfy.sh"), help="ntfy server")
    p.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC", "gotobi-fixing_kX7mP9nR2vQ4"), help="ntfy topic")
    p.add_argument("--ntfy-title", default=os.getenv("NTFY_TITLE", "gotobi-fixing"), help="ntfy title")
    p.add_argument("--ntfy-priority", default=os.getenv("NTFY_PRIORITY", "default"), help="ntfy priority")
    p.add_argument(
        "--notify",
        choices=("ntfy", "local"),
        default=os.getenv("GOTOBI_NOTIFY_MODE", "ntfy"),
        help="通知方式: ntfy(既定) / local(端末ローカル通知)",
    )
    p.add_argument("--no-window", action="store_true", help="通知ウィンドウ判定を無効化")
    p.add_argument("--now", default=None, help="テスト用: 現在時刻を仮指定（例: '2026-01-02 12:34' / '2026-01-02T12:34:56+09:00'）")
    p.add_argument("--no-ntfy", action="store_true", help="テスト用: ntfy送信を行わない")
    p.add_argument("--no-state", action="store_true", help="テスト用: state更新を行わない")
    # 互換: 旧オプション名
    p.add_argument("--no-state-update", action="store_true", help=argparse.SUPPRESS)
    # 互換: 旧dry-run（送信もstate更新も止める）
    p.add_argument("--dry-run", action="store_true", help="(互換) --no-ntfy と --no-state を同時に指定")

    args = p.parse_args(argv)

    jp_path = resolve_path(Path(args.jp))
    us_path = resolve_path(Path(args.us))
    state_path = resolve_path(Path(args.state))

    now_jst: dt.datetime | None = None
    if args.now is not None:
        now_jst = _parse_now_arg_to_jst(args.now)

    return Config(
        holiday_csv_jp=jp_path,
        holiday_csv_us=us_path,
        state_file=state_path,
        ntfy_server=args.ntfy_server,
        ntfy_topic_raw=args.ntfy_topic,
        ntfy_title=args.ntfy_title,
        ntfy_priority=args.ntfy_priority,
        notify_mode=str(args.notify),
        enforce_window=not args.no_window,
        test_now_jst=now_jst,
        enable_ntfy=not (bool(args.no_ntfy) or bool(args.dry_run)),
        enable_state_update=not (bool(args.no_state) or bool(args.no_state_update) or bool(args.dry_run)),
    )


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)

    # retry policy: 失敗したら10秒後に「再判定」してもう一度だけ送る
    for attempt in (1, 2):
        try:
            return run_once(cfg)
        except (FileNotFoundError, ValueError) as e:
            print(f"[ERROR] 入力データ不備: {e}", file=sys.stderr)
            return 2
        except urllib.error.HTTPError as e:
            print(f"[ERROR] ntfy HTTPError: {e.code} {e.reason}", file=sys.stderr)
        except urllib.error.URLError as e:
            print(f"[ERROR] ntfy URLError: {e.reason}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] 予期せぬエラー: {e}", file=sys.stderr)

        if attempt == 1:
            time.sleep(10)
            continue

        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

