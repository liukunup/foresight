.DEFAULT_GOAL := help

PROFILES := --profile mysql --profile rustfs

.PHONY: help selfhost selfhost-down selfhost-logs selfhost-status restart clean uninstall test

help:
	@printf 'ForeSight\n\n'
	@printf '  make selfhost          一键部署 MySQL + RustFS，生成密钥、初始化并启动\n'
	@printf '  make selfhost-status   查看服务状态\n'
	@printf '  make selfhost-logs     跟踪 ForeSight 日志\n'
	@printf '  make selfhost-down     停止服务\n'
	@printf '  make restart           重启全部服务\n'
	@printf '  make clean             清理容器和 ./data（保留配置与数据卷）\n'
	@printf '  make uninstall         彻底卸载（含数据卷、配置与镜像）\n'
	@printf '  make test              运行测试套件\n'

selfhost:
	@bash scripts/selfhost.sh

selfhost-status:
	@docker compose ps

selfhost-logs:
	@docker compose logs -f foresight

selfhost-down:
	@docker compose $(PROFILES) down

restart:
	@docker compose $(PROFILES) restart
	@printf '服务已重启\n'

clean:
	@docker compose $(PROFILES) down --remove-orphans
	@rm -rf data
	@printf '已清理容器和 ./data；.env、config.toml 与数据卷保留\n'

uninstall:
	@docker compose $(PROFILES) down -v --remove-orphans
	@rm -rf data
	@rm -f .env config.toml
	@docker image rm foresight:latest >/dev/null 2>&1 || true
	@printf '已卸载：容器、数据卷、配置、./data 与镜像均已移除\n'

test:
	@uv run pytest -q
