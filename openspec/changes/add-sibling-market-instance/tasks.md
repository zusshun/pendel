## 1. Implementation
- [x] 1.1 配置：sibling_markets、peer_heartbeat_paths、heartbeat_path、peer_stale_s、peer_cancel_timeout_s、stop_sweep_interval_s，并加上校验
- [x] 1.2 其他市场持仓检查改为按白名单判断；兄弟市场不加入 AccountState.markets；兄弟模式下窗口账本不记录账户资金费
- [x] 1.3 本实例心跳写入；对端心跳看护：代撤、核零、告警；代撤失败则熔断
- [x] 1.4 多实例模式退出时不关闭死人开关
- [x] 1.5 在公共停止路径（manager.stop）上对重复清扫限频
- [x] 1.6 告警加市场名前缀
- [x] 1.7 deploy：RDDT 的 systemd 单元和配置示例（独立 data_dir）；IBM 配置同步升级
- [ ] 1.8 测试：所有场景，以及 sibling_markets 为空时行为不变

  已新增 68 项测试覆盖配置、心跳、代撤、熔断、REST/WS 隔离、账本、清扫限频、M27 恢复和空配置回归；原有五个集成测试文件仅扩展市场前缀断言。当前所有 Telegram 都加市场前缀，因此空配置的告警文本发生变化；“所有 Telegram 加前缀”与“空配置逐字节不变”无法同时满足，已请求确认，尚不勾选该项。
## 2. Verification
- [x] 2.1 全量测试通过
- [ ] 2.2 正式环境：两个实例同时运行；kill -9 其中一个后，另一个在 30 秒内代撤，挂单归零
