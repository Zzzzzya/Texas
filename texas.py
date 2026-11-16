"""Console output and round aggregation for Texas."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from battle_reader import Battle, BattleReader, BattleSample, Match, SnapshotError, UnsupportedClient, check_client
from win32_process import ProcessError, ProcessMemory

_TRANSIENT_READ_ERRORS = (299, 487, 998)


class Report:
    def __init__(self, json_output: bool, names: dict[str, str]):
        self.json_output = json_output
        self.names = names

    def _rows(self, amounts: dict[str, float]) -> list[dict]:
        rows = []
        for key, value in sorted(amounts.items(), key=lambda pair: (-pair[1], pair[0])):
            row = {"character_id": key, "damage": value}
            if key in self.names:
                row["name"] = self.names[key]
            rows.append(row)
        return rows

    def _print(self, title: str, fields: dict, amounts: dict[str, float]) -> None:
        rows = self._rows(amounts)
        if self.json_output:
            print(json.dumps({**fields, "operators": rows}), flush=True)
            return
        print(f"\n{title}")
        if not rows:
            print("  已采样，统计表中没有干员条目。")
        for row in rows:
            name = f"  {row['name']}" if "name" in row else ""
            print(f"  {row['character_id']:<34} {row['damage']:>14,.2f}{name}")
        sys.stdout.flush()

    def round(self, match_number: int, sample: BattleSample) -> None:
        assert sample.battle is not None
        battle = sample.battle
        coverage = "读到结束状态" if sample.finished else "末次有效采样，可能不完整"
        self._print(
            f"第 {match_number} 局 · 第 {battle.round_number} 回合 · {battle.phase}（{coverage}）",
            {
                "type": "round", "match": match_number, "round": battle.round_number,
                "phase": battle.phase, "last_frame": sample.frame, "observed_end": sample.finished,
            },
            sample.damage,
        )

    def match(self, match_number: int, results: dict[Battle, BattleSample], reason: str) -> None:
        totals: dict[str, float] = defaultdict(float)
        for sample in results.values():
            for character_id, value in sample.damage.items():
                totals[character_id] += value
        observed = sorted({battle.round_number for battle in results})
        ended = sum(sample.finished for sample in results.values())
        rounds = "、".join(map(str, observed))
        self._print(
            f"第 {match_number} 局已采集合计 · 回合 {rounds} · "
            f"{ended}/{len(results)} 个战斗读到结束状态（{reason}）",
            {
                "type": "match", "match": match_number, "reason": reason,
                "observed_rounds": observed, "observed_battles": len(results),
                "battles_with_end": ended,
            },
            totals,
        )


class MatchTracker:
    def __init__(self, report: Report):
        self.report = report
        self.match: Match | None = None
        self.match_number = 0
        self.pending: BattleSample | None = None
        self.last_battle: BattleSample | None = None
        self.results: dict[Battle, BattleSample] = {}

    def _finish_round(self) -> None:
        sample = self.pending
        self.pending = None
        if sample is None or sample.battle is None:
            return
        if self.results.get(sample.battle) != sample:
            # One result per numbered round and phase. A replay replaces the result.
            self.results[sample.battle] = sample
            self.report.round(self.match_number, sample)

    def finish(self, reason: str) -> None:
        self._finish_round()
        if self.results:
            self.report.match(self.match_number, self.results, reason)
        self.results.clear()
        self.match = None
        self.last_battle = None

    def observe(self, sample: BattleSample | None) -> None:
        if sample is None:
            return

        if self.match is not None and sample.match != self.match:
            self.finish("进入下一局")
        if sample.match_ended:
            self.finish("对局结束")
            return
        if sample.battle is None:
            self._finish_round()
            return
        if self.match is None:
            self.match = sample.match
            self.match_number += 1

        previous = self.last_battle
        if previous is not None:
            counters_reset = any(sample.damage.get(key, 0) < value for key, value in previous.damage.items())
            frame_reset = sample.frame < previous.frame
            if previous.battle != sample.battle:
                self._finish_round()
                # The UI status can change before the controller clears its previous counters.
                if any(previous.damage.values()) and not (
                    counters_reset or (frame_reset and sample.damage != previous.damage)
                ):
                    return
            elif previous.finished and not sample.finished:
                restarted = frame_reset and (
                    sample.battle_started_at != previous.battle_started_at or counters_reset
                )
                if not restarted:
                    return
        self.last_battle = sample
        self.pending = sample


def _interval(value: str) -> int:
    try:
        interval = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("使用 50 到 2000 之间的整数毫秒数。") from exc
    if not 50 <= interval <= 2000:
        raise argparse.ArgumentTypeError("使用 50 到 2000 之间的整数毫秒数。")
    return interval


def _load_names(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    operators = document.get("operators") if isinstance(document, dict) else None
    if not isinstance(operators, dict):
        raise ValueError("干员资料必须是 StrongholdAssistant 的 codex_extra.json。")
    names: dict[str, str] = {}
    for name, operator in operators.items():
        char_id = operator.get("charId") if isinstance(operator, dict) else None
        if isinstance(char_id, str) and name and name.isprintable():
            names[char_id] = name
    return names


def _watch(pid: int | None, interval: float, report: Report) -> None:
    tracker = MatchTracker(report)
    reason = "游戏已退出"
    with ProcessMemory.open(pid) as memory:
        check_client(memory.module_path)
        print(f"已连接 Arknights.exe（PID {memory.pid}）。等待卫戍协议战斗，按 Ctrl+C 停止。", file=sys.stderr)
        reader = BattleReader(memory)
        failed_reads = 0
        try:
            while memory.is_running():
                try:
                    snapshot = reader.sample()
                except (SnapshotError, ProcessError) as exc:
                    if not memory.is_running():
                        break
                    if isinstance(exc, ProcessError) and exc.winerror not in _TRANSIENT_READ_ERRORS:
                        raise
                    failed_reads += 1
                    if failed_reads == 10:
                        print(f"暂未取得有效快照，继续等待。{exc}", file=sys.stderr)
                else:
                    failed_reads = 0
                    tracker.observe(snapshot)
                time.sleep(interval)
        except KeyboardInterrupt:
            reason = "手动停止"
        except ProcessError:
            reason = "读取中断"
            raise
        finally:
            tracker.finish(reason)
            if tracker.match_number == 0:
                print("未采集到有效的卫戍协议战斗。", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Texas · 卫戍协议实战伤害命令行采集器")
    parser.add_argument("--pid", type=int, help="指定 Arknights.exe 的进程 ID")
    parser.add_argument("--interval-ms", type=_interval, default=100, help="采样间隔，默认 100 毫秒")
    parser.add_argument("--catalog", type=Path, metavar="CODEX_EXTRA", help="用已有的 codex_extra.json 显示干员名")
    parser.add_argument("--json", action="store_true", help="将结果以 JSON Lines 输出到标准输出")
    parser.add_argument("--check-client", type=Path, metavar="GAMEASSEMBLY", help="只校验本地 GameAssembly.dll 和元数据版本")
    args = parser.parse_args()
    if args.check_client is not None and args.pid is not None:
        parser.error("--check-client 不能与 --pid 一起使用")

    try:
        if args.check_client is not None:
            check_client(args.check_client)
            print("客户端文件与已适配版本匹配。")
        else:
            _watch(args.pid, args.interval_ms / 1000, Report(args.json, _load_names(args.catalog)))
    except (OSError, ValueError, ProcessError, UnsupportedClient) as exc:
        print(f"Texas: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
