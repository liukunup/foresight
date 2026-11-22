.DEFAULT_GOAL := help

.PHONY: help selfhost selfhost-down selfhost-logs selfhost-status test

help:
	@printf 'ForeSight\n\n'
	@printf '  make selfhost          一键部署 MySQL + RustFS，生成密钥、初始化并启动\n'
	@printf '  make selfhost-status   查看服务状态\n'
	@printf '  make selfhost-logs     跟踪 ForeSight 日志\n'
	@printf '  make selfhost-down     停止服务（数据卷保留）\n'
	@printf '  make test              运行测试套件\n'

selfhost:
	@bash scripts/selfhost.sh

selfhost-status:
	@docker compose ps

selfhost-logs:
	@docker compose logs -f foresight

selfhost-down:
	@docker compose down

test:
	@uv run pytest -q
