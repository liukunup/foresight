.PHONY: selfhost status down restart clean uninstall

PROFILES := --profile mysql --profile rustfs

selfhost:
	@bash scripts/selfhost.sh

status:
	@docker compose ps

down:
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
	@docker image rm liukunup/foresight:latest >/dev/null 2>&1 || true
	@printf '已卸载：容器、数据卷、配置、./data 与镜像均已移除\n'
