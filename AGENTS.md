# Maze v2 工作约束

- 仅在 `v2` 分支开发；修改前检查分支和未提交内容。不改其他分支。
- 本机 Python/测试/依赖操作使用 `/opt/anaconda3/envs/maze-v2`，推荐
  `conda run -p /opt/anaconda3/envs/maze-v2 ...`。不要改 base 或其他环境。
- 以 `Maze_v2_Codex_Implementation_Plan.md` 的完整要求为目标，结合用户后续指令。
- 实现使用 Python，尽量标注类型，保持直接易读。简洁不能削弱正确性和验收。
- 当前只有 macOS ARM64 / 48GB 本机环境，GPU 和真实多机验证等待用户提供环境。
- 不默认提交、推送、下载模型、部署外部集群或停止不属于本任务的服务。
- 逐阶段维护 `docs/v2/IMPLEMENTATION_STATUS.md`，实际结果记录在测试报告中。
