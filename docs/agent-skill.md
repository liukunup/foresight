# 安装 AI Skill

```bash
./scripts/install_skill.sh --target opencode     # 或 --target claude
export FORESIGHT_API_TOKEN="..."                 # 脚本会提示下一步
```

脚本把 skill 复制到 agent 的 skills 目录，并把连接信息写在 `~/.config/foresight/connection.toml`（里面只有 API 地址和**环境变量名**，不存 token 明文）。

装好之后 AI 就能自己查这个归档：「上周谁发言最多」「大家都在抱怨什么」「这个频道的活跃时间是什么时候」——它会自己去调 API、自己算、自己给结论，而不是让你手动翻数据库。
