"""做市命令的参数与模式检查；交易逻辑延迟导入。"""

from pathlib import Path

from .maker_config import MakerSettings


COMMANDS = frozenset(('maker', 'maker-cancel-all'))


def register_commands(commands):
    maker = commands.add_parser('maker', help='实盘做市（默认 dry-run 只读）',
                                description='默认 dry-run；live 必须同时配置 mode=live 并加 --live')
    maker.add_argument('--config', type=Path, help='[maker] TOML 配置；启动时必填')
    maker.add_argument('--live', action='store_true', help='显式允许正式交易，仍需全部安全自检通过')
    maker.add_argument('--reset-halt', action='store_true', help='人工复位熔断锁，运行实例存在时拒绝')
    cancel = commands.add_parser('maker-cancel-all', help='按配置市场撤掉全部订单并核实为空')
    cancel.add_argument('--config', type=Path, required=True, help='与运行实例相同的 [maker] 配置')


def dispatch(args):
    reset = getattr(args, 'reset_halt', False)
    live = getattr(args, 'live', False)
    if reset and live:
        raise ValueError('人工复位不能同时使用 --live')
    if args.config is None:
        raise ValueError('启动或人工复位 maker 必须指定原实例的 --config')
    settings = MakerSettings.from_file(
        args.config, for_emergency=reset or args.command == 'maker-cancel-all')
    if args.command == 'maker' and not reset:
        if live != (settings.mode == 'live'):
            raise ValueError('live 必须同时满足配置 mode="live" 和命令行 --live')
    # 账户 flock 由这三个入口统一持有，避免 CLI 与运行循环重复获取同一把锁。
    from .maker import reset_halt, run_cancel_all, run_maker
    if reset:
        return reset_halt(settings)
    if args.command == 'maker-cancel-all':
        return run_cancel_all(settings)
    return run_maker(settings, live=live)
