# Texas

《明日方舟》PC 版卫戍协议实战伤害采集器。Texas 在命令行显示客户端记录的每回合伤害和本次采集的整局合计，可以输出 JSON Lines。它通过 Windows 的只读进程权限访问游戏统计。

当前版本适配 Windows x64 的「卫戍协议·规程·序列 I」（`act3autochess`），需要 64 位 Python 3.11 或更新版本，无第三方 Python 依赖。读取前会核对 `GameAssembly.dll` 和 `global-metadata.dat` 的 SHA-256。其他客户端构建需要重新核对数据结构。

## 使用

启动《明日方舟》PC 客户端，然后在本仓库目录执行：

```powershell
python .\texas.py
```

建议在进入卫戍协议前启动采集。每段战斗结束或切换阶段时输出一次。Texas 识别到对局结束时会打印已采集的合计。按 Ctrl+C 结束采集，也会输出当前已采集的合计。如果 Windows 拒绝读取，会报告相应错误。

```powershell
# 导出 JSON Lines，诊断信息仍显示在终端
python .\texas.py --json > .\damage.jsonl

# 可选。借用已有的卫戍协议助手资料显示干员名
python .\texas.py --catalog ..\StrongholdAssistant\data\codex_extra.json

# 单独核对客户端构建，无需启动游戏
python .\texas.py --check-client "D:\游戏目录\GameAssembly.dll"
```

`--pid` 可指定游戏进程 ID。`--interval-ms` 可调整采样间隔，默认 100 毫秒。未配资料的条目以游戏的干员 ID 显示。

## 伤害口径

- 采用客户端本方战斗统计的结算伤害。按游戏的干员 ID 汇总，同 ID 的棋子合并。召唤物有独立条目时保留其 ID。
- 每个「回合号＋战斗阶段」只计入一次。重新作战后以该阶段新采集到的结果替代旧记录。
- “读到结束状态”表示采样时游戏控制器已标为结束。“末次有效采样”可能早于最后一次伤害。整局合计只包含输出里列出的已采集阶段，开始采集之前的回合无法补录。
- 当前进行了客户端结构和代码的静态核对，尚未在实战中验证 Windows 进程读取或采集覆盖率。
