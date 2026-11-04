## ADDED Requirements

### Requirement: 服务器常驻实时采集
系统 SHALL 提供 `collect` 常驻服务：用单个公开 Ondo WS 连接订阅全市场盘口与成交，写盘前降采样（焦点市场每 focus_snapshot_s 存全量盘口，其他市场每分钟只存派生竞争指标，成交全部保存）；订阅焦点标的的币安 aggTrade；用 REST 轮询 Lighter 盘口；WS 断档期间用 REST 全市场扫描作后备。记录带接收时间与连接 ID，按小时轮转写入 `{source}/{YYYYMMDDTHH}Z-{run_id}.jsonl.gz`。不使用任何密钥。

#### Scenario: 降采样
- **WHEN** Ondo WS 以约 256ms 的频率推送全市场全量快照
- **THEN** 非焦点市场每分钟只写一条派生记录，焦点市场按 focus_snapshot_s 写入，成交不丢弃

#### Scenario: 同步调用不阻塞
- **WHEN** 执行 REST 请求或写盘
- **THEN** 在线程池或独立队列中进行，WS 消息处理不被阻塞

#### Scenario: 断线重连
- **WHEN** 任一 WS 连接断开或超时
- **THEN** 指数退避重连（最长 60 秒），按来源、市场、频道写入断档记录；断档在收到第一条有效消息时才结束，其余来源继续采集

#### Scenario: 静默与异常重启
- **WHEN** 某来源超过 silence_timeout_s 没有消息，或进程在上次异常退出后重新启动
- **THEN** 记录对应断档

#### Scenario: 心跳与连接寿命
- **WHEN** Ondo 连接持续存在，或币安连接接近 24 小时
- **THEN** 每 30 秒发送 ping；币安连接在 23 小时时主动重连并记录断档

#### Scenario: 优雅退出与磁盘上限
- **WHEN** 收到 SIGTERM 或 SIGINT，或磁盘用量达到 max_disk_gb
- **THEN** 冲刷缓冲区并关闭文件后退出；达到磁盘上限时以退出码 75 退出，不删除数据；systemd 单元配置 RestartPreventExitStatus=75

#### Scenario: 健康日志
- **WHEN** 服务运行中
- **THEN** 每分钟向 log/ 写入各来源消息数、最近消息年龄和重连次数

### Requirement: 采集数据导入回测
系统 SHALL 把采集到的 JSONL 导入 history SQLite：成交、按分钟计算的竞争量与价位深度、断档；回测在断档期间不报价，并标注覆盖情况。

#### Scenario: 断档期间
- **WHEN** 回测时间落在某来源的断档区间内
- **THEN** 依赖该来源的报价不新增；如果断档期间有持仓或挂单，标记"损益不可判定"，报告列出断档总时长

### Requirement: 服务器部署
系统 SHALL 提供 systemd 单元示例与中文部署说明；WS 依赖作为可选依赖 `live` 安装；未安装时回测与历史抓取仍可用。

#### Scenario: 未安装 websockets
- **WHEN** 环境中没有 websockets
- **THEN** collect 报出明确的中文错误并退出，其余命令不受影响
